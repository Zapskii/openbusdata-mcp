# Performance & Minimal Remote Calls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut local query cost (no JSON-blob reads on the query path, SQL-only planning) and reduce remote BODS API calls (pooled client, live-response TTL cache, server-side delta sweeps) without changing any tool's JSON output contract.

**Architecture:** Phase A wraps the HTTP layer (shared pooled `httpx.AsyncClient`, 20s TTL cache on live datafeed, `modifiedDate` filter on catalogue sweeps with a 7-day full-reconcile cadence). Phase B makes the SQLite schema fully relational (`days_mask` bitmask column; `journey_stop_times` gains `seq`+`arr`) so candidate pruning happens in SQL. Phase C rewrites the one-change planner as a single indexed SQL self-join and deletes the now-dead JSON-fetch/Python-loop helpers.

**Tech Stack:** Python ≥ 3.10, httpx, mcp (FastMCP), stdlib sqlite3 (FTS5). pytest at repo root.

**Spec:** `docs/superpowers/specs/2026-09-12-perf-and-remote-calls-design.md`

## Global Constraints

- Python ≥ 3.10. Runtime deps stay exactly `httpx`, `mcp>=1.12,<2`, `pyyaml` — no new dependencies.
- SQLite pre-3.32 variable limit is 999: the two-set queries in this plan bind at most 400 + 400 + 2 = 802 parameters. Never build `IN (...)` clauses larger than that; chunk past it.
- Times are zero-padded `'HH:MM:SS'` strings; hours may exceed 24 (TransXChange post-midnight services). Compare as strings; never clamp or wrap hours.
- From Phase B on, the query path must never parse `journeys.json` (the JSON blob becomes write-only legacy).
- Tool JSON output contracts are frozen: `search_stops`, `find_routes_between_stops`, `get_route_stops`, `find_buses_by_arrival_time`, `plan_journey`, `get_live_buses_on_route` keep their current shapes.
- All legacy-DB upgrades are one-time `ensure_schema` backfills keyed on `meta` flags, following the existing pattern in `store.py`.
- Tests: `pytest` from repo root. `conftest.py` redirects `HOME` and provides `writer`/`store` fixtures over a fresh temp DB per test. Test files are root-level `test_*.py`.
- Commits: conventional-commit style (`feat:`, `perf:`, `test:`, `refactor:`) ending with `Co-Authored-By: Claude Code <noreply@anthropic.com>`.
- Repo workflow: work in a worktree created via superpowers:using-git-worktrees; push to the `Zapskii` fork's main and PR upstream (`AndrewAubury/openbusdata-mcp`) per the established integration pattern.

---

## Phase A — remote-call hygiene

### Task 1: Shared pooled httpx client

**Files:**
- Modify: `src/openbusdata_mcp/server.py` (config block ~line 43; `make_tool` inner function ~line 683; `get_live_buses_on_route` ~line 890)
- Create: `test_http.py`
- Modify: `test_robustness.py` (test 6, ~line 315)

**Interfaces:**
- Consumes: nothing new.
- Produces: `server.get_http_client() -> httpx.AsyncClient` (lazy module-level singleton, timeout 30s, follow_redirects); `server.set_http_client(client: Optional[httpx.AsyncClient]) -> None` (test seam; `None` resets to lazy). Spec tools and `get_live_buses_on_route` call `get_http_client()` instead of constructing clients. Loaders are untouched.

- [ ] **Step 1: Write the failing tests**

Create `test_http.py`:

```python
# test_http.py
import asyncio
import httpx
import pytest

import openbusdata_mcp.server as server


def _client():
    counts = {"n": 0}

    def counting_handler(request):
        counts["n"] += 1
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(counting_handler))
    return client, counts


def test_spec_tool_reuses_shared_client():
    client, counts = _client()
    server.set_http_client(client)
    try:
        # Build a minimal fake spec tool the same way register_tools_from_specs
        # does, then call it twice: the shared client must serve both requests.
        async def probe_tool(**kwargs):
            c = server.get_http_client()
            resp = await c.get("https://example.test/api/v1/probe")
            return resp.text
        asyncio.run(probe_tool())
        asyncio.run(probe_tool())
        assert counts["n"] == 2
    finally:
        server.set_http_client(None)


def test_get_http_client_lazy_singleton():
    server.set_http_client(None)
    try:
        c1 = server.get_http_client()
        c2 = server.get_http_client()
        assert c1 is c2, "shared client must be a singleton"
    finally:
        server.set_http_client(None)
```

In `test_robustness.py`, replace test 6 (which patches `server.httpx.AsyncClient` — the live tool will no longer construct clients) with a shared-client fake:

```python
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
    server.set_http_client(_FakeSharedClient())
    try:
        result = asyncio.run(server.get_live_buses_on_route("A&B", "1 2"))
    finally:
        server.set_http_client(None)
    assert "operatorRef=A%26B" in captured[0], captured[0]
    assert "lineRef=1%202" in captured[0], captured[0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest test_http.py test_robustness.py::test_6_live_buses_params_url_encoded -v`
Expected: FAIL — `server` has no attribute `set_http_client` (AttributeError).

- [ ] **Step 3: Implement**

In `server.py`, after the `CACHE_DIR` block (~line 43), add:

```python
# Shared pooled HTTP client: one TCP/TLS connection pool for the process
# lifetime instead of a fresh client (new handshake) per tool call. Test seam:
# set_http_client() injects a client (or None to reset to lazy creation).
_http_client: Optional[httpx.AsyncClient] = None


def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    return _http_client


def set_http_client(client: Optional[httpx.AsyncClient]) -> None:
    global _http_client
    _http_client = client
```

In `register_tools_from_specs`'s `tool_func` body, replace:

```python
                        try:
                            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                                resp = await client.get(full_url)
```

with:

```python
                        try:
                            client = get_http_client()
                            resp = await client.get(full_url)
```

(keep the rest of the try/except unchanged; dedent its body one level).

In `get_live_buses_on_route`, replace the `async with httpx.AsyncClient(...)` block with:

```python
    client = get_http_client()
    try:
        resp = await client.get(full_url)
        resp.raise_for_status()
        # ... existing XML parsing body unchanged, dedented out of the old with-block ...
    except Exception as e:
        return f"Error: {type(e).__name__}: {str(e)}"
```

Do NOT touch `load_all_timetable_data`, `_load_dataset`, or `_load_timetable_delta` — bulk loaders keep their per-run clients.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest test_http.py test_robustness.py -v`
Expected: PASS (all of test_robustness, including the rewritten test 6).

- [ ] **Step 5: Run the full suite**

Run: `pytest`
Expected: PASS — loaders untouched, so no other test is affected.

- [ ] **Step 6: Commit**

```bash
git add src/openbusdata_mcp/server.py test_http.py test_robustness.py
git commit -m "perf: shared pooled httpx client for live and spec-generated tools

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 2: TTL cache on live datafeed responses

