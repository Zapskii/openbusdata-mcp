import asyncio
import io
import json
import zipfile
from urllib.parse import quote
from xml.sax.saxutils import escape as xml_escape

import httpx
import pytest

import openbusdata_mcp.server as server


def _check(condition, message: str) -> None:
    """Assert without rendering the operand.

    pytest renders an `assert` statement's operands on failure, and several
    operands in this file are request URLs carrying ?api_key=... or the bodies
    of live tools. With a real key in the environment a failing assertion would
    print the credential into the test output, where it ends up in transcripts
    and CI logs. Call sites pass a thunk and a message that names what was
    expected, never the value observed. (Ruling R34.)
    """
    if not condition():
        raise AssertionError(message)


def _assert_no_key(get_body, where: str) -> None:
    """No encoding of DUMMY_KEY reaches the body `get_body` returns."""
    forms = _leaked(get_body())
    if forms:
        raise AssertionError(
            f"API key encoding reached {where} ({len(forms)} form(s))")


# --- Test 1: spec-generated tools URL-encode path params
# Find the generated timetables dataset-by-ID tool and capture its request URL.
class _FakeResp:
    status_code = 200
    text = "{}"
    content = b"{}"

    def raise_for_status(self):
        pass

    def json(self):
        return {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_bytes(self):
        yield self.content


captured = []


class _FakeClient:
    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aclose(self):
        pass

    async def get(self, url):
        captured.append(url)
        return _FakeResp()

    def stream(self, method, url):
        captured.append(url)
        raise NotImplementedError(
            "zip-download tests must override stream() with a response that "
            "aiter_bytes()s a real zip; the base _FakeResp.content=b\"{}\" (2 bytes) "
            "is not a zip")


# --- Test 2: HTTP error bodies returned to the MCP client are truncated
class _FakeResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


class _FailingClient(_FakeClient):
    async def get(self, url):
        import httpx
        raise httpx.HTTPStatusError(
            "500 Server Error", request=None,
            response=_FakeResponse(500, "E" * 5000))


# --- Test 4: soft failures are counted as errors, not loaded
def _make_zip(xml: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.xml", xml)
    return buf.getvalue()


class _Resp:
    def __init__(self, status_code=200, content=b"{}"):
        self.status_code = status_code
        self.content = content
        self.text = content.decode("utf-8", "ignore")
    def json(self):
        return json.loads(self.content)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_bytes(self):
        yield self.content


class _ZipFailClient(_FakeClient):
    """meta OK, zip download 404s -> _load_dataset must return None."""
    async def get(self, url):
        if "/dataset/1/" in url:
            return _Resp(200, b'{"operatorName":"Op","url":"http://x/y.zip","modified":"2026-01-01T00:00:00Z"}')
        return _Resp(404, b"")

    def stream(self, method, url):
        assert method == "GET", f"unexpected stream method: {method}"
        return _Resp(404, b"")


class _ZipOkClient(_FakeClient):
    async def get(self, url):
        if "/dataset/2/" in url:
            return _Resp(200, b'{"operatorName":"Op","url":"http://x/y.zip","modified":"2026-01-01T00:00:00Z"}')
        return _Resp(404, b"")

    def stream(self, method, url):
        assert method == "GET", f"unexpected stream method: {method}"
        return _Resp(200, _make_zip(b"<TransXChange/>"))


# --- Test 7: _load_dataset reuses a caller-supplied client (no new one)
class _CountingClient(_FakeClient):
    instances = 0
    def __init__(self, **kw):
        _CountingClient.instances += 1
        super().__init__(**kw)


# --- Test 12: force_refresh purges datasets withdrawn from the catalogue
class _CatClient(_FakeClient):
    async def get(self, url):
        if "/dataset/?" in url:
            return _Resp(200, b'{"results":[{"id":1}]}')
        return _Resp(200, b'{"operatorName":"Op","url":"http://x/y.zip","modified":"2026-01-01T00:00:00Z"}')

    def stream(self, method, url):
        assert method == "GET", f"unexpected stream method: {method}"
        return _Resp(200, _make_zip(b"<TransXChange/>"))


# --- Test 13: mid-rewrite failure rolls back the purge
_JOURNEY_XML = b"""<TransXChange>
  <PublishedLineName>2</PublishedLineName>
  <JourneyPatternSection id="jps1">
    <JourneyPatternTimingLink>
      <From><StopPointRef>010A</StopPointRef></From>
      <To><StopPointRef>010B</StopPointRef></To>
      <RunTime>PT10M</RunTime>
    </JourneyPatternTimingLink>
  </JourneyPatternSection>
  <JourneyPattern id="jp1">
    <Direction>outbound</Direction>
    <JourneyPatternSectionRefs>jps1</JourneyPatternSectionRefs>
  </JourneyPattern>
  <VehicleJourney>
    <VehicleJourneyCode>J2</VehicleJourneyCode>
    <JourneyPatternRef>jp1</JourneyPatternRef>
    <DepartureTime>09:00:00</DepartureTime>
    <OperatingProfile><RegularDayType><DaysOfWeek><Monday/></DaysOfWeek></RegularDayType></OperatingProfile>
  </VehicleJourney>
</TransXChange>"""


class _ZipJourneyClient(_FakeClient):
    async def get(self, url):
        if "/dataset/2/" in url:
            return _Resp(200, b'{"operatorName":"Op","url":"http://x/y.zip","modified":"2026-01-01T00:00:00Z"}')
        return _Resp(404, b"")

    def stream(self, method, url):
        assert method == "GET", f"unexpected stream method: {method}"
        return _Resp(200, _make_zip(_JOURNEY_XML))


# --- Test 20: watermark stays put when soft failures exceed the delta budget
class _DeltaFailClient(_FakeClient):
    """Catalogue + per-dataset meta OK, every zip download 404s."""
    async def get(self, url):
        if "/dataset/?" in url:
            return _Resp(200, b'{"results":[{"id":2,"modified":"2026-09-01T00:00:00Z"},'
                               b'{"id":3,"modified":"2026-09-01T00:00:00Z"},'
                               b'{"id":4,"modified":"2026-09-01T00:00:00Z"}]}')
        if any(f"/dataset/{i}/" in url for i in (2, 3, 4)):
            return _Resp(200, b'{"operatorName":"Op","url":"http://x/y.zip","modified":"2026-09-01T00:00:00Z"}')
        return _Resp(404, b"")

    def stream(self, method, url):
        assert method == "GET", f"unexpected stream method: {method}"
        return _Resp(404, b"")


# --- Test 21: a partial catalogue sweep must not purge loaded datasets
_PARTIAL_PAGE = json.dumps(
    {"results": [{"id": 1000 + i, "modified": "2026-09-01T00:00:00Z"}
                 for i in range(100)]}).encode()


class _PartialSweepClient(_FakeClient):
    """First catalogue page full (100 results), second page 500s."""
    async def get(self, url):
        if "/dataset/?" in url and "offset=0" in url:
            return _Resp(200, _PARTIAL_PAGE)
        if "/dataset/?" in url:
            return _Resp(500, b"")
        if "/dataset/2/" in url:
            return _Resp(200, b'{"operatorName":"Op","url":"http://x/y.zip","modified":"2026-09-01T00:00:00Z"}')
        return _Resp(404, b"")

    def stream(self, method, url):
        assert method == "GET", f"unexpected stream method: {method}"
        return _Resp(200, _make_zip(b"<TransXChange/>"))


# --- Test 9: _sweep_catalogue paginates and returns ({id: entry}, complete)
class _PagedClient(_FakeClient):
    def __init__(self, **kw):
        self.calls = 0
    async def get(self, url):
        self.calls += 1
        if self.calls == 1:
            return _Resp(200, b'{"results":[{"id":1},{"id":2}]}')
        return _Resp(200, b'{"results":[]}')


@pytest.fixture(autouse=True)
def _restore_httpx_client():
    """Each test swaps server.httpx.AsyncClient for a fake; restore the real one.
    Also reset the shared pooled client singleton so no stale fake leaks
    between tests (spec tools fetch via server.get_http_client())."""
    orig = server.httpx.AsyncClient
    yield
    server.httpx.AsyncClient = orig
    server._http_client = None


def _dataset_tool():
    for t in server.mcp._tool_manager.list_tools():
        if t.name == "timetables_api_v1_dataset_by_datasetID":
            return t.fn
    return None


# --- Test 1: spec-generated tools URL-encode path params
def test_1_path_params_url_encoded():
    captured.clear()
    server.httpx.AsyncClient = _FakeClient
    tool_fn = _dataset_tool()
    assert tool_fn is not None, "generated dataset tool not found"
    result = asyncio.run(tool_fn(datasetID="12?q=1#frag"))
    assert result == "{}", f"unexpected tool result: {result}"
    _check(lambda: len(captured) == 1,
           f"expected exactly one request, got {len(captured)}")
    url = captured[0]
    # '?', '#' and any stray '/' must be percent-encoded so the path survives.
    # Checked through _check rather than `assert`: these operands carry the key.
    _check(lambda: "/api/v1/dataset/12%3Fq%3D1%23frag" in url,
           "the dataset id must be percent-encoded into the path")
    path_part = url.split("api_key")[0].split("/api/v1/dataset/", 1)[1]
    _check(lambda: "?" not in path_part,
           "the parameter must not break out of the path")


# --- Test 2: HTTP error bodies returned to the MCP client are truncated
def test_2_remote_error_bodies_truncated():
    captured.clear()
    server.httpx.AsyncClient = _FailingClient
    tool_fn = _dataset_tool()
    assert tool_fn is not None, "generated dataset tool not found"
    result = asyncio.run(tool_fn(datasetID="5"))
    assert result.startswith("HTTP Error 500: "), result[:60]
    assert len(result) < 600, f"remote error body not truncated ({len(result)} chars)"
    assert "E" * 600 not in result, "error body leaked in full"


# --- Test 3: legacy dataset_meta promotion survives junk keys
def test_3_legacy_meta_promotion_guards_junk_keys(writer):
    writer.conn.execute(
        "INSERT INTO meta VALUES ('dataset_meta', ?)",
        (json.dumps({"42": {"modified": "2026-01-01T00:00:00Z", "operator": "Op"},
                     "not-a-number": {"modified": "x", "operator": "Op"},
                     "7": "junk-not-a-dict"}),))
    writer.conn.commit()
    writer.ensure_schema()  # re-run: must promote 42, skip junk, not crash
    assert writer.loaded_ids() == {42}, \
        f"promotion produced garbage: {writer.loaded_ids()}"
    assert 42 in writer.loaded_ids(), "valid legacy dataset lost"
    row = writer.conn.execute("SELECT k FROM meta WHERE k='dataset_meta'").fetchone()
    assert row is None, "legacy meta key not consumed"


# --- Test 4: soft failures are counted as errors, not loaded
def test_4_soft_failures_counted_as_errors():
    server.httpx.AsyncClient = _ZipFailClient
    assert asyncio.run(server._load_dataset(1)) is None, "404 zip not reported as failure"
    server.httpx.AsyncClient = _ZipOkClient
    assert asyncio.run(server._load_dataset(2)) is not None, "good zip not reported as success"


# --- Test 5: plan_journey dedup keeps distinct operators on the same route
# Seed two journeys: same route number + depart, different operators.
def test_5_plan_dedup_keeps_distinct_operators(writer):
    writer.add_stop("010A", "A St"); writer.add_stop("010B", "B St")
    writer.add_journey("OpOne", "1", "outbound", "J1", {"mon"},
                       [{"naptan": "010A", "arrival": None, "departure": "09:00:00"},
                        {"naptan": "010B", "arrival": "09:10:00", "departure": None}], 1)
    writer.add_journey("OpTwo", "1", "outbound", "J2", {"mon"},
                       [{"naptan": "010A", "arrival": None, "departure": "09:00:00"},
                        {"naptan": "010B", "arrival": "09:12:00", "departure": None}], 2)
    writer.commit()
    result = asyncio.run(server.plan_journey("A St", "B St", "09:30", "mon", 0, check_disruptions=False))
    plans = json.loads(result)
    assert len(plans) == 2, f"expected 2 distinct plans, got {len(plans)}"
    assert {p["legs"][0]["operator"] for p in plans} == {"OpOne", "OpTwo"}, \
        "dedup collapsed distinct operators"


class _FakeLiveResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


class _FakeSharedClient:
    def __init__(self):
        self.urls = []

    async def get(self, url):
        captured.append(url)
        return _FakeLiveResponse(
            b'<?xml version="1.0"?><Siri><VehicleActivity>'
            b'<MonitoredVehicleJourney><VehicleRef>V1</VehicleRef>'
            b'</MonitoredVehicleJourney></VehicleActivity></Siri>')


# --- Test 6: live-buses URL params are percent-encoded (shared client)
def test_6_live_buses_params_url_encoded():
    captured.clear()
    server.set_http_client(_FakeSharedClient())
    try:
        result = asyncio.run(server.get_live_buses_on_route("A&B", "1 2"))
    finally:
        server.set_http_client(None)
    _check(lambda: "operatorRef=A%26B" in captured[0],
           "operatorRef must be percent-encoded")
    _check(lambda: "lineRef=1%202" in captured[0],
           "lineRef must be percent-encoded")


# --- Test 7: _load_dataset reuses a caller-supplied client (no new one)
def test_7_load_dataset_reuses_shared_client():
    _CountingClient.instances = 0
    server.httpx.AsyncClient = _CountingClient
    async def _run():
        async with server.httpx.AsyncClient() as client:
            return await server._load_dataset(2, client=client)
    asyncio.run(_run())
    assert _CountingClient.instances == 1, f"expected 1 client, got {_CountingClient.instances}"


# --- Test 8: _xml_contents yields one XML at a time
def test_8_xml_contents_lazy_generator():
    import zipfile as _zf
    buf = io.BytesIO()
    with _zf.ZipFile(buf, "w") as z:
        z.writestr("a.xml", "<A/>")
        z.writestr("b.txt", "not xml")
        z.writestr("c.xml", "<C/>")
    buf.seek(0)
    z = _zf.ZipFile(buf)
    assert list(server._xml_contents(z)) == ["<A/>", "<C/>"], "non-xml leaked or order wrong"


# --- Test 12: force_refresh purges datasets withdrawn from the catalogue
def test_12_force_refresh_reconciles_withdrawn_datasets(writer):
    writer.mark_dataset_loaded(2, "2026-01-01T00:00:00Z", "Op"); writer.commit()
    server.httpx.AsyncClient = _CatClient
    asyncio.run(server.load_all_timetable_data(force_refresh=True))
    assert 2 not in writer.loaded_ids(), "withdrawn dataset not purged on force_refresh"


# --- Test 13: mid-rewrite failure rolls back the purge
def test_13_mid_rewrite_failure_rolls_back_purge(writer):
    writer.add_journey("Op", "2", "outbound", "J2", {"mon"},
                       [{"naptan": "010A", "arrival": None, "departure": "09:00:00"},
                        {"naptan": "010B", "arrival": "09:10:00", "departure": None}], 2)
    writer.mark_dataset_loaded(2, "2026-01-01T00:00:00Z", "Op"); writer.commit()

    # The rewrite must reach writer.add_journey for the injected failure to fire
    # mid-write, so the zip carries a TransXChange with one real journey.
    server.httpx.AsyncClient = _ZipJourneyClient
    orig_add = server.writer.add_journey
    def _boom(*a, **k):
        raise RuntimeError("disk full")
    server.writer.add_journey = _boom
    try:
        try:
            asyncio.run(server._load_dataset(2, force_reload=True))
            raise AssertionError("expected RuntimeError")
        except RuntimeError:
            pass
    finally:
        server.writer.add_journey = orig_add
    # The next ensure_schema() is what would commit the orphaned purge
    # transaction; after a rollback it must find the dataset intact.
    server.writer.ensure_schema()
    assert 2 in writer.loaded_ids(), "purge not rolled back after mid-rewrite failure"
    assert writer.conn.execute("SELECT COUNT(*) FROM journeys WHERE ds_id=2").fetchone()[0] == 1, \
        "journeys lost to a partial purge"


# --- Test 20: watermark stays put when soft failures exceed the delta budget
# Task 1.1's core guarantee: failed downloads must not let the delta watermark
# advance past datasets that never refreshed. 3 soft failures (zip 404s) with
# 0 updated -> budget = max(2, 0) = 2 -> 3 > 2 -> watermark must NOT move.
def test_20_watermark_held_when_soft_failures_exceed_budget(writer):
    for _ds, _rt in ((2, "2"), (3, "3"), (4, "4")):
        writer.add_journey("Op", _rt, "outbound", f"J{_ds}", {"mon"},
                           [{"naptan": "010A", "arrival": None, "departure": "09:00:00"},
                            {"naptan": "010B", "arrival": "09:10:00", "departure": None}], _ds)
        writer.mark_dataset_loaded(_ds, "2026-01-01T00:00:00Z", "Op")
    writer.set_last_refresh("2026-01-01T00:00:00Z"); writer.commit()

    server.httpx.AsyncClient = _DeltaFailClient
    before = writer.last_refresh()
    asyncio.run(server._load_timetable_delta())
    assert writer.last_refresh() == before, \
        f"watermark advanced despite 3 errors > budget 2: {before} -> {writer.last_refresh()}"


# --- Test 21: a partial catalogue sweep must not purge loaded datasets
# A sweep that breaks on a non-200 page has only seen part of the catalogue;
# datasets missing from that partial view may still be live, so reconcile
# must not purge them (self-healing re-download would fix it, but it leaves
# the index incomplete during a force_refresh rebuild).
def test_21_partial_catalogue_sweep_does_not_purge(writer):
    writer.add_journey("Op", "2", "outbound", "J2", {"mon"},
                       [{"naptan": "010A", "arrival": None, "departure": "09:00:00"},
                        {"naptan": "010B", "arrival": "09:10:00", "departure": None}], 2)
    writer.mark_dataset_loaded(2, "2026-01-01T00:00:00Z", "Op"); writer.commit()

    server.httpx.AsyncClient = _PartialSweepClient
    asyncio.run(server.load_all_timetable_data(force_refresh=True))
    assert 2 in writer.loaded_ids(), "partial sweep purged a dataset still in the catalogue"


# --- Test 9: _sweep_catalogue paginates and returns ({id: entry}, complete)
def test_9_sweep_catalogue_pagination():
    server.httpx.AsyncClient = _PagedClient
    async def _sweep():
        async with server.httpx.AsyncClient() as c:
            return await server._sweep_catalogue(c)
    cat, complete = asyncio.run(_sweep())
    assert set(cat) == {1, 2} and complete, (cat, complete)


# --- Test 10: _format_failed truncates and formats
def test_10_format_failed_helper():
    assert server._format_failed([]) == ""
    assert server._format_failed([1, 2]) == " Failed dataset IDs: [1, 2] (details on stderr)."
    long = list(range(25))
    s = server._format_failed(long)
    assert "0, 1, 2" in s and "(+5 more)" in s, s


# --- Test 11: parse_transxchange handles namespaced and bare XML
def test_11_parse_transxchange_namespaced_and_bare():
    ns_xml = ('<TransXChange xmlns="http://www.transxchange.org.uk/">'
              '<StopPoints><AnnotatedStopPointRef><StopPointRef>010A</StopPointRef>'
              '<CommonName>Alpha</CommonName></AnnotatedStopPointRef></StopPoints>'
              '</TransXChange>')
    stops, routes, journeys = server.parse_transxchange(ns_xml, "Op")
    assert stops and stops[0].naptan == "010A", f"namespaced parse failed: {stops}"
    bare = "<TransXChange><StopPoints><AnnotatedStopPointRef><StopPointRef>010B</StopPointRef><CommonName>Beta</CommonName></AnnotatedStopPointRef></StopPoints></TransXChange>"
    stops, _, _ = server.parse_transxchange(bare, "Op")
    assert stops and stops[0].naptan == "010B", f"bare parse failed: {stops}"


# --- Test 22: the loader streams the zip to a temp file (get for meta)
class _StreamRecordingClient(_FakeClient):
    calls = []

    async def get(self, url):
        _StreamRecordingClient.calls.append(("get", url))
        if "/dataset/9/" in url:
            return _Resp(200, b'{"operatorName":"Op","url":"http://x/y.zip","modified":"2026-01-01T00:00:00Z"}')
        return _Resp(404, b"")

    def stream(self, method, url):
        assert method == "GET", f"unexpected stream method: {method}"
        _StreamRecordingClient.calls.append(("stream", url))
        return _Resp(200, _make_zip(b"<TransXChange/>"))


def test_22_zip_download_streams(writer):
    _StreamRecordingClient.calls = []

    async def run():
        async with _StreamRecordingClient() as client:
            return await server._load_dataset(9, client=client)

    result = asyncio.run(run())
    assert result is not None, "dataset did not load"
    kinds = [c[0] for c in _StreamRecordingClient.calls]
    _check(lambda: "stream" in kinds, "the zip must be streamed, not fetched")
    _check(lambda: "get" in kinds, "the catalogue metadata must be fetched via get")
    stream_urls = [c[1] for c in _StreamRecordingClient.calls if c[0] == "stream"]
    _check(lambda: bool(stream_urls) and "http://x/y.zip" in stream_urls[0],
           "the zip must come from the catalogue's download URL")


# --- Test 23: remote TransXChange XML is parsed entity-safe (defusedxml)
# stdlib ElementTree *expands* an internal entity; defusedxml refuses the
# document outright. The observable difference is whether the stop parses.
ENTITY_DOC = (
    '<?xml version="1.0"?>'
    '<!DOCTYPE TransXChange [<!ENTITY nm "Alpha Street">]>'
    '<TransXChange xmlns="http://www.transxchange.org.uk/">'
    '<StopPoints><AnnotatedStopPointRef>'
    '<StopPointRef>010A</StopPointRef><CommonName>&nm;</CommonName>'
    '</AnnotatedStopPointRef></StopPoints></TransXChange>')


def test_23_transxchange_entities_are_refused():
    stops, routes, journeys = server.parse_transxchange(ENTITY_DOC, "OpX")
    assert (stops, routes, journeys) == ([], [], []), (
        "an entity-bearing document must be refused, not expanded")


# --- Test 24/25: the API key never reaches output or the log stream (ruling R16)
# httpx builds HTTPStatusError messages from the request URL, so an unredacted
# failure publishes the credential into tool output and the session transcript.
#
# Ruling R24: the key MUST contain characters the two quote() variants treat
# differently, or these tests cannot see the gap they exist to catch. The
# package builds its api_key query parameter three different ways:
#   fares tool  f"...?api_key={API_KEY}"                 raw, unquoted
#   SIRI-SX     f"...?api_key={quote(API_KEY)}"          quote(), safe='/'
#   SIRI-VM     urlencode(params, quote_via=quote)        quote(), safe=''
# urlencode passes safe='' to quote, so a key containing '/' is encoded as
# %2F on the SIRI-VM path but left as '/' on the SIRI-SX path. An
# alphanumeric key encodes identically everywhere and hides that entirely.
# DUMMY_KEY is a fabricated credential used nowhere; it is deliberately
# awkward, not realistic.
DUMMY_KEY = "DUMMY/KEY+abc=123"


def _leaked(text: str) -> list:
    """Every encoding of DUMMY_KEY that appears in `text` (empty list = clean).

    Asserting only `DUMMY_KEY not in text` is VACUOUS for this key: the
    SIRI-VM URL carries quote(DUMMY_KEY, safe=''), which does not contain
    DUMMY_KEY as a substring, so that assertion passes even with no
    redaction at all. Check every form the builders can produce.
    """
    forms = {DUMMY_KEY, quote(DUMMY_KEY), quote(DUMMY_KEY, safe="")}
    return sorted(f for f in forms if f in text)


def test_24_api_key_is_redacted_from_tool_output(monkeypatch, seeded_route):
    monkeypatch.setattr(server, "API_KEY", DUMMY_KEY)
    server.set_http_client(httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(401, json={}))))
    server._LIVE_CACHE = server.TTLCache(20.0)
    try:
        out = asyncio.run(server.estimate_live_eta(
            "Charlie Road", "OPX", "12", day="mon"))
    finally:
        server.set_http_client(None)
        server._LIVE_CACHE = server.TTLCache(20.0)
    _assert_no_key(lambda: out, "estimate_live_eta output")
    _check(lambda: "401" in out, "the failure must still be reported readably")


