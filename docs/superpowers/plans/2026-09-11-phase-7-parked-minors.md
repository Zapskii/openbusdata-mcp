# Phase 7 Implementation Plan: Parked Phase 6 Minors

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the nine Minor findings parked by the Phase 6 whole-branch review, so the FTS5-search and streamed-download work from Phase 6 is left in a clean, documented, footgun-free state.

**Architecture:** Nine small, independent edits across three files — `src/openbusdata_mcp/store.py` (search semantics + docstring), `test_delta.py` (test fixes/strengthening), `test_robustness.py` (mock hardening), plus one `docs/RUNBOOK.md` semantics note. No schema changes, no behavior change to the tool contract. Batched into three tasks and dispatched as ONE implementer batch (SDD "batch small same-shape work" rule), then reviewed as one unit.

**Tech Stack:** Python `>=3.10`, stdlib `sqlite3` (FTS5), httpx, pytest. No new runtime dependencies.

**Spec:** No new spec — this is a cleanup of findings already-ruled in the Phase 6 ledger/final review. Source of truth for each item: the Phase 6 whole-branch review verdict (0 Critical / 0 Important / 9 Minor, all parked) and Rulings 6.1-A..C, 6.2-A recorded in the Phase 6 wrap-up.

## Global Constraints

- **No tool-output shape changes** — `search_stops`, `resolve_stop`, and the loader keep their exact contracts.
- **No schema changes** — `stops_fts`/triggers/backfill from Phase 6 stay as-is.
- **Suite must stay 40 passing** (22 delta + 18 robustness) — test-numbering stays within existing tests; no new numbered tests, only strengthened existing ones.
- Tests run from the repo root via `uv run --with 'mcp<2' --with httpx --with pyyaml --with pytest pytest -q`.

## The nine parked minors (from the Phase 6 ledger)

| # | Finding | Severity | Fix location |
|---|---|---|---|
| 1 | `\` untokenizable input untested in test_24 | Minor | `test_delta.py` test_24 |
| 2 | FTS5-first can return a strict subset of old LIKE results — worth a RUNBOOK semantics line | Minor (worth acting on) | `docs/RUNBOOK.md` |
| 3 | "common road" assertion in test_24 redundant; its comment factually wrong (LIKE *would* match "Common Roadside") | Minor (worth acting on) | `test_delta.py` test_24 |
| 4 | `_fts_query` docstring misattributes the token filter to FTS5 (Ruling 6.1-C filter is actually applied in Python) | Minor | `src/openbusdata_mcp/store.py` `_fts_query` |
| 5 | Base `_FakeClient.stream` returns 2-byte `b"{}"` content — latent trap for a future test that forgets to override | Minor | `test_robustness.py` `_FakeClient` |
| 6 | Mock `stream` methods ignore the `method` arg | Minor | `test_robustness.py` six subclass `stream` methods |
| 7 | Test 23's `mkdtemp()` dir never cleaned | Minor | `test_delta.py` test_23 |
| 8 | `store` fixture unused in test_20 | Minor | `test_delta.py` test_20 |
| 9 | Whitespace-only query now returns all stops (regression: pre-FTS `%%   %` required literal spaces; post-FTS `.strip()` → `""` → `%%` matches everything) | Minor | `src/openbusdata_mcp/store.py` `search_stops` |

**Confirmed regression (minor 9):** pre-Phase-6 `search_stops("   ")` built `LIKE '%%   %'` (three literal spaces), matching ~nothing. Post-Phase-6 it strips to `""` and routes to `_search_stops_like("")` → `LIKE '%%'` → **every stop**. Fix: empty-after-strip returns `[]`.

---

### Task 7.1: Search semantics (minors 2, 3, 4, 9)

**Files:**
- Modify: `src/openbusdata_mcp/store.py` — `_fts_query` docstring (line 18-24); `search_stops` (line 100-111).
- Modify: `test_delta.py` — test_24 (line 342-367).
- Modify: `docs/RUNBOOK.md` — after the existing FTS5 note (the `ensure_schema now creates the stops_fts` line).

**Interfaces:**
- Consumes: `_fts_query` (module-level, store.py:18), `_search_stops_like` (store.py:93), `writer.add_stop` fixture.
- Produces: `search_stops(query, limit=50)` — same signature, but whitespace-only → `[]`. `_fts_query` — same behavior, corrected docstring. RUNBOOK documents the FTS5 semantics.

- [ ] **Step 1: Write the failing tests** (edit existing tests in `test_delta.py`)

In test_24 `test_24_fts_search_and_like_fallback` (line 342), after the `S5` seeding add an `S6`:

```python
    writer.add_stop("S6", "Back\\slash Stop")