**Files:**
- Modify: `src/openbusdata_mcp/server.py` (new `TTLCache` class near the config block; `get_live_buses_on_route`)
- Modify: `test_http.py`

**Interfaces:**
- Consumes: `server.get_http_client()`, `server.set_http_client()` (Task 1).
- Produces: `server.TTLCache(ttl_seconds: float)` with `get(key) -> Optional[value]` and `put(key, value) -> None` (monotonic clock, ≤256 entries, clears on overflow); module-level `server._LIVE_CACHE = TTLCache(20.0)` consulted by `get_live_buses_on_route` keyed on `(operator_ref, line_ref)`, caching the parsed bus list.

- [ ] **Step 1: Write the failing tests**

Append to `test_http.py`:

```python
import time


def test_ttl_cache_hit_then_expiry(monkeypatch):
    from openbusdata_mcp.server import TTLCache
    cache = TTLCache(ttl_seconds=20.0)
    cache.put("k", {"a": 1})
    assert cache.get("k") == {"a": 1}
    fake_now = time.monotonic() + 21.0
    monkeypatch.setattr(time, "monotonic", lambda: fake_now)
    assert cache.get("k") is None, "expired entry must not be served"


def test_live_buses_ttl_cache_skips_second_request():
    sirii = b'<?xml version="1.0"?><Siri><VehicleActivity>' \
            b'<MonitoredVehicleJourney><VehicleRef>V1</VehicleRef>' \
            b'</MonitoredVehicleJourney></VehicleActivity></Siri>'

    def xml_handler(request):
        counts["n"] += 1
        return httpx.Response(200, content=sirii)

    client, counts = _client()  # replaced below; reuse the counting dict
    client = httpx.AsyncClient(transport=httpx.MockTransport(xml_handler))
    server.set_http_client(client)
    server._LIVE_CACHE = server.TTLCache(20.0)
    try:
        r1 = asyncio.run(server.get_live_buses_on_route("OPX", "12"))
        r2 = asyncio.run(server.get_live_buses_on_route("OPX", "12"))
        assert counts["n"] == 1, "second call within TTL must be served from cache"
        assert r1 == r2
        r3 = asyncio.run(server.get_live_buses_on_route("OPY", "12"))
        assert counts["n"] == 2, "a different route key must miss the cache"
    finally:
        server.set_http_client(None)
        server._LIVE_CACHE = server.TTLCache(20.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest test_http.py -v`
Expected: FAIL — `TTLCache` not defined (ImportError/AttributeError); the live-cache test fails on `counts["n"] == 2`.

- [ ] **Step 3: Implement**

In `server.py`, near the shared-client block:

```python
import time

class TTLCache:
    """Tiny process-local TTL cache: monotonic clock, bounded entry count."""

    def __init__(self, ttl_seconds: float):
        self.ttl = ttl_seconds
        self._store: dict = {}

    def get(self, key):
        hit = self._store.get(key)
        if hit is None:
            return None
        ts, val = hit
        if time.monotonic() - ts > self.ttl:
            del self._store[key]
            return None
        return val

    def put(self, key, val):
        if len(self._store) >= 256:
            self._store.clear()  # simple bounded reset; keys are few
        self._store[key] = (time.monotonic(), val)


_LIVE_CACHE = TTLCache(20.0)  # bus positions are re-polled within seconds
```

In `get_live_buses_on_route`, after building `full_url` and before the request:

```python
    cached = _LIVE_CACHE.get((operator_ref, line_ref))
    if cached is not None:
        return json.dumps(cached, indent=2, ensure_ascii=False)
```

and just before the success `return json.dumps(buses, ...)` line:

```python
        _LIVE_CACHE.put((operator_ref, line_ref), buses)
```

Note: cache only successful parses; error-path returns bypass the cache.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest test_http.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite and commit**

Run: `pytest`
Expected: PASS.

```bash
git add src/openbusdata_mcp/server.py test_http.py
git commit -m "perf: 20s TTL cache on live bus datafeed responses

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 3: Server-side modifiedDate filter on delta sweeps

**Files:**
- Modify: `src/openbusdata_mcp/server.py` (`_sweep_catalogue` ~line 404; `_load_timetable_delta` ~line 509)
- Modify: `src/openbusdata_mcp/store.py` (`TimetableWriter`: new `last_full_sweep` / `set_last_full_sweep` / `full_sweep_due` helpers next to `last_refresh` ~line 570)
- Create: `test_sweep.py`

**Interfaces:**
- Consumes: `TimetableWriter` meta table (existing).
- Produces:
  - `_sweep_catalogue(client, modified_since: Optional[str] = None) -> tuple[dict[int, dict], bool]` — adds `modifiedDate=<iso>` (urlencoded) when `modified_since` is given.
  - `TimetableWriter.set_last_full_sweep(iso: str)`, `TimetableWriter.full_sweep_due(max_age_days: int = 7) -> bool` (meta key `last_full_sweep`; absent/unparseable → due).
  - `_load_timetable_delta` sweep policy: `full = reconcile or writer.full_sweep_due()`; filtered sweep passes `reference.isoformat()`; purge runs only on full sweeps; full sweeps write `set_last_full_sweep`.

- [ ] **Step 1: Write the failing tests**

Create `test_sweep.py`:

```python
# test_sweep.py
import asyncio
from datetime import datetime, timedelta, timezone

import httpx

import openbusdata_mcp.server as server

_NOW = datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _sweep_client(captured):
    def handler(request):
        captured.append(str(request.url))
        return httpx.Response(200, json={"results": []})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_sweep_catalogue_passes_modified_date():
    captured = []
    async def _run():
        async with _sweep_client(captured) as client:
            return await server._sweep_catalogue(
                client, modified_since="2026-09-01T00:00:00+00:00")
    catalog, complete = asyncio.run(_run())
    assert complete and catalog == {}
    assert any("modifiedDate=2026-09-01" in u for u in captured), captured


def test_sweep_catalogue_without_filter_has_no_modified_date():
    captured = []
    async def _run():
        async with _sweep_client(captured) as client:
            return await server._sweep_catalogue(client)
    asyncio.run(_run())
    assert captured and all("modifiedDate" not in u for u in captured), captured


def test_full_sweep_due_default_and_watermark(writer):
    assert writer.full_sweep_due() is True, "no watermark recorded -> due"
    writer.set_last_full_sweep(_iso(_NOW - timedelta(hours=1)))
    assert writer.full_sweep_due() is False
    writer.set_last_full_sweep(_iso(_NOW - timedelta(days=8)))
    assert writer.full_sweep_due() is True, "stale watermark -> due"