def test_25_api_key_is_redacted_from_siri_sx_log(monkeypatch, capsys):
    monkeypatch.setattr(server, "API_KEY", DUMMY_KEY)
    server.set_http_client(httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(403, json={}))))
    server._SX_CACHE = server.TTLCache(60.0)
    try:
        assert asyncio.run(server._fetch_sx("disruptions")) is None
    finally:
        server.set_http_client(None)
        server._SX_CACHE = server.TTLCache(60.0)
    err = capsys.readouterr().err
    _assert_no_key(lambda: err, "the siri-sx log stream")
    _check(lambda: "403" in err, "the failure must still be logged readably")


# --- Test 26: the fares tool's error return is redacted too (ruling R22)
# R22 found this site only after Task 6 added the fetch path, which is exactly the
# staleness the sweep in this task's brief exists to prevent. It has no module cache
# to reset -- get_fare_prices caches nothing -- so nothing is reset here.
def test_26_api_key_is_redacted_from_fares_tool(monkeypatch):
    monkeypatch.setattr(server, "API_KEY", DUMMY_KEY)
    server.set_http_client(httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(401, json={}))))
    try:
        out = asyncio.run(server.get_fare_prices("DS1"))
    finally:
        server.set_http_client(None)
    _assert_no_key(lambda: out, "get_fare_prices output")
    _check(lambda: "401" in out, "the failure must still be reported readably")


