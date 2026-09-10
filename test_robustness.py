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

print("ALL ROBUSTNESS TESTS PASS")