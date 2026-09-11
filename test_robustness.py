import asyncio
import io
import json
import zipfile

import pytest

import openbusdata_mcp.server as server


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
    """Each test swaps server.httpx.AsyncClient for a fake; restore the real one."""
    orig = server.httpx.AsyncClient
    yield
    server.httpx.AsyncClient = orig


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
    assert len(captured) == 1, captured
    url = captured[0]
    # '?', '#' and any stray '/' must be percent-encoded so the path survives
    assert "/api/v1/dataset/12%3Fq%3D1%23frag" in url, url
    assert "?" not in url.split("api_key")[0].split("/api/v1/dataset/", 1)[1], \
        f"param broke out of the path: {url}"


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
    result = asyncio.run(server.plan_journey("A St", "B St", "09:30", "mon", 0))
    plans = json.loads(result)
    assert len(plans) == 2, f"expected 2 distinct plans, got {len(plans)}"
    assert {p["legs"][0]["operator"] for p in plans} == {"OpOne", "OpTwo"}, \
        "dedup collapsed distinct operators"


# --- Test 6: live-buses URL params are percent-encoded
def test_6_live_buses_params_url_encoded():
    captured.clear()
    server.httpx.AsyncClient = _FakeClient
    result = asyncio.run(server.get_live_buses_on_route("A&B", "1 2"))
    assert "operatorRef=A%26B" in captured[0], captured[0]
    assert "lineRef=1%202" in captured[0], captured[0]


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
    assert "stream" in kinds, f"zip not streamed: {_StreamRecordingClient.calls}"
    assert "get" in kinds, f"meta not fetched via get: {_StreamRecordingClient.calls}"
    stream_urls = [c[1] for c in _StreamRecordingClient.calls if c[0] == "stream"]
    assert stream_urls and "http://x/y.zip" in stream_urls[0], stream_urls