# --- Test 27: the passthrough handler's SUCCESS returns are redacted too (P31)
# Its sibling error returns (the HTTPStatusError branch and the generic one)
# already pass through _redact; P31's point is the invariant that EVERY return
# from that handler does, so no future reader has to re-derive reachability on
# this surface. The leak it prevents needs a remote endpoint to echo the
# request URL -- key included -- into its own 200 body, which is what this test
# puts in the handler; it is not asserted that any real endpoint does so.
def _echo_url_client(response_for):
    def handler(request):
        return response_for(str(request.url))
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_27_api_key_is_redacted_from_passthrough_success(monkeypatch):
    monkeypatch.setattr(server, "API_KEY", DUMMY_KEY)
    tool_fn = _dataset_tool()
    assert tool_fn is not None, "generated dataset tool not found"

    # JSON branch: the echoed URL arrives in a JSON body -> resp.json() path.
    server.set_http_client(_echo_url_client(
        lambda url: httpx.Response(200, json={"echo": url})))
    try:
        out = asyncio.run(tool_fn(datasetID="12"))
    finally:
        server.set_http_client(None)
    _assert_no_key(lambda: out, "the passthrough success output")
    _check(lambda: "echo" in out, "the successful response must still be returned")

    # Non-JSON branch: resp.json() raises, so the handler returns resp.text.
    server.set_http_client(_echo_url_client(
        lambda url: httpx.Response(200, content=url.encode())))
    try:
        out = asyncio.run(tool_fn(datasetID="12"))
    finally:
        server.set_http_client(None)
    _assert_no_key(lambda: out, "the passthrough success output")
    _check(lambda: "api/v1/dataset" in out,
           "the successful response must still be returned")


