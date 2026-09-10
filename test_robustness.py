import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

# Redirect HOME (cache dir) and the store DB to a temp dir BEFORE importing
# the server, so startup touches nothing real.
os.environ["HOME"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import openbusdata_mcp.store as store_mod  # noqa: E402
store_mod.DB_PATH = Path(os.environ["HOME"]) / "index.db"

import openbusdata_mcp.server as server  # noqa: E402
from openbusdata_mcp.store import TimetableWriter  # noqa: E402


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


captured = []


class _FakeClient:
    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        captured.append(url)
        return _FakeResp()


server.httpx.AsyncClient = _FakeClient

tool_fn = None
for t in server.mcp._tool_manager.list_tools():
    if t.name == "timetables_api_v1_dataset_by_datasetID":
        tool_fn = t.fn
assert tool_fn is not None, "generated dataset tool not found"

result = asyncio.run(tool_fn(datasetID="12?q=1#frag"))
assert result == "{}", f"unexpected tool result: {result}"
assert len(captured) == 1, captured
url = captured[0]
# '?', '#' and any stray '/' must be percent-encoded so the path survives
assert "/api/v1/dataset/12%3Fq%3D1%23frag" in url, url
assert "?" not in url.split("api_key")[0].split("/api/v1/dataset/", 1)[1], \
    f"param broke out of the path: {url}"
print("1. path params URL-encoded: OK")

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


server.httpx.AsyncClient = _FailingClient
captured.clear()
result = asyncio.run(tool_fn(datasetID="5"))
assert result.startswith("HTTP Error 500: "), result[:60]
assert len(result) < 600, f"remote error body not truncated ({len(result)} chars)"
assert "E" * 600 not in result, "error body leaked in full"
print("2. remote error bodies truncated: OK")

# --- Test 3: legacy dataset_meta promotion survives junk keys
writer = TimetableWriter()
writer.ensure_schema()
writer.conn.execute(
    "INSERT INTO meta VALUES ('dataset_meta', ?)",
    (json.dumps({"42": {"modified": "2026-01-01T00:00:00Z", "operator": "Op"},
                 "not-a-number": {"modified": "x", "operator": "Op"},
                 "7": "junk-not-a-dict"}),))
writer.conn.commit()
writer.ensure_schema()  # re-run: must promote 42, skip junk, not crash
assert writer.loaded_ids() == {42, 7} or 7 not in writer.loaded_ids(), \
    f"promotion produced garbage: {writer.loaded_ids()}"
assert 42 in writer.loaded_ids(), "valid legacy dataset lost"
row = writer.conn.execute("SELECT k FROM meta WHERE k='dataset_meta'").fetchone()
assert row is None, "legacy meta key not consumed"
print("3. legacy meta promotion guards junk keys: OK")

# --- Test 4: soft failures are counted as errors, not loaded
import io, zipfile

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

class _ZipFailClient(_FakeClient):
    """meta OK, zip download 404s -> _load_dataset must return None."""
    async def get(self, url):
        if "/dataset/1/" in url:
            return _Resp(200, b'{"operatorName":"Op","url":"http://x/y.zip","modified":"2026-01-01T00:00:00Z"}')
        return _Resp(404, b"")

server.httpx.AsyncClient = _ZipFailClient
assert asyncio.run(server._load_dataset(1)) is None, "404 zip not reported as failure"

class _ZipOkClient(_FakeClient):
    async def get(self, url):
        if "/dataset/2/" in url:
            return _Resp(200, b'{"operatorName":"Op","url":"http://x/y.zip","modified":"2026-01-01T00:00:00Z"}')
        return _Resp(200, _make_zip(b"<TransXChange/>"))

server.httpx.AsyncClient = _ZipOkClient
assert asyncio.run(server._load_dataset(2)) is not None, "good zip not reported as success"
print("4. soft failures counted as errors: OK")

# --- Test 5: plan_journey dedup keeps distinct operators on the same route
# Seed two journeys: same route number + depart, different operators.
w = TimetableWriter()
w.ensure_schema()
w.add_stop("010A", "A St"); w.add_stop("010B", "B St")
w.add_journey("OpOne", "1", "outbound", "J1", {"mon"},
              [{"naptan": "010A", "arrival": None, "departure": "09:00:00"},
               {"naptan": "010B", "arrival": "09:10:00", "departure": None}], 1)
w.add_journey("OpTwo", "1", "outbound", "J2", {"mon"},
              [{"naptan": "010A", "arrival": None, "departure": "09:00:00"},
               {"naptan": "010B", "arrival": "09:12:00", "departure": None}], 2)
w.commit()
result = asyncio.run(server.plan_journey("A St", "B St", "09:30", "mon", 0))
plans = json.loads(result)
assert len(plans) == 2, f"expected 2 distinct plans, got {len(plans)}"
assert {p["legs"][0]["operator"] for p in plans} == {"OpOne", "OpTwo"}, \
    "dedup collapsed distinct operators"
print("5. plan dedup keeps distinct operators: OK")

# --- Test 6: live-buses URL params are percent-encoded
captured.clear()
server.httpx.AsyncClient = _FakeClient
result = asyncio.run(server.get_live_buses_on_route("A&B", "1 2"))
assert "operatorRef=A%26B" in captured[0], captured[0]
assert "lineRef=1%202" in captured[0], captured[0]
print("6. live-buses params URL-encoded: OK")

# --- Test 12: force_refresh purges datasets withdrawn from the catalogue
class _CatClient(_FakeClient):
    async def get(self, url):
        if "/dataset/?" in url:
            return _Resp(200, b'{"results":[{"id":1}]}')
        return _Resp(200, b'{"operatorName":"Op","url":"http://x/y.zip","modified":"2026-01-01T00:00:00Z"}')

w = TimetableWriter(); w.ensure_schema()
w.mark_dataset_loaded(2, "2026-01-01T00:00:00Z", "Op"); w.commit()
server.httpx.AsyncClient = _CatClient
asyncio.run(server.load_all_timetable_data(force_refresh=True))
assert 2 not in w.loaded_ids(), "withdrawn dataset not purged on force_refresh"
print("12. force_refresh reconciles withdrawn datasets: OK")

# --- Test 13: mid-rewrite failure rolls back the purge
w = TimetableWriter(); w.ensure_schema()
w.add_journey("Op", "2", "outbound", "J2", {"mon"},
              [{"naptan": "010A", "arrival": None, "departure": "09:00:00"},
               {"naptan": "010B", "arrival": "09:10:00", "departure": None}], 2)
w.mark_dataset_loaded(2, "2026-01-01T00:00:00Z", "Op"); w.commit()

# The rewrite must reach writer.add_journey for the injected failure to fire
# mid-write, so the zip carries a TransXChange with one real journey.
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
        return _Resp(200, _make_zip(_JOURNEY_XML))

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
assert 2 in w.loaded_ids(), "purge not rolled back after mid-rewrite failure"
assert w.conn.execute("SELECT COUNT(*) FROM journeys WHERE ds_id=2").fetchone()[0] == 1, \
    "journeys lost to a partial purge"
print("13. mid-rewrite failure rolls back purge: OK")

# --- Test 21: a partial catalogue sweep must not purge loaded datasets
# A sweep that breaks on a non-200 page has only seen part of the catalogue;
# datasets missing from that partial view may still be live, so reconcile
# must not purge them (self-healing re-download would fix it, but it leaves
# the index incomplete during a force_refresh rebuild).
w = TimetableWriter(); w.ensure_schema()
w.add_journey("Op", "2", "outbound", "J2", {"mon"},
              [{"naptan": "010A", "arrival": None, "departure": "09:00:00"},
               {"naptan": "010B", "arrival": "09:10:00", "departure": None}], 2)
w.mark_dataset_loaded(2, "2026-01-01T00:00:00Z", "Op"); w.commit()

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

server.httpx.AsyncClient = _PartialSweepClient
asyncio.run(server.load_all_timetable_data(force_refresh=True))
assert 2 in w.loaded_ids(), "partial sweep purged a dataset still in the catalogue"
print("21. partial catalogue sweep does not purge loaded datasets: OK")

print("ALL ROBUSTNESS TESTS PASS")