```

Replace the redundant "common road" block (lines 353-354) — the comment claims `LIKE '%common road%'` would not match "Common Roadside", which is false (`%common road%` IS a substring of "common roadside"), and the assertion is redundant with the FTS5-locking `"road common"` assertion that follows:

```python
    # FTS5 prefix matching is locked by the "road common" assertion below;
    # the redundant "common road" (LIKE would match) case is intentionally
    # not asserted separately.
```

After the existing `search_stops("_")` untokenizable assertion (line 362), add the missing `\` case (minor 1) and the whitespace regression (minor 9):

```python
    assert {s["naptan"] for s in store.search_stops("\\")} == {"S6"}, (
        "bare backslash escaped literally (LIKE fallback)")
    assert store.search_stops("   ") == [], (
        "whitespace-only query must return nothing, not all stops")
```

- [ ] **Step 2: Run the new assertions to verify they fail**

Run: `uv run --with 'mcp<2' --with httpx --with pyyaml --with pytest pytest test_delta.py::test_24_fts_search_and_like_fallback -q`
Expected: FAIL — the whitespace assertion fails (`search_stops("   ")` returns all 6 stops, not `[]`). The `\\` assertion may pass (LIKE fallback with no backslash-named stop → empty) if `_fts_query` already routes it; that's fine — the whitespace line is the RED signal.

- [ ] **Step 3: Implement the fixes**

In `src/openbusdata_mcp/store.py`, fix the `_fts_query` docstring (minor 4 — the filter is applied here in Python, not by FTS5):

```python
def _fts_query(query: str) -> Optional[str]:
    """Build an FTS5 MATCH expression from user input, or None if untokenizable.

    Each whitespace token becomes a quoted prefix term, AND-joined. Tokens with
    no alphanumeric characters (bare '%', '_', '\', quotes) are dropped before
    FTS5 sees them — they would match nothing meaningful — so if every token is
    dropped this returns None and the caller falls back to LIKE.
    """
```

In `search_stops`, add the empty-after-strip guard (minor 9):

```python
    def search_stops(self, query: str, limit: int = 50) -> list[dict]:
        q = query.strip()
        if not q:
            return []   # whitespace-only: nothing to search (regression fix)
        if len(q) < 2:
            return self._search_stops_like(q, limit)   # short queries: LIKE directly
```

(Rest of `search_stops` unchanged.)

In `docs/RUNBOOK.md`, replace the Phase 6 FTS5 note with one that documents the semantics (minor 2):

```markdown
- `ensure_schema` now creates the `stops_fts` FTS5 index, backfills it once on upgrade, and `search_stops`/`resolve_stop` query it first with a LIKE fallback. **Semantics:** FTS5 is token+prefix (AND of `"token"*` terms); a query can return a strict subset of what the old substring LIKE would have matched when FTS5 finds *something* (the fallback only runs when FTS5 returns nothing). Whitespace-only queries return nothing.
```

- [ ] **Step 4: Run the full suite**

Run: `uv run --with 'mcp<2' --with httpx --with pyyaml --with pytest pytest -q`
Expected: PASS — all 40 tests. Existing wildcard (test_9), NaPTAN literal (test_10), MAX_RESOLVE (test_19), and FTS5 (test_24) assertions stay green.

- [ ] **Step 5: Commit**

```bash
git add src/openbusdata_mcp/store.py test_delta.py docs/RUNBOOK.md
git commit -m "fix: search semantics — whitespace-only returns [], correct _fts_query docstring + test_24/RUNBOOK"
```

---

### Task 7.2: Test hygiene (minors 1, 7, 8)

**Files:**
- Modify: `test_delta.py` — test_23 (line 306-338), test_20 (line 235-243). Minor 1's `\` case is already handled in Task 7.1 Step 1; this task owns the fixtures.

**Interfaces:**
- Consumes: the `writer` fixture (conftest), pytest's built-in `tmp_path` fixture.
- Produces: test_23 leaves no temp dir behind; test_20 no longer requests an unused fixture.

- [ ] **Step 1: Make the failing-signal explicit** (these are RED-optional: the current code is *working*, just unhygienic — the RED is "test no longer relies on the abandoned pattern")

Run: `uv run --with 'mcp<2' --with httpx --with pyyaml --with pytest pytest test_delta.py::test_23_fts_backfill_and_trigger_sync -q` — currently passes (35 lines, all assertions green). That's the baseline.

- [ ] **Step 2: Implement the fixes**

In test_23 `test_23_fts_backfill_and_trigger_sync` (line 306), switch from `tempfile.mkdtemp()` to pytest's `tmp_path` (minor 7) — the fixture auto-cleans and the test no longer needs `Path`/`tempfile` imports for its own dir:

```python
def test_23_fts_backfill_and_trigger_sync(tmp_path):
    # Simulate a pre-FTS DB: stops present, no stops_fts, no backfill flag.
    _bk = tmp_path / "fts.db"