# --- Test 28: every success path carrying remote-derived content is redacted
# (rulings R29/R32). P31 closed one instance of this class; this pins the
# property instead of the lines -- five tools, five mocked remote documents,
# each echoing the request URL (key included) into a field its parser hands
# back to the caller.
#
# R32 added the two SIRI-SX tools, which the R29 sweep missed. That sweep
# grepped for existing _redact call sites, and that grep is structurally blind
# to a function that calls _redact nowhere. The list below now comes from the
# enumeration of remote-returning functions recorded in the wave report, not
# from a grep.
#
# The leak check takes a THUNK, not the body, on purpose: pytest renders a
# failing frame's arguments, so passing the body itself (or asserting on
# _leaked(body)) publishes the very credential this test exists to keep out of
# output -- measured, not assumed. For the same reason every check below is an
# inline `raise`, never an `assert`: pytest renders an assert's operands, and on
# the unfixed code this body carries the key. The messages report how many
# encodings leaked, never which.
def _assert_success_path_is_clean(get_body, marker: str, tool: str) -> None:
    body = get_body()  # a local, not an argument: locals are not rendered
    if marker not in body:
        raise AssertionError(f"{tool} did not return the parsed remote content")
    _assert_no_key(lambda: body, f"{tool} output")
    if "api_key=***" not in body:
        raise AssertionError(f"{tool} output kept no redacted URL to show")


