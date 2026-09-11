# Phase 6 Implementation Plan: FTS5 Stop Search + True HTTP Streaming

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the three deferred items from the Phase 1-5 code-health plan: FTS5 stop-name search, true HTTP streaming of dataset zips, and a robust test_20 planner assertion.

**Architecture:** Add an FTS5 virtual table (`stops_fts`) mirroring `stops` via sync triggers, with a one-time in-place backfill keyed on a meta flag; route `search_stops`/`resolve_stop` through FTS5 with a LIKE fallback. Stream zip downloads to a seekable temp file instead of holding `zip_resp.content` in RAM. Seed test_20 with enough journeys to force the covering-index plan.

**Tech Stack:** Python `>=3.10`, SQLite FTS5 (built-in), httpx streaming (built-in). No new runtime dependencies.

**Spec:** `docs/superpowers/specs/2026-09-10-phase-6-fts5-streaming-design.md` (approved 2026-09-10, commit `a34e3d6`)

## Global Constraints

- Python `>=3.10`; **no new runtime dependencies** (FTS5 is built into SQLite; httpx streaming is built-in).
- **Tool output shapes must not change** — `search_stops` keeps returning `[{"naptan", "name"}]`; `resolve_stop` stays internal.
- **24:00+ post-midnight times must survive unclamped** — untouched by this phase.
- **Schema changes must be additive** — existing `index.db` files (3.48 GB in prod) upgrade in place, no rebuild.
- **One transaction per dataset** in the loader — the temp file lives outside the DB transaction; the parse loop stays inside the per-dataset transaction.
- **The delta watermark policy stays** — untouched by this phase.
- Tests run from the repo root; the store DB is redirected to a temp dir before any persistence.

## Pre-flight rulings (plan-level corrections to the spec)

These are baked into the task text below; the implementer follows the task text, not the spec's code snippets verbatim.