```

In test_20 `test_20_discard_dataset_index_only_scan` (line 235), drop the unused `store` fixture (minor 8):

```python
def test_20_discard_dataset_index_only_scan(writer):
    # Seed enough journeys that the planner must choose the covering
    # j_oproute index for the DISTINCT scan (robust across SQLite versions).
    for i in range(300):
        writer.add_journey("Op", f"20-{i}", "outbound", f"J{i}", {"mon"}, stops(), 99)
    ...
```

- [ ] **Step 3: Run the full suite**

Run: `uv run --with 'mcp<2' --with httpx --with pyyaml --with pytest pytest -q`
Expected: PASS — all 40 tests.

- [ ] **Step 4: Commit**

```bash
git add test_delta.py
git commit -m "test: use tmp_path in test_23, drop unused store fixture in test_20"
```

---

### Task 7.3: Mock-client hardening (minors 5, 6)

**Files:**
- Modify: `test_robustness.py` — `_FakeClient.stream` (line 54-56) and the six subclass `stream` methods (lines 107, 117, 136, 169, 206, 454).

**Interfaces:**
- Consumes: the loader's `client.stream("GET", url)` contract (real httpx `stream` is a regular method returning an async CM — Ruling 6.2-A).
- Produces: a base `_FakeClient.stream` that fails loudly instead of returning 2 garbage bytes; subclass `stream` methods that guard the HTTP method.

- [ ] **Step 1: Baseline** (RED-optional — current mocks are working, just trappy)

Run: `uv run --with 'mcp<2' --with httpx --with pyyaml --with pytest pytest test_robustness.py -q`
Expected: PASS — 18 tests.

- [ ] **Step 2: Implement the fixes**

In the base `_FakeClient` (line 54-56), replace the `return _FakeResp()` footgun (minor 5) with a loud failure:

```python
    def stream(self, method, url):
        captured.append(url)
        raise NotImplementedError(
            "zip-download tests must override stream() with a response that "
            "aiter_bytes()s a real zip; the base _FakeResp.content=b\"{}\" (2 bytes) "
            "is not a zip")
```

In each of the six subclass `stream` methods (`_ZipFailClient` 107, `_ZipOkClient` 117, `_ZipJourneyClient` 136, `_CatClient` 169, `_DeltaFailClient` 206, `_PartialSweepClient` 454), guard the method arg (minor 6) — the loader only ever calls `client.stream("GET", ...)`:

```python
    def stream(self, method, url):
        assert method == "GET", f"unexpected stream method: {method}"
        return _Resp(...)   # existing body unchanged
```

- [ ] **Step 3: Run the full suite**

Run: `uv run --with 'mcp<2' --with httpx --with pyyaml --with pytest pytest -q`
Expected: PASS — all 40 tests (every zip-downloading test goes through one of the six explicit `stream` overrides, so the base `raise` is never hit).

- [ ] **Step 4: Commit**

```bash
git add test_robustness.py
git commit -m "test: harden zip-download mock stream() — fail loudly on base, assert method"
```

---

## Self-Review

**Coverage of all nine minors:**
- 1 (`\` in test_24) → Task 7.1 Step 1
- 2 (RUNBOOK semantics) → Task 7.1 Step 3
- 3 (redundant/wrong "common road") → Task 7.1 Step 1
- 4 (`_fts_query` docstring) → Task 7.1 Step 3
- 5 (base stream 2-byte footgun) → Task 7.3 Step 2
- 6 (stream ignores method) → Task 7.3 Step 2
- 7 (test_23 mkdtemp) → Task 7.2 Step 2
- 8 (test_20 unused store) → Task 7.2 Step 2
- 9 (whitespace regression) → Task 7.1 Steps 1+3
- **Placeholder scan:** every step has concrete code with exact anchors; no TBD/TODO.
- **Type/name consistency:** `_fts_query`, `_search_stops_like`, `search_stops`, the `writer` fixture, and the six mock-class names all match the checked-in code (verified against `store.py:18,93,100` and `test_robustness.py` line numbers above). `tmp_path` is pytest built-in (no import). The whitespace guard returns `[]` — consistent with the "no query that works today returns empty" guarantee being relaxed only for blank input.
- **Regression check on minor 9:** `search_stops(" ")` pre-fix matched ~nothing (had to contain 3 literal spaces); returning `[]` is strictly closer to old behavior than the current all-stops. No other caller relies on whitespace returning everything (grep: only MCP tool `search_stops` + tests).