def _vm_echo_client():
    """Mock SIRI-VM feed whose VehicleRef is the request URL it was asked for.

    The URL's '&' separators must be XML-escaped or the document is malformed;
    the parser hands the unescaped text straight into the vehicle dict."""
    def handler(request):
        ref = xml_escape(str(request.url))
        return httpx.Response(200, content=(
            '<?xml version="1.0"?>'
            '<Siri xmlns="http://www.siri.org.uk/siri"><VehicleActivity>'
            '<MonitoredVehicleJourney>'
            f'<VehicleRef>{ref}</VehicleRef>'
            '<DirectionRef>outbound</DirectionRef>'
            # near seeded_route's 010B (51.51, -0.11), so estimate_live_eta
            # matches this vehicle instead of taking the no-match branch
            '<VehicleLocation><Latitude>51.511</Latitude>'
            '<Longitude>-0.111</Longitude></VehicleLocation>'
            '</MonitoredVehicleJourney></VehicleActivity></Siri>').encode())
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _fares_echo_handler(request):
    """Catalogue metadata, then a NeTEx document whose zone ref echoes the
    download request URL (the fares download URL is the one built with the
    raw, unquoted key)."""
    url = str(request.url)
    if "/api/v1/fares/dataset/" in url:
        return httpx.Response(200, json={"url": "https://example.test/dl"})
    ref = xml_escape(url)
    return httpx.Response(200, content=(
        '<?xml version="1.0"?>'
        '<PublicationDelivery xmlns="http://www.netex.org.uk/netex">'
        '<dataObjects><CompositeFrame><FaresFrame>'
        '<FareFrame version="1.0" id="FF">'
        '<fareStructureElements>'
        '<FareStructureElement version="1.0" id="FSE">'
        '<distanceMatrixElements>'
        '<DistanceMatrixElement version="1.0" id="Z1+Z2">'
        f'<StartTariffZoneRef version="1.0" ref="{ref}"/>'
        '<GeographicalIntervalPrice version="1.0" id="p">'
        '<Amount>1.20</Amount>'
        '</GeographicalIntervalPrice>'
        '</DistanceMatrixElement>'
        '</distanceMatrixElements>'
        '</FareStructureElement>'
        '</fareStructureElements>'
        '</FareFrame>'
        '</FaresFrame></CompositeFrame></dataObjects>'
        '</PublicationDelivery>').encode())