def test_delta_uses_filtered_sweep_when_fresh(monkeypatch, writer):
    writer.mark_dataset_loaded(1, None, "OpA")
    writer.set_last_refresh(_iso(_NOW - timedelta(days=2)))
    writer.set_last_full_sweep(_iso(_NOW - timedelta(hours=1)))
    writer.commit()
    seen = {}

    async def fake_sweep(client, modified_since=None):
        seen["modified_since"] = modified_since
        return {99: {"id": 99, "modified": _iso(_NOW - timedelta(days=1))}}, True

    async def fake_load(ds_id, force_reload=False, client=None):
        seen["loaded"] = seen.get("loaded", []) + [ds_id]
        return {}

    monkeypatch.setattr(server, "_sweep_catalogue", fake_sweep)
    monkeypatch.setattr(server, "_load_dataset", fake_load)
    result = asyncio.run(server._load_timetable_delta())
    assert seen["modified_since"] is not None, "fresh watermark must use a filtered sweep"
    assert seen["loaded"] == [99]
    assert "purged" in result and "0 purged" in result, result


def test_delta_full_sweep_when_reconcile_due(monkeypatch, writer):
    writer.mark_dataset_loaded(1, None, "OpA")
    writer.set_last_refresh("2026-09-10T00:00:00+00:00")
    writer.commit()  # no last_full_sweep watermark -> full sweep due
    seen = {}

    async def fake_sweep(client, modified_since=None):
        seen["modified_since"] = modified_since
        return {}, True  # catalogue no longer contains dataset 1

    async def fake_load(ds_id, force_reload=False, client=None):
        return {}

    monkeypatch.setattr(server, "_sweep_catalogue", fake_sweep)
    monkeypatch.setattr(server, "_load_dataset", fake_load)
    result = asyncio.run(server._load_timetable_delta())
    assert seen["modified_since"] is None, "full sweep must be unfiltered"
    assert "1 purged" in result, result  # dataset 1 withdrawn -> purged
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest test_sweep.py -v`
Expected: FAIL — `_sweep_catalogue` takes no `modified_since`; writer helpers missing; filtered-sweep behavior absent.

- [ ] **Step 3: Implement**

In `store.py`'s `TimetableWriter`, next to `last_refresh`:

```python
    def last_full_sweep(self) -> Optional[str]:
        row = self.conn.execute(
            "SELECT v FROM meta WHERE k='last_full_sweep'").fetchone()
        return row[0] if row and row[0] else None

    def set_last_full_sweep(self, iso: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO meta VALUES ('last_full_sweep', ?)", (iso,))

    def full_sweep_due(self, max_age_days: int = 7) -> bool:
        """True when a full (unfiltered) catalogue sweep is overdue.

        Withdrawal purges need the full catalogue, so reconcile forces a full
        sweep; otherwise a filtered delta sweep is enough until this watermark
        ages out (default 7 days)."""
        iso = self.last_full_sweep()
        ts = _parse_iso(iso) if iso else None
        if ts is None:
            return True
        return (datetime.now(timezone.utc) - ts).total_seconds() > max_age_days * 86400
```

`_parse_iso` does not exist — add a tiny parser next to `_parse_time` at the top of `store.py`:

```python
def _parse_iso(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
```

and add `from datetime import datetime, timezone` to store.py's imports (it currently imports neither).

In `server.py`, change `_sweep_catalogue`'s signature and URL build:

```python
async def _sweep_catalogue(client: httpx.AsyncClient,
                           modified_since: Optional[str] = None
                           ) -> tuple[dict[int, dict], bool]:
    """Sweep the BODS catalogue, returning ({id: entry}, complete).

    complete is True only when the sweep finished normally (empty or short
    page); False when it broke on a non-200 page (partial view).

    modified_since: when given, the API filters server-side on modifiedDate,
    so the sweep pages only changed datasets instead of the whole catalogue.
    A filtered sweep CANNOT detect catalogue withdrawals — callers that need
    reconcile must sweep unfiltered."""
    catalog: dict[int, dict] = {}
    offset = 0
    limit = 100
    while True:
        url = f"{BASE_URL}/api/v1/dataset/?limit={limit}&offset={offset}"
        if modified_since:
            url += "&modifiedDate=" + quote(modified_since, safe="")
        url += f"&api_key={API_KEY}"
        resp = await client.get(url)
        if resp.status_code != 200:
            return catalog, False
        results = resp.json().get("results", [])
        if not results:
            return catalog, True
        for r in results:
            catalog[r["id"]] = r
        if len(results) < limit:
            return catalog, True
        offset += limit
```

In `_load_timetable_delta`, replace the sweep block:

```python
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        catalog, _ = await _sweep_catalogue(client)
```

with:

```python
    # Full sweep when reconcile is requested or the full-sweep watermark is
    # stale (withdrawal purges need the whole catalogue); otherwise a
    # server-side filtered sweep touches only changed datasets.
    full = reconcile or writer.full_sweep_due()
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        catalog, sweep_complete = await _sweep_catalogue(
            client, modified_since=None if full else reference.isoformat())
    if full and sweep_complete:
        writer.set_last_full_sweep(datetime.now(timezone.utc).isoformat())
        writer.commit()
```

and gate the purge block on the full sweep:

```python
    if all_ids and sweep_complete and full:
```

(the existing `if not catalog: return ...` guard stays as-is).

Also update the `load_timetable_delta` tool docstring: add one line — "Catalogue sweeps are server-side filtered by modifiedDate when the index is fresh; a full sweep (needed to detect withdrawn datasets) runs when reconcile=True or at most every 7 days."

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest test_sweep.py test_delta.py -v`
Expected: PASS — test_delta exercises `_load_timetable_delta` paths; the reconcile purge now also requires `full`, which its tests satisfy (no `last_full_sweep` watermark recorded there → full sweep → behavior unchanged).

- [ ] **Step 5: Run the full suite and commit**

Run: `pytest`
Expected: PASS.

```bash
git add src/openbusdata_mcp/server.py src/openbusdata_mcp/store.py test_sweep.py
git commit -m "perf: server-side modifiedDate filter on delta catalogue sweeps

Filtered sweeps by default; full sweeps (withdrawal reconcile) run when
requested or at most every 7 days via a last_full_sweep watermark.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Phase B — relational query schema

### Task 4: days_mask column on journeys

**Files:**
- Modify: `src/openbusdata_mcp/store.py` (module constants near `MAX_RESOLVE`; `TimetableWriter.ensure_schema`; `add_journey`)
- Create: `test_relational.py`

**Interfaces:**
- Consumes: existing `days` JSON column (`["mon", ...]`).
- Produces: `store.DAY_BITS = {"mon":1, "tue":2, "wed":4, "thu":8, "fri":16, "sat":32, "sun":64}`; `store._days_mask(days: set) -> int`; `journeys.days_mask INT` column, backfilled once (meta flag `days_mask_backfilled`); `add_journey` writes `days_mask` on insert.

- [ ] **Step 1: Write the failing tests**

Create `test_relational.py`:

```python
# test_relational.py
import openbusdata_mcp.store as store_mod
from openbusdata_mcp.store import DAY_BITS, _days_mask


def test_days_mask_bit_values():
    assert DAY_BITS == {"mon": 1, "tue": 2, "wed": 4, "thu": 8,
                        "fri": 16, "sat": 32, "sun": 64}
    assert _days_mask({"mon", "tue"}) == 3
    assert _days_mask({"sun"}) == 64
    assert _days_mask({"mon", "wed", "fri"}) == 1 | 4 | 16
    assert _days_mask(set()) == 0
    assert _days_mask({"bogus"}) == 0, "unknown day names contribute nothing"


def test_add_journey_writes_days_mask(writer, store):
    writer.add_journey("Op", "4", "outbound", "J4", {"sat", "sun"},
                       [{"naptan": "010A", "arrival": None, "departure": "09:00:00"},
                        {"naptan": "010B", "arrival": "09:10:00", "departure": None}], 5)
    writer.commit()
    mask = writer.conn.execute(
        "SELECT days_mask FROM journeys WHERE id=1").fetchone()[0]
    assert mask == 96


def test_days_mask_backfilled_on_legacy_db(writer):
    # Simulate a legacy row written before days_mask existed. The per-test
    # fixture already ran ensure_schema on an empty DB (setting the flag), so
    # clear the flag first — exactly the state of a real legacy DB.
    writer.conn.execute(
        "INSERT INTO journeys (id, ds_id, op, route, direction, code, days, json) "
        "VALUES (10, 7, 'Op', '4b', 'outbound', 'J4b', '[\"thu\"]', '{}')")
    writer.commit()
    writer.conn.execute("DELETE FROM meta WHERE k='days_mask_backfilled'")
    writer.conn.commit()
    writer.ensure_schema()  # must backfill days_mask from the days JSON
    mask = writer.conn.execute(
        "SELECT days_mask FROM journeys WHERE id=10").fetchone()[0]
    assert mask == 8
    # Second ensure_schema with the flag present must not touch the row.
    writer.ensure_schema()
    assert writer.conn.execute(
        "SELECT days_mask FROM journeys WHERE id=10").fetchone()[0] == 8
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest test_relational.py -v`
Expected: FAIL — `DAY_BITS`/`_days_mask` not importable; `days_mask` column missing.

- [ ] **Step 3: Implement**

In `store.py`, next to `MAX_RESOLVE`:

```python
# Operating-day bitmask for SQL-level day filtering: mon=1 .. sun=64.
DAY_BITS = {"mon": 1, "tue": 2, "wed": 4, "thu": 8,
            "fri": 16, "sat": 32, "sun": 64}


def _days_mask(days: set) -> int:
    return sum(DAY_BITS[d] for d in days if d in DAY_BITS)
```

In `ensure_schema`, after the `journey_stop_times` backfill block and before the legacy `routes.ds_id` check, add:

```python
        # journeys.days_mask: SQL-level day filter (7-bit, mon=1 .. sun=64).
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(journeys)")}
        if "days_mask" not in cols:
            self.conn.execute(
                "ALTER TABLE journeys ADD COLUMN days_mask INT DEFAULT 0")
        if not self.conn.execute(
                "SELECT 1 FROM meta WHERE k='days_mask_backfilled'").fetchone():
            for jid, days_json in self.conn.execute(
                    "SELECT id, days FROM journeys WHERE days_mask=0").fetchall():
                try:
                    day_set = set(json.loads(days_json or "[]"))
                except (ValueError, TypeError):
                    day_set = set()
                self.conn.execute(
                    "UPDATE journeys SET days_mask=? WHERE id=?",
                    (_days_mask(day_set), jid))
            self.conn.execute(
                "INSERT OR REPLACE INTO meta VALUES ('days_mask_backfilled', '1')")
```

In `add_journey`, extend the INSERT:

```python
        cur = self.conn.execute(
            "INSERT INTO journeys (ds_id, op, route, direction, code, days, days_mask, json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ds_id, op, num, direction, code, json.dumps(sorted(days)),
             _days_mask(days), json.dumps(j)))
```

Note: `WHERE days_mask=0` legitimately re-scans empty-day journeys on every backfill run of a legacy DB — but the meta flag makes the whole block run once, so this is fine.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest test_relational.py test_delta.py -v`
Expected: PASS — `add_journey` callers in test_delta don't pass days_mask, so their calls are unchanged.

- [ ] **Step 5: Run the full suite and commit**

Run: `pytest`
Expected: PASS.

```bash
git add src/openbusdata_mcp/store.py test_relational.py
git commit -m "feat: days_mask bitmask column for SQL-level day filtering

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 5: journey_stop_times v2 — seq + arr columns

**Files:**
- Modify: `src/openbusdata_mcp/store.py` (`ensure_schema` CREATE TABLE + backfill; `add_journey`)
- Modify: `test_relational.py`

**Interfaces:**
- Consumes: `journeys.json` (one-time rebuild source); existing meta flag `journey_stop_times_backfilled` (reused for the v2 rebuild).
- Produces: `journey_stop_times(naptan TEXT, journey_id INT, seq INT, dep TEXT, arr TEXT)` — `dep` = COALESCE(departure, arrival), `arr` = arrival; index `jst_j(journey_id, seq)` alongside existing `jst_n(naptan, dep)`; `add_journey` writes `(naptan, journey_id, seq, dep, arr)` named-column inserts.

- [ ] **Step 1: Write the failing tests**

Append to `test_relational.py`:

```python
def test_journey_stop_times_v2_rows(writer, store):
    writer.add_journey("Op", "5", "outbound", "J5", {"mon"}, [
        {"naptan": "010A", "arrival": None, "departure": "09:00:00"},
        {"naptan": "010B", "arrival": "09:10:00", "departure": "09:11:00"},
        {"naptan": "010C", "arrival": "09:20:00", "departure": None}], 5)
    writer.commit()
    rows = writer.conn.execute(
        "SELECT seq, naptan, dep, arr FROM journey_stop_times "
        "WHERE journey_id=1 ORDER BY seq").fetchall()
    assert rows == [(0, "010A", "09:00:00", None),
                    (1, "010B", "09:11:00", "09:10:00"),
                    (2, "010C", "09:20:00", "09:20:00")], rows


def test_journey_stop_times_v2_backfill(writer):
    writer.add_journey("Op", "5b", "outbound", "J5b", {"mon"}, [
        {"naptan": "010A", "arrival": None, "departure": "08:00:00"},
        {"naptan": "010B", "arrival": "08:10:00", "departure": None}], 5)
    writer.commit()
    # Simulate a pre-v2 DB: old-shape rows, no seq/arr, flag present.
    writer.conn.execute("DELETE FROM journey_stop_times")
    writer.conn.execute(
        "INSERT INTO journey_stop_times (naptan, journey_id, dep) "
        "SELECT json_extract(value, '$.naptan'), j.id, "
        "       COALESCE(json_extract(value, '$.departure'),"
        "                json_extract(value, '$.arrival')) "
        "FROM journeys j, json_each(j.json, '$.stops')")
    writer.conn.commit()
    writer.ensure_schema()  # flag present -> no v2 rebuild expected here;
    # the real legacy path is exercised by resetting the flag:
    writer.conn.execute(
        "DELETE FROM meta WHERE k='journey_stop_times_backfilled'")
    writer.conn.commit()
    writer.ensure_schema()
    rows = writer.conn.execute(
        "SELECT seq, naptan, dep, arr FROM journey_stop_times "
        "WHERE journey_id=1 ORDER BY seq").fetchall()
    assert rows == [(0, "010A", "08:00:00", None),
                    (1, "010B", "08:10:00", "08:10:00")], rows
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest test_relational.py -v`
Expected: FAIL — `journey_stop_times` has no `seq`/`arr` columns (OperationalError on select).

- [ ] **Step 3: Implement**

In `ensure_schema`'s `executescript`, replace the `journey_stop_times` definition:

```sql
CREATE TABLE IF NOT EXISTS journey_stop_times (
    journey_id INT, seq INT, naptan TEXT, dep TEXT, arr TEXT);
```

and add to the index block:

```sql
CREATE INDEX IF NOT EXISTS jst_j ON journey_stop_times(journey_id, seq);
```

Replace the existing `journey_stop_times_backfilled` block with a v2-aware version (rebuild once from JSON, ever):

```python
        # journey_stop_times v2 (seq + arr): populated by add_journey; a DB
        # upgraded in place (pre-v2 rows, or an empty table with journeys
        # present) is rebuilt from the stored stops JSON once. dep follows
        # COALESCE(departure, arrival) to match add_journey's write side;
        # arr is the raw arrival (NULL where the timetable publishes none).
        if not self.conn.execute(
                "SELECT 1 FROM meta WHERE k='journey_stop_times_backfilled'").fetchone():
            cols = {r[1] for r in self.conn.execute(
                "PRAGMA table_info(journey_stop_times)")}
            for c in ("seq", "arr"):
                if c not in cols:
                    self.conn.execute(
                        f"ALTER TABLE journey_stop_times ADD COLUMN {c} INT"
                        if c == "seq" else
                        f"ALTER TABLE journey_stop_times ADD COLUMN {c} TEXT")
            self.conn.execute("DELETE FROM journey_stop_times")
            self.conn.execute(
                "INSERT INTO journey_stop_times (journey_id, seq, naptan, dep, arr) "
                "SELECT j.id, e.key, "
                "       json_extract(e.value, '$.naptan'), "
                "       COALESCE(json_extract(e.value, '$.departure'),"
                "                json_extract(e.value, '$.arrival')), "
                "       json_extract(e.value, '$.arrival') "
                "FROM journeys j, json_each(j.json, '$.stops') e")
            self.conn.execute(
                "INSERT OR REPLACE INTO meta VALUES"
                " ('journey_stop_times_backfilled', '1')")
```

`json_each`'s `key` on a JSON array is the 0-based element index — that is `seq`.

In `add_journey`, replace the two `journey_stop_times`/`journey_stops` executemany calls' `journey_stop_times` one:

```python
        self.conn.executemany(
            "INSERT INTO journey_stop_times (naptan, journey_id, seq, dep, arr) "
            "VALUES (?,?,?,?,?)",
            [(s["naptan"], cur.lastrowid, i,
              s.get("departure") or s.get("arrival"), s.get("arrival"))
             for i, s in enumerate(stops)])
```

(the `journey_stops` insert is untouched.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest test_relational.py test_perf.py test_delta.py -v`
Expected: PASS — `test_perf` tests 25–27 select named columns / count rows, so they hold with the new shape.

- [ ] **Step 5: Run the full suite and commit**

Run: `pytest`
Expected: PASS.

```bash
git add src/openbusdata_mcp/store.py test_relational.py
git commit -m "feat: journey_stop_times v2 — seq + arr columns, one-time JSON rebuild

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 6: SQL candidate pruning — _candidate_journeys over journey_stop_times

**Files:**
- Modify: `src/openbusdata_mcp/store.py` (`Candidate` NamedTuple near `DAY_BITS`; `_candidate_journeys` ~line 251; `_find_buses_cached` ~line 290; `_plan_direct_cached` ~line 313)
- Modify: `test_relational.py`
- Modify: `test_delta.py` (test 15, ~line 224)
- Modify: `test_perf.py` (`_old_candidates` ~line 91; test 29 ~line 107; delete test 30 ~line 125)

**Interfaces:**
- Consumes: `journey_stop_times(journey_id, seq, naptan, dep, arr)` + `jst_j`/`jst_n` indexes (Task 5); `journeys.days_mask` (Task 4).
- Produces: `store.Candidate(NamedTuple)` with fields `journey_id: int, operator: str, route: str, direction: str, code: str, naptan_a: str, naptan_b: str, depart_a: Optional[str], arrive_b: str, seq_a: int, seq_b: int`. `_candidate_journeys(naptans_a, naptans_b, day, target_s)` yields `Candidate`s from one SQL query (no JSON parsing). `_find_buses_cached` / `_plan_direct_cached` consume `Candidate`s; their public wrappers and cache keys are unchanged.

- [ ] **Step 1: Write the failing test**

Append to `test_relational.py`:

```python
def test_candidate_journeys_sql_parity(writer, store):
    import json as _json
    writer.add_journey("Op", "6", "outbound", "J6", {"mon"}, [
        {"naptan": "A1", "arrival": None, "departure": "09:00:00"},
        {"naptan": "A2", "arrival": "09:05:00", "departure": None},
        {"naptan": "B1", "arrival": "09:20:00", "departure": None}], 5)
    writer.add_journey("Op", "6b", "outbound", "J6b", {"tue"}, [
        {"naptan": "A1", "arrival": None, "departure": "09:00:00"},
        {"naptan": "B1", "arrival": "09:20:00", "departure": None}], 5)
    writer.commit()
    cands = list(store._candidate_journeys({"A1"}, {"B1"}, "mon", "09:30:00"))
    assert len(cands) == 1
    c = cands[0]
    assert (c.journey_id, c.code, c.naptan_a, c.naptan_b) == (1, "J6", "A1", "B1")
    assert c.depart_a == "09:00:00" and c.arrive_b == "09:20:00"
    assert (c.seq_a, c.seq_b) == (0, 2)
    # First-occurrence semantics: A2 appears before B1; boarding at A2 works.
    c2 = list(store._candidate_journeys({"A2"}, {"B1"}, "mon", "09:30:00"))
    assert len(c2) == 1 and c2[0].seq_a == 1 and c2[0].seq_b == 2
    # Day mask filters in SQL: the tue journey only on tue.
    tue = list(store._candidate_journeys({"A1"}, {"B1"}, "tue", "09:30:00"))
    assert [x.code for x in tue] == ["J6b"]
    # Arrival target filters: nothing arrives by 09:19.
    assert list(store._candidate_journeys({"A1"}, {"B1"}, "mon", "09:19:00")) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest test_relational.py::test_candidate_journeys_sql_parity -v`
Expected: FAIL — `_candidate_journeys` still yields `(dict, idx_a, idx_b)` tuples (AttributeError on `.code`).

- [ ] **Step 3: Implement**

In `store.py`, add after `DAY_BITS`/`_days_mask`:

```python
class Candidate(NamedTuple):
    """One A-boards-before-B journey resolved entirely in SQL."""
    journey_id: int
    operator: str
    route: str
    direction: str
    code: str
    naptan_a: str
    naptan_b: str
    depart_a: Optional[str]
    arrive_b: Optional[str]
    seq_a: int
    seq_b: int
```

(add `from typing import NamedTuple, Optional` to imports.)

Replace `_candidate_journeys`:

```python
    def _candidate_journeys(self, naptans_a: set, naptans_b: set, day: str,
                            target_s: str):
        """Yield Candidates for journeys boarding in A, alighting in B, running
        on day, arriving at B by target_s — resolved in one indexed SQL query.

        First-occurrence semantics preserved: MIN(seq) per journey reproduces
        the previous Python next() scan. First-occurrence of a naptan in a
        loop route's stop list differs from last-occurrence only for loop
        routes repeating a naptan from A or B — an accepted edge case.
        Variable budget: <= 400 + 400 + 2 = 802 < 999 (see MAX_RESOLVE)."""
        if not naptans_a or not naptans_b:
            return
        a_marks = ",".join("?" * len(naptans_a))
        b_marks = ",".join("?" * len(naptans_b))
        rows = self.conn.execute(f"""
            SELECT j.id, j.op, j.route, j.direction, j.code,
                   sa.naptan, sb.naptan, sa.dep, sb.arr, aa.seq, bb.seq
            FROM journeys j
            JOIN (SELECT journey_id, MIN(seq) AS seq FROM journey_stop_times
                  WHERE naptan IN ({a_marks}) GROUP BY journey_id) aa
              ON aa.journey_id = j.id
            JOIN (SELECT journey_id, MIN(seq) AS seq FROM journey_stop_times
                  WHERE naptan IN ({b_marks}) GROUP BY journey_id) bb
              ON bb.journey_id = j.id
            JOIN journey_stop_times sa ON sa.journey_id = j.id AND sa.seq = aa.seq
            JOIN journey_stop_times sb ON sb.journey_id = j.id AND sb.seq = bb.seq
            WHERE j.days_mask & ? != 0
              AND aa.seq < bb.seq
              AND sb.arr IS NOT NULL AND sb.arr <= ?
        """, list(naptans_a) + list(naptans_b)
             + [DAY_BITS[day], target_s]).fetchall()
        for (jid, op, route, direction, code, na, nb, dep_a, arr_b,
             seq_a, seq_b) in rows:
            yield Candidate(jid, op, route, direction, code, na, nb,
                            dep_a, arr_b, seq_a, seq_b)
```

Rewrite `_find_buses_cached` and `_plan_direct_cached` bodies (wrappers, cache decorator, and keys unchanged):

```python
    @functools.lru_cache(maxsize=128)
    def _find_buses_cached(self, key: tuple, a: frozenset, b: frozenset,
                           day: str, target_s: str) -> list[dict]:
        cands = list(self._candidate_journeys(set(a), set(b), day, target_s))
        names = self.stop_names_bulk(
            [c.naptan_a for c in cands] + [c.naptan_b for c in cands])
        out = [{
            "operator": c.operator, "route": c.route,
            "direction": c.direction, "journey_code": c.code,
            "board_at": names.get(c.naptan_a, "Unknown"),
            "depart": c.depart_a,
            "alight_at": names.get(c.naptan_b, "Unknown"),
            "arrive": c.arrive_b} for c in cands]
        out.sort(key=lambda x: x["arrive"] or "")
        return out[:20]

    @functools.lru_cache(maxsize=128)
    def _plan_direct_cached(self, key: tuple, a: frozenset, b: frozenset,
                            day: str, target_s: str) -> list[dict]:
        cands = list(self._candidate_journeys(set(a), set(b), day, target_s))
        names = self.stop_names_bulk(
            [c.naptan_a for c in cands] + [c.naptan_b for c in cands])
        return [{
            "type": "direct",
            "legs": [{
                "operator": c.operator, "route": c.route,
                "board": names.get(c.naptan_a, "Unknown"),
                "depart": c.depart_a,
                "alight": names.get(c.naptan_b, "Unknown"),
                "arrive": c.arrive_b}],
            "total_changes": 0} for c in cands]
```

In `test_delta.py`, replace test 15:

```python
# --- Test 15: _candidate_journeys yields the same journeys both tools use
def test_15_candidate_journeys_shared_helper(writer, store):
    writer.add_journey("Op", "15", "outbound", "J15", {"mon"},
                       [{"naptan": "15A", "arrival": None, "departure": "09:00:00"},
                        {"naptan": "15B", "arrival": "09:10:00", "departure": None}], 99)
    writer.commit()
    cands = list(store._candidate_journeys({"15A"}, {"15B"}, "mon", "09:30:00"))
    assert len(cands) == 1 and cands[0].code == "J15"
    assert (cands[0].naptan_a, cands[0].naptan_b) == ("15A", "15B")
    assert list(store._candidate_journeys({"15A"}, {"15B"}, "tue", "09:30:00")) == []
```

In `test_perf.py`, replace `_old_candidates` (it must not depend on `_fetch_journeys`, which dies in Task 8) and test 29's unpacking, and delete test 30:

```python
def _old_candidates(store, na, nb, day, target_s):
    """Pre-change implementation — frozen reference reading journeys.json."""
    import json as _json
    rows = store.conn.execute("SELECT id, days, json FROM journeys").fetchall()
    for jid, days, raw in rows:
        if day not in _json.loads(days):
            continue
        stops = _json.loads(raw)["stops"]
        ia = next((i for i, s in enumerate(stops) if s["naptan"] in na), None)
        ib = next((i for i, s in enumerate(stops) if s["naptan"] in nb), None)
        if ia is None or ib is None or ia >= ib:
            continue
        ar = stops[ib].get("arrival")
        if ar and ar[:8] <= target_s:
            yield jid, ia, ib


def test_29_candidate_journeys_intersection_parity(writer, store):
    # _candidate_journeys rows are unordered SQL output; both sides compared
    # sorted on (journey_id, seq_a, seq_b) triples.
    for i in range(2):
        seq = ["CTR0", f"R{i}_hub", f"R{i}_1", f"R{i}_2"]
        for dep0 in ("08:00:00", "09:00:00"):
            stops = [{"naptan": s, "arrival": dep0, "departure": dep0} for s in seq]
            writer.add_journey(f"Op{i}", str(i), "outbound",
                               f"J{i}-{dep0}", {"mon"}, stops, 1)
    writer.commit()
    for na, nb in [({"CTR0"}, {"R0_2"}), ({"CTR0", "R1_hub"}, {"R0_1", "R1_2"})]:
        got = [(c.journey_id, c.seq_a, c.seq_b)
               for c in store._candidate_journeys(na, nb, "mon", "09:30:00")]
        ref = list(_old_candidates(store, na, nb, "mon", "09:30:00"))
        assert sorted(got) == sorted(ref), (na, nb, got, ref)
```

Delete `test_30_candidate_journeys_fetches_only_intersection` entirely — with SQL-side pruning there is no Python fetch step left to spy on; the property it guarded (never touching non-intersection data) is now inherent to the query.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest test_relational.py test_delta.py test_perf.py test_robustness.py -v`
Expected: PASS. Note: `find_buses_by_arrival_time`'s `depart` now falls back to the boarding stop's arrival when no departure is published (was `null`) — the documented, accepted behavior delta from the spec.

- [ ] **Step 5: Run the full suite and commit**

Run: `pytest`
Expected: PASS.

```bash
git add src/openbusdata_mcp/store.py test_relational.py test_delta.py test_perf.py
git commit -m "perf: candidate journeys resolved in one indexed SQL query

_candidate_journeys yields a Candidate NamedTuple from journey_stop_times
with MIN(seq) first-occurrence semantics; find_buses_by_arrival_time and
plan_direct consume it without touching journeys.json.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Phase C — SQL one-change planner + cleanup

### Task 7: One-change planner as one SQL self-join

**Files:**
- Modify: `src/openbusdata_mcp/store.py` (`_plan_one_change_cached` ~line 337 — body only; public wrapper unchanged)
- Create: `test_planner.py`

**Interfaces:**
- Consumes: `journey_stop_times(journey_id, seq, naptan, dep, arr)`, `jst_n(naptan, dep)`, `jst_j(journey_id, seq)`, `journeys.days_mask` (Tasks 4–5); `DAY_BITS`.
- Produces: `_plan_one_change_cached` same signature `(key, a: frozenset, b: frozenset, day, target_s) -> list[dict]`, same output shape (`type: "change"`, two legs, `total_changes: 1`). Public `plan_one_change` unchanged. `plan_journey` tool in server.py needs NO changes.

- [ ] **Step 1: Write the failing tests**

Create `test_planner.py`:

```python
# test_planner.py
from openbusdata_mcp.store import _days_mask  # noqa: F401  (import sanity)

A_TO_M = [{"naptan": "A1", "arrival": None, "departure": "09:00:00"},
          {"naptan": "M1", "arrival": "09:10:00", "departure": "09:12:00"},
          {"naptan": "B1", "arrival": "09:20:00", "departure": None}]
M_TO_B = [{"naptan": "M1", "arrival": None, "departure": "09:15:00"},
          {"naptan": "B1", "arrival": "09:25:00", "departure": None}]


def _seed(writer):
    writer.add_journey("OpA", "7", "outbound", "J7a", {"mon"}, A_TO_M, 1)
    writer.add_journey("OpB", "8", "outbound", "J7b", {"mon"}, M_TO_B, 2)
    writer.commit()


def test_one_change_plan_found(writer, store):
    _seed(writer)
    plans = store.plan_one_change({"A1"}, {"B1"}, "mon", "10:00:00")
    assert len(plans) == 1, plans
    p = plans[0]
    assert p["type"] == "change" and p["total_changes"] == 1
    leg1, leg2 = p["legs"]
    assert (leg1["operator"], leg1["board"], leg1["alight"]) == ("OpA", "A1", "M1")
    assert leg1["depart"] == "09:00:00" and leg1["arrive"] == "09:10:00"
    assert (leg2["operator"], leg2["board"], leg2["alight"]) == ("OpB", "M1", "B1")
    assert leg2["depart"] == "09:15:00" and leg2["arrive"] == "09:25:00"


def test_one_change_respects_target(writer, store):
    _seed(writer)
    # Change itinerary arrives 09:25 — excluded by a 09:24 target.
    assert store.plan_one_change({"A1"}, {"B1"}, "mon", "09:24:00") == []


def test_one_change_respects_day(writer, store):
    _seed(writer)
    assert store.plan_one_change({"A1"}, {"B1"}, "tue", "10:00:00") == []


def test_one_change_no_self_transfer(writer, store):
    # Single journey through A->M->B must NOT yield a change plan onto itself.
    writer.add_journey("OpA", "7c", "outbound", "J7c", {"mon"}, A_TO_M, 1)
    writer.commit()
    assert store.plan_one_change({"A1"}, {"B1"}, "mon", "10:00:00") == []


def test_one_change_direct_first_occurrence_not_reboarded(writer, store):
    # j2 boards at the FIRST occurrence of M1; a later M1 in j2 is ignored.
    writer.add_journey("OpA", "7", "outbound", "J7a", {"mon"}, A_TO_M, 1)
    writer.add_journey("OpB", "8", "outbound", "J7d", {"mon"}, [
        {"naptan": "M1", "arrival": "08:00:00", "departure": "08:05:00"},   # first M1: too early
        {"naptan": "X9", "arrival": "08:30:00", "departure": None},
        {"naptan": "M1", "arrival": "09:15:00", "departure": "09:16:00"},   # later M1: would work, must be ignored
        {"naptan": "B1", "arrival": "09:25:00", "departure": None}], 2)
    writer.commit()
    assert store.plan_one_change({"A1"}, {"B1"}, "mon", "10:00:00") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest test_planner.py -v`
Expected: FAIL or ERROR — the current implementation reads JSON and may behave differently; in particular `test_one_change_no_self_transfer` and the first-occurrence test exercise semantics the current code may violate or the tests may pass by accident — either way, the failing tests pin the new contract before the rewrite.

- [ ] **Step 3: Implement**

Replace the body of `_plan_one_change_cached` (keep the `@functools.lru_cache(maxsize=128)` decorator and the exact signature):

```python
    @functools.lru_cache(maxsize=128)
    def _plan_one_change_cached(self, key: tuple, a: frozenset, b: frozenset,
                                day: str, target_s: str) -> list[dict]:
        """One-change plans in a single SQL self-join over journey_stop_times.

        Semantics preserved from the Python-loop version:
          - leg1 boards at the FIRST occurrence of any A stop (MIN(seq)),
            alights at any later stop with a published arrival;
          - leg2 boards at the FIRST occurrence of that same mid stop,
            departs at/after leg1's mid arrival (dep column is already
            COALESCE(departure, arrival)), and alights at the FIRST
            occurrence of any B stop, arriving by target_s;
          - j2 != j1; both journeys run on `day` (days_mask).
        Variable budget: <= 400 + 400 + 2 = 802 < 999. LIMIT 2000 bounds the
        worst-case cross join; the tool-level dedup + top-15 cap follows."""
        a, b = set(a), set(b)
        if not a or not b:
            return []
        a_marks = ",".join("?" * len(a))
        b_marks = ",".join("?" * len(b))
        mask = DAY_BITS[day]
        rows = self.conn.execute(f"""
            SELECT j1.id, j1.op, j1.route, j1.direction, j1.code,
                   sa.naptan, sm.naptan, sa.dep, sm.arr,
                   j2.id, j2.op, j2.route, j2.direction, j2.code,
                   sm2.dep, sb.naptan, sb.arr
            FROM journeys j1
            JOIN (SELECT journey_id, MIN(seq) AS seq FROM journey_stop_times
                  WHERE naptan IN ({a_marks}) GROUP BY journey_id) aa
              ON aa.journey_id = j1.id
            JOIN journey_stop_times sa ON sa.journey_id = j1.id AND sa.seq = aa.seq
            JOIN journey_stop_times sm ON sm.journey_id = j1.id
              AND sm.seq > aa.seq AND sm.arr IS NOT NULL
            JOIN journeys j2 ON j2.id != j1.id AND j2.days_mask & ? != 0
            JOIN journey_stop_times sm2 ON sm2.journey_id = j2.id
              AND sm2.naptan = sm.naptan AND sm2.dep >= sm.arr
              AND sm2.seq = (SELECT MIN(seq) FROM journey_stop_times
                             WHERE journey_id = j2.id AND naptan = sm.naptan)
            JOIN (SELECT journey_id, MIN(seq) AS seq FROM journey_stop_times
                  WHERE naptan IN ({b_marks}) GROUP BY journey_id) bb
              ON bb.journey_id = j2.id
            JOIN journey_stop_times sb ON sb.journey_id = j2.id AND sb.seq = bb.seq
            WHERE j1.days_mask & ? != 0
              AND sa.dep IS NOT NULL
              AND sm2.seq < bb.seq
              AND sb.arr IS NOT NULL AND sb.arr <= ?
            LIMIT 2000
        """, list(a) + list(b) + [mask, mask, target_s]).fetchall()
        naptans = {r[5] for r in rows} | {r[6] for r in rows} | {r[15] for r in rows}
        names = self.stop_names_bulk(list(naptans))
        plans = []
        for (jid1, op1, route1, dir1, code1, na, mid, dep_a, mid_arr,
             jid2, op2, route2, dir2, code2, dep2, nb, arr_b) in rows:
            plans.append({
                "type": "change",
                "legs": [
                    {"operator": op1, "route": route1,
                     "board": names.get(na, "Unknown"), "depart": dep_a,
                     "alight": names.get(mid, "Unknown"), "arrive": mid_arr},
                    {"operator": op2, "route": route2,
                     "board": names.get(mid, "Unknown"), "depart": dep2,
                     "alight": names.get(nb, "Unknown"), "arrive": arr_b}],
                "total_changes": 1})
        return plans
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest test_planner.py test_robustness.py::test_5_plan_dedup_keeps_distinct_operators -v`
Expected: PASS — including test_robustness test 5, which pins the tool-level contract through `plan_journey`.

- [ ] **Step 5: Run the full suite and commit**

Run: `pytest`
Expected: PASS.

```bash
git add src/openbusdata_mcp/store.py test_planner.py
git commit -m "perf: one-change planner as a single indexed SQL self-join

Same first-occurrence semantics as the Python-loop version; the (naptan, dep)
index drives the leg1->leg2 join. No tool contract change.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 8: Remove dead JSON-fetch and loop helpers

**Files:**
- Modify: `src/openbusdata_mcp/store.py` (delete `_journey_row_to_out` ~line 204, `_fetch_journey` ~line 212, `_fetch_journeys` ~line 218, `_journeys_touching` ~line 234, `_journeys_departing` ~line 244)
- Modify: `test_delta.py` (delete test 13 `_journeys_touching` block ~line 200, test 14 ~line 211, test 22 ~line 268)
- Modify: `test_perf.py` (delete test 28 ~line 50)

**Interfaces:**
- Consumes: Tasks 4–7 (nothing calls these helpers anymore).
- Produces: nothing — pure removal. `find_buses_by_arrival_time`, `plan_direct`, `plan_one_change`, `plan_journey` and all tool contracts unchanged.

- [ ] **Step 1: Verify nothing references the helpers**

Run: `grep -rn "_fetch_journeys\|_fetch_journey\b\|_journey_row_to_out\|_journeys_touching\|_journeys_departing" src/ test_delta.py test_perf.py test_robustness.py test_planner.py test_relational.py test_http.py test_sweep.py e2e_sqlite.py`
Expected: matches only inside `store.py` definitions and the tests listed for deletion (test 13/14/22 in test_delta.py, test 28 in test_perf.py). If a match appears anywhere else, STOP and re-classify — something still consumes these.

- [ ] **Step 2: Delete the helpers and their tests**

Delete from `store.py`: `_journey_row_to_out`, `_fetch_journey`, `_fetch_journeys`, `_journeys_touching`, `_journeys_departing` (all five methods of `TimetableStore`).

Delete from `test_delta.py`: `test_13_journey_stops_populated_and_purged`, `test_14_fetch_journeys_batches`, `test_22_fetch_journeys_chunks_past_variable_limit`.

Delete from `test_perf.py`: `test_28_journeys_departing_uses_index_and_matches_old_semantics`.

- [ ] **Step 3: Run the full suite**

Run: `pytest`
Expected: PASS.

- [ ] **Step 4: Run the perf sanity script**

Run: `python test_perf.py 2>/dev/null || pytest test_perf.py -q`
Expected: PASS (`test_perf.py` has no `__main__` runner; pytest invocation is the real check).

- [ ] **Step 5: Commit**

```bash
git add src/openbusdata_mcp/store.py test_delta.py test_perf.py
git commit -m "refactor: remove dead JSON-fetch and loop helpers

Journey data is read through the relational tables everywhere; the
journeys.json blob is now write-only legacy.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Final verification (run after Task 8)

- [ ] `pytest` — full suite green.
- [ ] `python -c "import openbusdata_mcp.server"` — server imports cleanly (spec tools register, schema upgrades run).
- [ ] Spot-check an existing DB upgrade path if `~/.cache/openbusdata/index.db` exists: back up the file first, then `python -c "from openbusdata_mcp.store import TimetableWriter; w = TimetableWriter(); w.ensure_schema(); w.commit()"`, and confirm `SELECT COUNT(*) FROM journeys WHERE days_mask=0` matches journeys with no operating days (expected ~0) and `journey_stop_times` row count is unchanged.
- [ ] Push the branch to the `Zapskii` fork and open the upstream PR per the integration pattern; PR body ends with the Claude Code attribution line.