# Phase 6 Design: FTS5 Stop Search + True HTTP Streaming

**Date:** 2026-09-10
**Status:** Approved design (user: "proceed")

## Goal

Ship the two deferred items from the Phase 1-5 code-health plan plus one parked test finding:

1. **FTS5 stop-name search** — replace the `LIKE '%...%'` scan in `search_stops`/`resolve_stop` with a full-text index, preserving today's behavior via a LIKE fallback.
2. **True HTTP streaming of dataset zips** — stop holding `zip_resp.content` (the whole zip) in RAM; stream to a seekable temp file.
3. **test_20 planner robustness** — seed enough journeys that the `EXPLAIN QUERY PLAN` index assertion is robust across SQLite versions.

## Global Constraints (inherited from the Phase 1-5 plan)

- Python `>=3.10`; **no new runtime dependencies** (FTS5 is built into SQLite; httpx streaming is built-in).
- **Tool output shapes must not change** — `search_stops` keeps returning `[{"naptan", "name"}]`; `resolve_stop` stays internal.
- **24:00+ post-midnight times must survive unclamped** — untouched by this phase.
- **Schema changes must be additive** — existing `index.db` files (3.48 GB in prod) upgrade in place, no rebuild.
- **One transaction per dataset** in the loader — the temp file lives outside the DB transaction; the parse loop stays inside the per-dataset transaction.
- **The delta watermark policy stays** — untouched by this phase.
- Tests run from the repo root; the store DB is redirected to a temp dir before any persistence.

## 1. FTS5 Stop-Name Search

### 1.1 Schema (additive, in-place upgrade)

In `TimetableWriter.ensure_schema` (`src/openbusdata_mcp/store.py:351`), add to the `executescript`:

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

- The FTS rowid mirrors `stops.rowid` (the `stops` table is a normal rowid table: `naptan TEXT PRIMARY KEY, name TEXT, lat REAL, lon REAL`).
- `add_stop` uses `INSERT ... ON CONFLICT(naptan) DO UPDATE` (`store.py:444-449`) — a new stop fires the INSERT trigger, a conflict fires the UPDATE trigger. Both are covered.
- `naptan UNINDEXED` keeps the naptan retrievable but out of the index.

**Backfill** (one-time, in-place upgrade for existing DBs), following the Phase 2 `journey_stops` pattern (`store.py:378-385`), keyed on a `stops_fts_backfilled` meta flag:

```python
if not self.conn.execute(
        "SELECT 1 FROM meta WHERE k='stops_fts_backfilled'").fetchone():
    self.conn.execute(
        "INSERT OR REPLACE INTO stops_fts(rowid, name, naptan) "
        "SELECT rowid, name, naptan FROM stops")
    self.conn.execute(
        "INSERT OR REPLACE INTO meta VALUES ('stops_fts_backfilled', '1')")
```

`INSERT OR REPLACE` makes the backfill idempotent against the rowid key even if the flag is ever missing with data present.

### 1.2 Query builder

A module-level helper (next to `_like_escape`, `store.py:14`) builds an FTS5 `MATCH` expression from user input:

```python
def _fts_query(query: str) -> Optional[str]:
    """Build an FTS5 MATCH expression from user input, or None if untokenizable."""
    tokens = [t for t in query.strip().lower().split() if t]
    if not tokens:
        return None
    return " AND ".join(f'"{t.replace(chr(34), chr(34) * 2)}"*' for t in tokens)
```