# The SIRI-SX phases carry a sentinel next to the echoed URL rather than a
# field-name marker. A field name is not enough there: json.dumps renders
# {"summary": null} and {"reason": null}, so a phase whose message parsed but
# carried no text would still match "summary"/"reason" and pass the leak check
# for the wrong reason.
ECHO_SENTINEL = "ECHO-SENTINEL"

_SX_KIND = {"get_disruptions": "disruptions",
            "get_cancellations": "cancellations"}

# One vacuity marker per phase: the parsed-content tell that the remote
# document reached the caller at all.
_MARKER = {"get_live_buses_on_route": "vehicle_id",
           "estimate_live_eta": '"vehicles"',
           "get_fare_prices": '"start_zones"',
           "get_disruptions": ECHO_SENTINEL,
           "get_cancellations": ECHO_SENTINEL}


def _sx_echo_client(kind):
    """Mock SIRI-SX feed echoing the request URL into the field its parser
    carries through. Not the same field for both: parse_siri_sx reads a
    message's <Summary> (falling back to <Description>), while
    parse_cancellations reads a cancelled-vehicle entry's <CancellationReason>
    (falling back to <Reason>). Each document is shaped so its own parser
    produces an entry -- the cancellations traversal only fires on a
    CANCELLATION_TAGS element."""
    def handler(request):
        url = xml_escape(str(request.url))
        if kind == "disruptions":
            doc = ('<?xml version="1.0"?>'
                   '<Siri xmlns="http://www.siri.org.uk/siri"><InfoMessage>'
                   f'<Summary>{ECHO_SENTINEL} {url}</Summary>'
                   '<OperatorRef>OPX</OperatorRef>'
                   '</InfoMessage></Siri>')
        else:
            doc = ('<?xml version="1.0"?>'
                   '<Siri xmlns="http://www.siri.org.uk/siri">'
                   '<EstimatedVehicleJourneyCancellations>'
                   '<OperatorRef>OPX</OperatorRef><LineRef>12</LineRef>'
                   f'<CancellationReason>{ECHO_SENTINEL} {url}'
                   '</CancellationReason>'
                   '</EstimatedVehicleJourneyCancellations></Siri>')
        return httpx.Response(200, content=doc.encode())
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("tool", ["get_live_buses_on_route", "estimate_live_eta",
                                  "get_fare_prices", "get_disruptions",
                                  "get_cancellations"])