- **Ruling 6.1-A:** `tempfile` is **not** imported in `server.py` (the spec §2.1 claimed it was). Task 6.2 adds `import tempfile`.
- **Ruling 6.1-B:** the spec's `os.fdopen(tmp_fd, "wb")` leaks the fd on early returns (meta 404, no `download_url`, stream 404, `size < 100`). Task 6.2 closes the fd immediately after `mkstemp` and opens the path normally — same streaming behavior, no fd leak.
- **Ruling 6.1-C:** `_fts_query` must drop tokens with no alphanumeric characters (bare `%`, `_`, `\`, quotes). The spec's version only drops empty tokens, which would send `"%"*` to FTS5 (tokenizes to nothing — undefined/error-prone). The alphanumeric filter makes the LIKE fallback deterministic.
- **Ruling 6.1-D:** test_20 seeds 300 journeys with **distinct route numbers** (not all identical) so the `DISTINCT` scan genuinely benefits from the covering index — matches the spec's stated goal ("forced to choose the covering index").

---

### Task 6.1: FTS5 stop-name search

**Files:**
- Modify: `src/openbusdata_mcp/store.py` — add `_fts_query` (module-level, next to `_like_escape` at line 14); add FTS5 DDL to the `ensure_schema` executescript (line 351+); add the one-time backfill after the `journey_stops` backfill block (line ~385); rewrite `search_stops` (79-84) and `resolve_stop` (86-94).
- Test: `test_delta.py` — append Test 23 (backfill + trigger sync) and Test 24 (FTS5 matching + LIKE fallback).
- Docs: `docs/RUNBOOK.md` — add the FTS5 migration note.

**Interfaces:**
- Consumes: `stops` table (normal rowid table: `naptan TEXT PRIMARY KEY, name TEXT, lat REAL, lon REAL`), `add_stop` (`INSERT ... ON CONFLICT(naptan) DO UPDATE`, store.py:444-449), `MAX_RESOLVE` (400, store.py:22), `_like_escape` (store.py:14), `Optional` (already imported).
- Produces: `_fts_query(query: str) -> Optional[str]` (module-level); `search_stops(query, limit=50) -> list[dict]` (unchanged signature); `resolve_stop(stop_query) -> set[str]` (unchanged signature); `stops_fts` virtual table + 3 sync triggers + one-time backfill keyed on the `stops_fts_backfilled` meta flag.

- [ ] **Step 1: Write the failing tests**

Append to `test_delta.py` (after Test 22):

```python
# --- Test 23: stops_fts backfill + trigger sync keep the FTS index current
def test_23_fts_backfill_and_trigger_sync():
    # Simulate a pre-FTS DB: stops present, no stops_fts, no backfill flag.
    _bk = Path(tempfile.mkdtemp()) / "fts.db"
    w2 = TimetableWriter(_bk)
    w2.conn.execute(
        "CREATE TABLE stops (naptan TEXT PRIMARY KEY, name TEXT, lat REAL, lon REAL)")
    w2.conn.execute("INSERT INTO stops VALUES ('F1','Alpha Road',NULL,NULL)")
    w2.conn.execute("INSERT INTO stops VALUES ('F2','Beta Lane',NULL,NULL)")
    w2.conn.commit()
    w2.ensure_schema()
    n = w2.conn.execute("SELECT COUNT(*) FROM stops_fts").fetchone()[0]
    assert n == 2, f"backfill missing: {n}"
    # Flag prevents re-running the backfill.
    w2.ensure_schema()
    n = w2.conn.execute("SELECT COUNT(*) FROM stops_fts").fetchone()[0]
    assert n == 2, "backfill re-ran"
    # Insert trigger (add_stop -> INSERT ... ON CONFLICT DO UPDATE).
    w2.add_stop("F3", "Gamma Way")
    w2.conn.commit()
    n = w2.conn.execute("SELECT COUNT(*) FROM stops_fts").fetchone()[0]
    assert n == 3, "insert trigger missed"
    # Update trigger (conflict path fires the UPDATE trigger).
    w2.add_stop("F1", "Alpha Road North")
    w2.conn.commit()
    row = w2.conn.execute(
        "SELECT name FROM stops_fts WHERE rowid="
        "(SELECT rowid FROM stops WHERE naptan='F1')").fetchone()
    assert row and row[0] == "Alpha Road North", "update trigger missed"
    # Delete trigger.
    w2.conn.execute("DELETE FROM stops WHERE naptan='F2'")
    w2.conn.commit()
    n = w2.conn.execute("SELECT COUNT(*) FROM stops_fts").fetchone()[0]
    assert n == 2, "delete trigger missed"


# --- Test 24: search_stops uses FTS5 (token+prefix) with a LIKE fallback
def test_24_fts_search_and_like_fallback(writer, store):
    writer.add_stop("S1", "Oaks Cross")
    writer.add_stop("S2", "Oakscross Road")
    writer.add_stop("S3", "100% Bus Stop")
    writer.add_stop("S4", "Under_Score Stop")
    writer.commit()
    # FTS5 token+prefix: "oaks" matches both "Oaks" and "Oakscross".
    assert {s["naptan"] for s in store.search_stops("oaks")} == {"S1", "S2"}
    # FTS5 AND: both tokens must match.
    assert {s["naptan"] for s in store.search_stops("bus stop")} == {"S3"}
    # LIKE fallback: mid-token substring FTS5 cannot express.
    assert {s["naptan"] for s in store.search_stops("kscr")} == {"S2"}
    # Untokenizable input -> LIKE fallback (wildcards match literally).
    assert {s["naptan"] for s in store.search_stops("%")} == {"S3"}
    assert {s["naptan"] for s in store.search_stops("_")} == {"S4"}
    # Short query (< 2 chars) -> LIKE directly (substring, not prefix).
    assert {s["naptan"] for s in store.search_stops("o")} == {"S1", "S2", "S3", "S4"}
    # resolve_stop: FTS5 path (capped) and NaPTAN literal unchanged.
    assert store.resolve_stop("oaks") == {"S1", "S2"}
    assert store.resolve_stop("010A") == {"010A"}
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run --with 'mcp<2' --with httpx --with pyyaml --with pytest pytest test_delta.py::test_23_fts_backfill_and_trigger_sync test_delta.py::test_24_fts_search_and_like_fallback -q`
Expected: FAIL — `no such table: stops_fts` (the FTS5 table does not exist yet).

- [ ] **Step 3: Add the `_fts_query` helper**

In `src/openbusdata_mcp/store.py`, immediately after `_like_escape` (line 14):

```python
def _fts_query(query: str) -> Optional[str]:
    """Build an FTS5 MATCH expression from user input, or None if untokenizable.

    Each whitespace token becomes a quoted prefix term, AND-joined. Tokens with
    no alphanumeric characters (bare '%', '_', '\', quotes) tokenize to nothing
    in FTS5, so they return None and the caller falls back to LIKE.
    """
    tokens = [t for t in query.strip().lower().split() if t]
    tokens = [t for t in tokens if any(c.isalnum() for c in t)]
    if not tokens:
        return None
    return " AND ".join(f'"{t.replace(chr(34), chr(34) * 2)}"*' for t in tokens)
```

- [ ] **Step 4: Add the FTS5 DDL to `ensure_schema`**

In `src/openbusdata_mcp/store.py`, inside the `executescript` string (line 351+), after the `CREATE INDEX IF NOT EXISTS j_oproute ON journeys(op, route);` line, add:

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS stops_fts USING fts5(
    name, naptan UNINDEXED, tokenize='unicode61');
CREATE TRIGGER IF NOT EXISTS stops_fts_ai AFTER INSERT ON stops BEGIN
    INSERT INTO stops_fts(rowid, name, naptan) VALUES (new.rowid, new.name, new.naptan);
END;
CREATE TRIGGER IF NOT EXISTS stops_fts_ad AFTER DELETE ON stops BEGIN
    DELETE FROM stops_fts WHERE rowid = old.rowid;
END;
CREATE TRIGGER IF NOT EXISTS stops_fts_au AFTER UPDATE ON stops BEGIN
    UPDATE stops_fts SET name = new.name, naptan = new.naptan WHERE rowid = old.rowid;
END;
```

- [ ] **Step 5: Add the one-time backfill**

In `src/openbusdata_mcp/store.py`, immediately after the `journey_stops` backfill block (after the `INSERT OR REPLACE INTO meta VALUES ('journey_stops_backfilled', '1')` line, ~line 385), add:

```python
        # One-time backfill: stops_fts only gets populated by the triggers, so
        # a DB upgraded in place (stops already present) would have an empty
        # FTS index and every FTS search would silently return nothing.
        # Backfill from stops once, keyed on a meta flag so it never re-runs.
        if not self.conn.execute(
                "SELECT 1 FROM meta WHERE k='stops_fts_backfilled'").fetchone():
            self.conn.execute(
                "INSERT OR REPLACE INTO stops_fts(rowid, name, naptan) "
                "SELECT rowid, name, naptan FROM stops")
            self.conn.execute(
                "INSERT OR REPLACE INTO meta VALUES ('stops_fts_backfilled', '1')")
```

- [ ] **Step 6: Rewrite `search_stops`**

In `src/openbusdata_mcp/store.py`, replace the current `search_stops` (lines 79-84) with:

```python
    def _search_stops_like(self, query: str, limit: int = 50) -> list[dict]:
        q = f"%{_like_escape(query.lower())}%"
        rows = self.conn.execute(
            "SELECT naptan, name FROM stops WHERE LOWER(name) LIKE ? ESCAPE '\\' "
            "ORDER BY name LIMIT ?", (q, limit)).fetchall()
        return [{"naptan": n, "name": name} for n, name in rows]

    def search_stops(self, query: str, limit: int = 50) -> list[dict]:
        q = query.strip()
        if len(q) < 2:
            return self._search_stops_like(q, limit)   # short queries: LIKE directly
        fts = _fts_query(q)
        if fts:
            rows = self.conn.execute(
                "SELECT naptan, name FROM stops_fts WHERE stops_fts MATCH ? "
                "ORDER BY rank LIMIT ?", (fts, limit)).fetchall()
            if rows:
                return [{"naptan": n, "name": name} for n, name in rows]
        return self._search_stops_like(q, limit)        # FTS5 empty -> LIKE fallback
```

- [ ] **Step 7: Rewrite `resolve_stop`**

In `src/openbusdata_mcp/store.py`, replace the current `resolve_stop` (lines 86-94) with:

```python
    def resolve_stop(self, stop_query: str) -> set[str]:
        """NaPTAN-shaped input = literal, else FTS5 match (capped), else LIKE."""
        q = stop_query.strip()
        if q.isdigit() or (len(q) >= 4 and q[:2].isdigit() and q.isalnum()):
            return {q}
        fts = _fts_query(q)
        if fts:
            rows = self.conn.execute(
                "SELECT naptan FROM stops_fts WHERE stops_fts MATCH ? LIMIT ?",
                (fts, MAX_RESOLVE)).fetchall()
            if rows:
                return {r[0] for r in rows}
        return {r[0] for r in self.conn.execute(
            "SELECT naptan FROM stops WHERE LOWER(name) LIKE ? ESCAPE '\\' "
            f"LIMIT {MAX_RESOLVE}",
            (f"%{_like_escape(q.lower())}%",))}
```

- [ ] **Step 8: Run the full suite to verify everything passes**

Run: `uv run --with 'mcp<2' --with httpx --with pyyaml --with pytest pytest -q`
Expected: PASS — all 39 tests (22 existing delta + 2 new FTS5 + 15 robustness). The existing stop-search tests (Test 9 wildcards, Test 10 NaPTAN literal, Test 7/19 `MAX_RESOLVE` cap) stay green.

- [ ] **Step 9: Add the RUNBOOK note**

In `docs/RUNBOOK.md`, after the test-command code block (after the `e2e_sqlite.py` line, ~line 153), add:

```markdown
- `ensure_schema` now creates the `stops_fts` FTS5 index and backfills it once on upgrade (no manual step).
```

- [ ] **Step 10: Commit**

```bash
git add src/openbusdata_mcp/store.py test_delta.py docs/RUNBOOK.md
git commit -m "feat: FTS5 stop-name search with LIKE fallback (additive, in-place upgrade)"
```

---

### Task 6.2: True HTTP streaming of dataset zips

**Files:**
- Modify: `src/openbusdata_mcp/server.py` — add `import tempfile`; rewrite `_load_dataset` (311-345).
- Test: `test_robustness.py` — add `aiter_bytes` + async-context-manager to `_FakeResp` (13) and `_Resp` (69); add `stream` to `_FakeClient` (28) and the six zip-downloading clients (`_ZipFailClient` 78, `_ZipOkClient` 86, `_ZipJourneyClient` 132, `_CatClient` 102, `_DeltaFailClient` 140, `_PartialSweepClient` 158); append Test 22.

**Interfaces:**
- Consumes: `client.stream("GET", url)` async context manager with `resp.status_code` and `resp.aiter_bytes()`; `tempfile.mkstemp`; `os`; `_xml_contents` (unchanged, lazy generator reading from the open `ZipFile`).
- Produces: `_load_dataset(ds_id, force_reload=False, client=None) -> Optional[dict]` (unchanged signature). The zip is streamed to a seekable temp file; the temp file lives outside the per-dataset DB transaction.

- [ ] **Step 1: Write the failing test**

Append to `test_robustness.py` (after Test 21):

```python
# --- Test 22: the loader streams the zip to a temp file (get for meta)
class _StreamRecordingClient(_FakeClient):
    calls = []

    async def get(self, url):
        _StreamRecordingClient.calls.append(("get", url))
        if "/dataset/9/" in url:
            return _Resp(200, b'{"operatorName":"Op","url":"http://x/y.zip","modified":"2026-01-01T00:00:00Z"}')
        return _Resp(404, b"")

    async def stream(self, method, url):
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
```

- [ ] **Step 2: Run the new test to verify it fails**

Run: `uv run --with 'mcp<2' --with httpx --with pyyaml --with pytest pytest test_robustness.py::test_22_zip_download_streams -q`
Expected: FAIL — `assert result is not None` (the loader still calls `client.get` for the zip, which returns 404 for the zip URL, so `_load_dataset` returns None and `stream` is never recorded).

- [ ] **Step 3: Add the `tempfile` import**

In `src/openbusdata_mcp/server.py`, add `import tempfile` to the stdlib imports (e.g. after `import sys`, line 7).

- [ ] **Step 4: Rewrite `_load_dataset` to stream**

In `src/openbusdata_mcp/server.py`, replace the body of `_load_dataset` (lines 311-345) with:

```python
async def _load_dataset(ds_id: int, force_reload: bool = False,
                        client: Optional[httpx.AsyncClient] = None) -> Optional[dict]:
    """Returns meta on success, None on failure (caller counts errors).

    Reuses `client` if given; otherwise owns and closes a private one.
    The zip is streamed to a seekable temp file (never held in RAM whole).
    """
    owns = client is None
    if owns:
        client = httpx.AsyncClient(timeout=120.0, follow_redirects=True)
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".zip")
    os.close(tmp_fd)  # we only need the path; open it normally below
    z = None
    try:
        meta_resp = await client.get(f"{BASE_URL}/api/v1/dataset/{ds_id}/?api_key={API_KEY}")
        if meta_resp.status_code != 200:
            return None
        meta = meta_resp.json()

        operator = meta.get("operatorName", "Unknown")
        download_url = meta.get("url")
        if not download_url:
            return meta  # not a timetable dataset; nothing to load

        size = 0
        async with client.stream("GET", f"{download_url}?api_key={API_KEY}") as resp:
            if resp.status_code != 200:
                return None
            with open(tmp_path, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
                    size += len(chunk)
        if size < 100:
            return None

        try:
            z = zipfile.ZipFile(tmp_path)
            contents = _xml_contents(z)
        except zipfile.BadZipFile:
            # Some BODS datasets publish a bare TransXChange XML document
            # instead of a zip container.
            with open(tmp_path, "rb") as f:
                head = f.read(200)
            if head.lstrip().startswith(b"<?xml") or b"<TransXChange" in head:
                with open(tmp_path, "rb") as f:
                    contents = iter([f.read().decode("utf-8", errors="ignore")])
            else:
                return None

        already = ds_id in writer.loaded_ids()
        if already and not force_reload:
            return meta

        # ensure_schema commits, so it runs BEFORE the purge to keep purge +
        # rewrite inside the single per-dataset transaction closed below.
        writer.ensure_schema()
        try:
            if force_reload:
                writer.discard_dataset(ds_id)

            for content in contents:
                stops, routes, journeys = parse_transxchange(content, operator)
                for stop in stops:
                    writer.add_stop(stop.naptan, stop.name, stop.lat, stop.lon)
                for route in routes:
                    # Legacy (untagged) journeys for this op+route are superseded
                    # by this tagged write.
                    writer.discard_untagged_for(route.operator, route.route_num)
                    writer.upsert_route(route.operator, route.route_num,
                                        route.directions, route.stops, ds_id)
                for journey in journeys:
                    writer.add_journey(
                        journey.operator, journey.route_num, journey.direction,
                        journey.journey_code, journey.days,
                        [asdict(s) for s in journey.stops], ds_id)

            writer.mark_dataset_loaded(ds_id, meta.get("modified"), operator)
            writer.commit()  # one transaction per dataset: implicit checkpoint
        except Exception:
            # A failure mid-rewrite must not leave the purge uncommitted:
            # the next ensure_schema() would commit it, persisting a partial
            # purge. Roll the whole purge + rewrite back and re-raise.
            writer.conn.rollback()
            raise
        return meta
    finally:
        if z is not None:
            z.close()
        os.unlink(tmp_path)
        if owns:
            await client.aclose()
```

- [ ] **Step 5: Refactor the mock clients**

In `test_robustness.py`:

1. `_FakeResp` (line 13) — add after `json`:

```python
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_bytes(self):
        yield self.content
```

2. `_FakeClient` (line 28) — add after `get`:

```python
    async def stream(self, method, url):
        captured.append(url)
        return _FakeResp()
```

3. `_Resp` (line 69) — add after `json`:

```python
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_bytes(self):
        yield self.content
```

4. The six zip-downloading clients each gain a `stream` method (the zip now comes via `stream`; `get` serves only meta/catalogue):

```python
# _ZipFailClient: zip download 404s
    async def stream(self, method, url):
        return _Resp(404, b"")

# _ZipOkClient: zip streams OK
    async def stream(self, method, url):
        return _Resp(200, _make_zip(b"<TransXChange/>"))

# _ZipJourneyClient: zip streams the journey XML
    async def stream(self, method, url):
        return _Resp(200, _make_zip(_JOURNEY_XML))

# _CatClient: zip streams OK
    async def stream(self, method, url):
        return _Resp(200, _make_zip(b"<TransXChange/>"))

# _DeltaFailClient: every zip download 404s
    async def stream(self, method, url):
        return _Resp(404, b"")

# _PartialSweepClient: zip streams OK
    async def stream(self, method, url):
        return _Resp(200, _make_zip(b"<TransXChange/>"))
```

5. `_ZipOkClient.get` and `_ZipJourneyClient.get` — change their non-meta fallback from `_Resp(200, _make_zip(...))` to `_Resp(404, b"")` (the zip no longer comes via `get`). `_ZipFailClient.get`, `_CatClient.get`, `_DeltaFailClient.get`, `_PartialSweepClient.get` keep their current logic unchanged.

- [ ] **Step 6: Run the full suite to verify everything passes**

Run: `uv run --with 'mcp<2' --with httpx --with pyyaml --with pytest pytest -q`
Expected: PASS — all 40 tests (39 from Task 6.1 + the new Test 22). The existing robustness tests (soft failures, bare-XML fallback, mid-rewrite rollback, watermark, partial sweep) stay green against the new `stream` interface.

- [ ] **Step 7: Commit**

```bash
git add src/openbusdata_mcp/server.py test_robustness.py
git commit -m "feat: stream dataset zips to a seekable temp file instead of holding them in RAM"
```

---

### Task 6.3: test_20 planner robustness

**Files:**
- Modify: `test_delta.py` — Test 20 (line 235).
- Test: the converted Test 20 itself.

**Interfaces:**
- Consumes: `writer.add_journey(op, num, direction, code, days, stops, ds_id)` (store.py:481), the `stops()` helper (test_delta.py:8), the `writer` fixture.
- Produces: a Test 20 whose `EXPLAIN QUERY PLAN ... j_oproute` assertion is robust across SQLite versions.

- [ ] **Step 1: Seed enough journeys in Test 20**

In `test_delta.py`, replace the body of `test_20_discard_dataset_index_only_scan` (lines 235-241) with:

```python
def test_20_discard_dataset_index_only_scan(writer, store):
    # Seed enough journeys that the planner must choose the covering
    # j_oproute index for the DISTINCT scan (robust across SQLite versions).
    for i in range(300):
        writer.add_journey("Op", f"20-{i}", "outbound", f"J{i}", {"mon"}, stops(), 99)
    writer.commit()
    plan = writer.conn.execute(
        "EXPLAIN QUERY PLAN SELECT DISTINCT op, route FROM journeys").fetchall()
    assert any("j_oproute" in str(row) for row in plan), f"index not used: {plan}"
```

- [ ] **Step 2: Run Test 20 to verify it passes**

Run: `uv run --with 'mcp<2' --with httpx --with pyyaml --with pytest pytest test_delta.py::test_20_discard_dataset_index_only_scan -q`
Expected: PASS — the plan uses `j_oproute`.

- [ ] **Step 3: Run the full suite**

Run: `uv run --with 'mcp<2' --with httpx --with pyyaml --with pytest pytest -q`
Expected: PASS — all 40 tests.

- [ ] **Step 4: Commit**

```bash
git add test_delta.py
git commit -m "test: seed 300 journeys in test_20 so the j_oproute index assertion is robust"
```

---

## Self-Review

**Spec coverage:**
- §1.1 FTS5 schema (virtual table + 3 triggers) → Task 6.1 Step 4. Backfill keyed on `stops_fts_backfilled` with `INSERT OR REPLACE` idempotency → Task 6.1 Step 5.
- §1.2 `_fts_query` builder → Task 6.1 Step 3 (with the alphanumeric-token filter from Ruling 6.1-C).
- §1.3 `search_stops` (short-query LIKE, FTS5 `ORDER BY rank`, LIKE fallback) → Task 6.1 Step 6.
- §1.4 `resolve_stop` (NaPTAN literal, FTS5 capped at `MAX_RESOLVE`, LIKE fallback) → Task 6.1 Step 7.
- §1.5 documented semantics change (fallback guarantees no query that works today returns empty) → locked by Test 24's `kscr`/`%`/`_` assertions.
- §2.1 loader streaming (temp file, byte-count check, bare-XML fallback from temp file, `finally` close+unlink spanning the parse) → Task 6.2 Steps 3-4 (with the fd-leak fix from Ruling 6.1-B).
- §2.2 mock-client refactor (`aiter_bytes` + async-CM on `_FakeResp`/`_Resp`, `stream` on `_FakeClient` + six zip clients) → Task 6.2 Step 5.
- §3 test_20 seeding → Task 6.3 Step 1 (with distinct route numbers from Ruling 6.1-D).
- §4 RUNBOOK note → Task 6.1 Step 9.
- Testing strategy (a) FTS5 matching, (b) LIKE fallback, (c) trigger sync, (d) backfill + flag, (e) existing stop-search tests stay green → Test 23 + Test 24 + full-suite runs.

**Placeholder scan:** Every step has concrete code; no TBD/TODO; every test has an expected outcome.

**Type consistency:** `_fts_query` returns `Optional[str]` and is consumed as `if fts:` in both `search_stops` and `resolve_stop`. `search_stops`/`resolve_stop` signatures unchanged. `_load_dataset` signature unchanged. `_search_stops_like` is a new private method on `TimetableStore` (same class as `search_stops`). Mock `stream` methods all take `(self, method, url)` and return a `_Resp`/`_FakeResp` — matching the loader's `client.stream("GET", url)` call. `_make_zip` and `_JOURNEY_XML` are module-level in `test_robustness.py` and referenced by the new `stream` methods in the same file.