- Each token is wrapped in double quotes (literal — FTS5 special chars inside quotes are inert) with embedded quotes doubled (FTS5's escape), then `*` makes it a prefix query.
- "Oaks Cross" → `"oaks"* AND "cross"*`. "100%" → `"100%"*` (tokenizer sees `100`). Bare `%`, `_`, `\` → no tokens → `None` → LIKE fallback.

### 1.3 `search_stops` (user-facing tool)

```python
def search_stops(self, query: str, limit: int = 50) -> list[dict]:
    q = query.strip()
    if len(q) < 2:
        return self._search_stops_like(q, limit)          # short queries: LIKE directly
    fts = _fts_query(q)
    if fts:
        rows = self.conn.execute(
            "SELECT naptan, name FROM stops_fts WHERE stops_fts MATCH ? "
            "ORDER BY rank LIMIT ?", (fts, limit)).fetchall()
        if rows:
            return [{"naptan": n, "name": name} for n, name in rows]
    return self._search_stops_like(q, limit)              # FTS5 empty -> LIKE fallback
```

`_search_stops_like` is the current implementation (`store.py:79-84`) extracted verbatim. Output shape unchanged.

### 1.4 `resolve_stop` (internal, feeds route planning)

```python
def resolve_stop(self, stop_query: str) -> set[str]:
    q = stop_query.strip()
    if q.isdigit() or (len(q) >= 4 and q[:2].isdigit() and q.isalnum()):
        return {q}                                        # NaPTAN literal, unchanged
    fts = _fts_query(q)
    if fts:
        rows = self.conn.execute(
            "SELECT naptan FROM stops_fts WHERE stops_fts MATCH ? LIMIT ?",
            (fts, MAX_RESOLVE)).fetchall()
        if rows:
            return {r[0] for r in rows}
    return {r[0] for r in self.conn.execute(               # LIKE fallback, capped
        "SELECT naptan FROM stops WHERE LOWER(name) LIKE ? ESCAPE '\\' "
        f"LIMIT {MAX_RESOLVE}",
        (f"%{_like_escape(q.lower())}%",))}
```

The `MAX_RESOLVE` cap (SQLite 999-variable guard, Test 7/19) is preserved on both paths.

### 1.5 Documented semantics change

FTS5 is token + prefix matching. Compared to the current substring `LIKE`:

- **More permissive for prefixes:** `"road"*` matches "roadside"; `"common"* AND "road"*` matches "Common Roadside" where `LIKE '%common road%'` would not.
- **Less permissive for mid-token substrings:** query "kscr" won't match "Oakscross" via FTS5 — but then FTS5 returns nothing and the LIKE fallback runs, so **no query that works today returns empty**.
- Untokenizable input (bare `%`, `_`, `\`, whitespace) → `_fts_query` returns `None` → LIKE fallback, so the Test 9 wildcard-escaping assertions still pass unchanged.

## 2. True HTTP Streaming of Dataset Zips

### 2.1 Loader change (`src/openbusdata_mcp/server.py:311-345`)

Replace the `client.get(zip_url)` + `io.BytesIO(zip_resp.content)` path with streaming to a seekable temp file:

```python
tmp_fd, tmp_path = tempfile.mkstemp(suffix=".zip")
z = None
try:
    size = 0
    async with client.stream("GET", f"{download_url}?api_key={API_KEY}") as resp:
        if resp.status_code != 200:
            return None
        with os.fdopen(tmp_fd, "wb") as f:
            async for chunk in resp.aiter_bytes():
                f.write(chunk)
                size += len(chunk)
    if size < 100:
        return None
    try:
        z = zipfile.ZipFile(tmp_path)
        contents = _xml_contents(z)
    except zipfile.BadZipFile:
        with open(tmp_path, "rb") as f:
            head = f.read(200)
        if head.lstrip().startswith(b"<?xml") or b"<TransXChange" in head:
            with open(tmp_path, "rb") as f:
                contents = iter([f.read().decode("utf-8", errors="ignore")])
        else:
            return None
    # ... unchanged: already/force_reload check, ensure_schema, per-dataset
    # parse loop, mark_dataset_loaded + commit / rollback ...
finally:
    if z is not None:
        z.close()
    os.unlink(tmp_path)
```

- The temp file must outlive the parse loop — `_xml_contents` is a lazy generator reading from `z`, so the `finally` (close + unlink) wraps the whole parse, not just the download.
- The 100-byte minimum check becomes a byte-count check on the streamed size.
- The bare-XML fallback reads the head (and, on match, the full content) from the temp file instead of `zip_resp.content`.
- `tempfile` and `os` are already imported in `server.py`.
- The `owns`/`aclose` client lifecycle is unchanged.

### 2.2 Mock-client refactor (`test_robustness.py`)

The loader now calls `client.stream("GET", url)` for the zip and `client.get(url)` for the meta. The mock clients must support both:

- `_FakeResp` gains `async def aiter_bytes(self): yield self.content` and `__aenter__`/`__aexit__` (async context manager).
- `_FakeClient` (base) gains `async def stream(self, method, url)` returning `_FakeResp()` (mirrors `get`).
- `_Resp` gains `aiter_bytes` + `__aenter__`/`__aexit__`.
- The six zip-downloading clients (`_ZipFailClient`, `_ZipOkClient`, `_ZipJourneyClient`, `_CatClient`, `_DeltaFailClient`, `_PartialSweepClient`) gain a `stream` method returning the zip `_Resp` (or the 404 `_Resp` for `_ZipFailClient`); their `get` methods now only need to serve the meta/catalogue URLs.
- Tests 1/2/6 (dataset tool, live-buses) use `get` only — unaffected. Test 7 (`_CountingClient`) never reaches a zip download (meta has no `url`) — unaffected.

## 3. test_20 Planner Robustness

`test_delta.py` Test 20 currently seeds one journey before the `EXPLAIN QUERY PLAN ... j_oproute` assertion. Seed a few hundred (e.g. 300) journeys so SQLite's planner is forced to choose the covering index, making the assertion robust across SQLite versions. Assertion unchanged.

## 4. Docs

`docs/RUNBOOK.md`: add a one-line note that `ensure_schema` now creates the `stops_fts` FTS5 index and backfills it once on upgrade (no manual step). The test command is already pytest.

## Testing Strategy

- **FTS5:** new tests lock (a) FTS5 token+prefix matching and `rank` ordering, (b) the LIKE fallback fires when FTS5 returns nothing, (c) triggers keep `stops_fts` in sync on insert/update/delete, (d) the backfill populates `stops_fts` for a pre-existing `stops` table and never re-runs (flag), (e) all existing stop-search tests (Test 9 wildcards, Test 10 NaPTAN literal, Test 7/19 `MAX_RESOLVE` cap) stay green.
- **Streaming:** existing robustness tests (soft failures, bare-XML fallback, mid-rewrite rollback, watermark, partial sweep) stay green against the new mock `stream` interface; a new test asserts the loader streams (mock records `stream` was used for the zip URL and `get` for the meta).
- **test_20:** assertion unchanged, now on a many-row table.
- Full suite: `uv run --with 'mcp<2' --with httpx --with pyyaml --with pytest pytest -q` — all 37 existing + new tests pass.

## Out of Scope

- **Dependency pinning** (`mcp>=1.12` in `pyproject.toml`) — user declined for Phase 6.
- **FTS5 for the `routes` table** (`get_route_stops` still uses `LIKE` on `num`/`op`) — the deferred note names stop-name search only.
- **Changing the FTS5 semantics beyond the documented token+prefix+fallback** — the user chose "FTS5 + LIKE fallback" over pure FTS5.