def test_28_remote_echo_is_redacted_from_every_success_path(
        tool, monkeypatch, seeded_route):
    """A mocked remote document echoes the request URL (key included) into a
    field each tool's parser carries to the caller; the credential must not
    surface. Parametrized so a regression names the tool that leaked."""
    monkeypatch.setattr(server, "API_KEY", DUMMY_KEY)

    if tool in _SX_KIND:
        # _fetch_sx caches the parsed feed per kind for 60s, so an entry left
        # by another test would have this phase asserting on that test's
        # document instead of the echo.
        server.set_http_client(_sx_echo_client(_SX_KIND[tool]))
        server._SX_CACHE = server.TTLCache(60.0)
        try:
            body = asyncio.run(getattr(server, tool)())
        finally:
            server.set_http_client(None)
            server._SX_CACHE = server.TTLCache(60.0)
    elif tool == "get_fare_prices":
        server.set_http_client(httpx.AsyncClient(
            transport=httpx.MockTransport(_fares_echo_handler)))
        try:
            body = asyncio.run(server.get_fare_prices("DS1"))
        finally:
            server.set_http_client(None)
    else:
        server.set_http_client(_vm_echo_client())
        server._LIVE_CACHE = server.TTLCache(20.0)
        try:
            if tool == "get_live_buses_on_route":
                body = asyncio.run(server.get_live_buses_on_route("OPX", "12"))
            else:
                body = asyncio.run(server.estimate_live_eta(
                    "Charlie Road", "OPX", "12", day="mon"))
        finally:
            server.set_http_client(None)
            server._LIVE_CACHE = server.TTLCache(20.0)

    _assert_success_path_is_clean(lambda: body, _MARKER[tool], tool)


