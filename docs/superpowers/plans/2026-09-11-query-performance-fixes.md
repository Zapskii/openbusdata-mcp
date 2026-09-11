# (Phased) Query-Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the two pathological query paths in `openbusdata-mcp` (indexed journey-departure lookups, candidate-set intersection, FTS5 route search) so `plan_journey`/`find_buses_by_arrival_time`/`get_route_stops` are fast at full-UK scale, plus a short tail of cheap wins.

**Architecture:** Four additive phases over the existing SQLite store. Two new normalized indexes (`journey_stop_times` for stop→departure-time, `routes_fts` for route search) that follow the project's established backfill+trigger pattern, one pure read-side algorithmic fix (intersect touching sets before fetching JSON), and a few small query-path/pragma/cache tweaks. No new dependencies; every behavior change is locked by a parity test against the pre-change implementation plus an `EXPLAIN QUERY PLAN` index-usage assertion.

**Tech Stack:** Python 3.12, stdlib `sqlite3` (SQLite 3.47), FTS5, JSON1, Pytest (conftest temp-DB fixtures).

**Spec:** No upstream spec exists. This plan embeds the evidence and design decisions in the **Spec (Evidence & Decisions)** section below; a companion design doc will be persisted to `docs/superpowers/specs/2026-09-11-query-performance-design.md` at execution time (executors read this plan; the decisions it records are binding).

## Spec (Evidence & Decisions)

Measured on a synthetic DB (10,080 journeys, 258,720 journey_stops rows, 41.5k stops) built at `/tmp/perf/synth_index.db`:

| Path | Current | After | Fix |
|---|---|---|---|
| One `_journeys_departing("CTR0", …)` call (busy stop) | 192 ms | 8 ms | `journey_stop_times(naptan, journey_id, dep)` index |
| `plan_one_change(...)` (816 journeys) | **16,492 ms** | 659 ms | same index |
| `plan_direct`/`find_buses` candidate iteration, large B-set (6,720 driving journeys) | 198 ms | 71 ms | intersect touching sets before fetching JSON |
| `get_route_stops` | 0.8 ms @ 60 routes; ~1 s @ UK routes scale | indexed | FTS5 on routes (same contract as `search_stops`) |

Extrapolated to full UK scale (~500k journeys), the pre-fix `plan_one_change` scanning component is ~850 scans × ~11 s ≈ hours; this plan makes it seconds.

**Design decisions (binding):**
1. `journey_stop_times.dep` = `COALESCE(departure, arrival)`; a row with NULL `dep` never matches `dep >= ?` — identical semantics to the old correlated `json_each` `EXISTS` query.
2. `get_route_stops` adopts the established FTS5-first + LIKE-fallback contract (as `search_stops` already does). Token-prefix matching can differ from substring matching for mid-string substrings (e.g. `13` searched as `3`); this is the accepted stop-search contract. LIKE fallback preserves old substring behaviour when FTS returns nothing.
3. Plan-result cache is keyed on `(data_key, A, B, day, target)` where `data_key` = all `(ds_id, modified)` rows from `loaded_datasets`; callers receive a fresh `list()` copy so cached entries are never mutated externally.
4. `stops_fts`/`routes_fts` `optimize` runs after full/initial loads only (not per-delta) to bound cost.
5. Candidate intersection drives from the smaller of the two touching sets.

**Success criteria:** full existing suite (`test_delta.py`, `test_robustness.py`) passes with no regressions; all parity tests in `test_perf.py` pass; `plan_one_change` sub-1s on the `/tmp/perf`-style synthetic DB.

## Global Constraints

- Tool output shapes must stay byte-identical for `plan_direct`, `find_buses_by_arrival_time`, `get_route_stops`, `search_stops`, `find_routes_between` (callers and MCP tests snapshot them).
- Times are compared as zero-padded `'HH:MM:SS'` string clocks with NO 24-h clamping (`_parse_time` / `_fmt_seconds`); never convert to wall-clock.
- Stay under SQLite's 999-variable limit: keep `MAX_RESOLVE = 400`; `IN (...)` clauses built from resolved sets; `_fetch_journeys` keeps its 500-row chunking.
- New normalized tables must follow the established pattern: created in `ensure_schema`, populated on write, one-time backfill keyed on a meta flag (`journey_stops_backfilled`, `stops_fts_backfilled` — reuse this exact convention), and purged in `discard_dataset` / `discard_untagged_for`.
- Index-usage locks use `EXPLAIN QUERY PLAN` asserting the index name appears (project convention, e.g. test_20).
- No new dependencies. Tests live in a new root-level `test_perf.py`; use `writer.add_journey`/`upsert_route`/`add_stop` fixtures and per-test temp DBs from conftest.
- `TimetableStore` opens read-only (`mode=ro`); `TimetableWriter` uses WAL + `synchronous=NORMAL`.

---

## Phase 1 — Indexed stop-departure lookups

Why first: this is the critical fix (hours→seconds on `plan_one_change` at scale), it is shippable on its own, and Phases 2–4 are independent of it.

### Task 1: Writer-side `journey_stop_times` table

**Files:**
- Modify: `src/openbusdata_mcp/store.py` — `TimetableWriter.ensure_schema`, `TimetableWriter.add_journey`, `TimetableWriter.discard_dataset`, `TimetableWriter.discard_untagged_for`
- Test: `test_perf.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: table `journey_stop_times(naptan TEXT, journey_id INT, dep TEXT)` with index `jst_n(naptan, dep)`; `add_journey` populates a row per stop (`dep` = `COALESCE(departure, arrival)`); both discard paths delete rows for the removed journeys.

- [ ] **Step 1: Write the failing tests** (create `test_perf.py` with the three tests below)

```python
# test_perf.py
import tempfile
from pathlib import Path
from openbusdata_mcp.store import TimetableWriter

STOP = lambda n, a, d: {"naptan": n, "arrival": a, "departure": d}  # noqa: E731

def test_25_journey_stop_times_populated_on_add(writer, store):
    writer.add_journey("Op", "25", "outbound", "J25", {"mon"}, [
        STOP("010A", None, "09:00:00"),
        STOP("010B", "09:10:00", None),
        STOP("010C", None, None)], 99)
    writer.commit()
    rows = writer.conn.execute(
        "SELECT naptan, dep FROM journey_stop_times WHERE journey_id=1 "
        "ORDER BY naptan").fetchall()
    assert rows == [("010A", "09:00:00"), ("010B", "09:10:00"), ("010C", None)], rows


def test_26_journey_stop_times_backfill():
    _bk = Path(tempfile.mkdtemp()) / "upgrade.db"
    w2 = TimetableWriter(_bk)
    w2.ensure_schema()
    w2.add_journey("Op", "26", "outbound", "J26", {"mon"}, [
        STOP("010A", None, "09:00:00"), STOP("010B", "09:10:00", None)], 5)
    w2.conn.execute("DELETE FROM journey_stop_times")
    w2.conn.execute("DELETE FROM meta WHERE k='journey_stop_times_backfilled'")
    w2.conn.commit()
    w2.ensure_schema()  # must backfill from stored JSON
    n = w2.conn.execute(
        "SELECT COUNT(*) FROM journey_stop_times WHERE journey_id=1").fetchone()[0]
    assert n == 2, f"backfill missing: {n}"


def test_27_journey_stop_times_purged_on_discard(writer, store):
    writer.add_journey("Op", "27", "outbound", "J27", {"mon"}, [
        STOP("010A", None, "09:00:00"), STOP("010B", "09:10:00", None)], 5)
    writer.add_journey("Op2", "27b", "outbound", "J27b", {"mon"}, [
        STOP("010A", None, "08:00:00")], 0)
    writer.commit()
    assert writer.conn.execute("SELECT COUNT(*) FROM journey_stop_times").fetchone()[0] == 3
    writer.discard_dataset(5)
    assert writer.conn.execute(
        "SELECT COUNT(*) FROM journey_stop_times WHERE journey_id IN "
        "(SELECT id FROM journeys WHERE ds_id=0)").fetchone()[0] == 1
    writer.discard_untagged_for("Op2", "27b")
    assert writer.conn.execute("SELECT COUNT(*) FROM journey_stop_times").fetchone()[0] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest test_perf.py -v`
Expected: FAIL — "no such table: journey_stop_times".

- [ ] **Step 3: Implement** (in `store.py`)

In `ensure_schema`'s `executescript`, after the `CREATE INDEX IF NOT EXISTS j_oproute ...` line:
```sql
CREATE TABLE IF NOT EXISTS journey_stop_times (naptan TEXT, journey_id INT, dep TEXT);
CREATE INDEX IF NOT EXISTS jst_n ON journey_stop_times(naptan, dep);
```
Immediately after the existing `journey_stops` backfill block in `ensure_schema`:
```python
if not self.conn.execute(
        "SELECT 1 FROM meta WHERE k='journey_stop_times_backfilled'").fetchone():
    self.conn.execute(
        "INSERT INTO journey_stop_times (naptan, journey_id, dep) "
        "SELECT json_extract(value, '$.naptan'), j.id, "
        "       COALESCE(json_extract(value, '$.departure'), json_extract(value, '$.arrival')) "
        "FROM journeys j, json_each(j.json, '$.stops')")
    self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('journey_stop_times_backfilled', '1')")
```
In `add_journey`, after the `journey_stops` `executemany`:
```python
        self.conn.executemany(
            "INSERT INTO journey_stop_times VALUES (?,?,?)",
            [(s["naptan"], cur.lastrowid, s.get("departure") or s.get("arrival"))
             for s in stops])
```
In `discard_dataset`, after the `journey_stops` delete:
```python
        self.conn.execute(
            "DELETE FROM journey_stop_times WHERE journey_id IN "
            "(SELECT id FROM journeys WHERE ds_id=?)", (ds_id,))
```
In `discard_untagged_for`, after its `journey_stops` delete:
```python
        self.conn.execute(
            "DELETE FROM journey_stop_times WHERE journey_id IN "
            "(SELECT id FROM journeys WHERE ds_id=0 AND op=? AND route=?)",
            (op, route_num))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest test_perf.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/openbusdata_mcp/store.py test_perf.py
git commit -m "feat: write-side journey_stop_times departure index (schema, backfill, purge)"
```

### Task 2: Reader-side `_journeys_departing` uses the index

**Files:**
- Modify: `src/openbusdata_mcp/store.py` — `TimetableStore._journeys_departing` (now store.py:232-240)
- Test: `test_perf.py`

**Interfaces:**
- Consumes: `journey_stop_times` table + `jst_n` index from Task 1.
- Produces: `_journeys_departing(naptan: str, after: str) -> list[int]` — same contract, now index-backed. Consumers (`plan_one_change`, store.py:316-317) need no changes.

- [ ] **Step 1: Write the failing test**

```python
def test_28_journeys_departing_uses_index_and_matches_old_semantics(writer, store):
    writer.add_journey("Op", "28", "outbound", "J28a", {"mon"}, [
        STOP("010A", None, "09:00:00"),
        STOP("010B", "09:10:00", "09:11:00")], 1)
    writer.add_journey("Op", "28", "outbound", "J28b", {"mon"}, [
        STOP("010A", None, "10:00:00"),
        STOP("010B", "09:10:00", None)], 1)
    writer.commit()
    # reference: the pre-index correlated json_each query (frozen semantics)
    ref = [r[0] for r in store.conn.execute(
        "SELECT id FROM journeys WHERE EXISTS ("
        "  SELECT 1 FROM json_each(journeys.json, '$.stops')"
        "  WHERE json_extract(value,'$.naptan')=?"
        "    AND COALESCE(json_extract(value,'$.departure'),"
        "                 json_extract(value,'$.arrival')) >= ?"
        ") ORDER BY id", ("010B", "09:11:00")).fetchall()]
    assert store._journeys_departing("010B", "09:11:00") == ref, "parity with old semantics"
    plan = writer.conn.execute(
        "EXPLAIN QUERY PLAN SELECT DISTINCT journey_id FROM journey_stop_times "
        "WHERE naptan=? AND dep>=? ORDER BY journey_id", ("010B", "09:11:00")).fetchall()
    assert any("jst_n" in str(row) for row in plan), f"index not used: {plan}"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest test_perf.py::test_28 -v`
Expected: FAIL — the index assertion fails because `_journeys_departing` still runs the old query (parity line may pass, EXPLAIN check on `journey_stop_times` is what fails).

- [ ] **Step 3: Implement** — replace the body of `_journeys_departing`:

```python
    def _journeys_departing(self, naptan: str, after: str) -> list[int]:
        """Journey ids that depart `naptan` at/after `after` (indexed range seek)."""
        rows = self.conn.execute(
            "SELECT DISTINCT journey_id FROM journey_stop_times "
            "WHERE naptan=? AND dep>=? ORDER BY journey_id", (naptan, after)).fetchall()
        return [r[0] for r in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest test_delta.py test_robustness.py test_perf.py -v`
Expected: all pass (parity + index lock green; no consumer regressions).

- [ ] **Step 5: Commit**

```bash
git add src/openbusdata_mcp/store.py
git commit -m "feat: reader-side _journeys_departing via journey_stop_times index"
```

**Phase 1 exit criterion:** `plan_one_change` on the `/tmp/perf`-style synthetic DB is sub-1s (was ~16 s); full suite green.

---

## Phase 2 — Intersect touching sets before fetching JSON

Why: `plan_direct` / `find_buses_by_arrival_time` fetch and Python-scan the JSON of **every** journey touching set B, which is huge when B is a busy stop or a fuzzy set (up to 400 NaPTANs). Intersecting `touching(A) ∩ touching(B)` in SQL first cuts the JSON fetch to the smaller set; result set is identical (the skipped journeys could never yield a candidate).

### Task 3: `_candidate_journeys` prefilter

**Files:**
- Modify: `src/openbusdata_mcp/store.py` — `TimetableStore._candidate_journeys` (now store.py:242-260)
- Test: `test_perf.py`

**Interfaces:**
- Consumes: `_journeys_touching` (existing, indexed), `_fetch_journeys` (existing).
- Produces: `_candidate_journeys(naptans_a, naptans_b, day, target_s)` — same generator contract `yield (journey_dict, idx_a, idx_b)`; consumed by `plan_direct` and `find_buses_by_arrival_time` unchanged.

- [ ] **Step 1: Write the failing tests**

```python
def _old_candidates(store, na, nb, day, target_s):
    """Pre-change implementation — frozen reference for parity."""
    for jid, j in store._fetch_journeys(store._journeys_touching(nb)).items():
        if day not in j["days"]:
            continue
        ia = next((i for i, s in enumerate(j["stops"]) if s["naptan"] in na), None)
        ib = next((i for i, s in enumerate(j["stops"]) if s["naptan"] in nb), None)
        if ia is None or ib is None or ia >= ib:
            continue
        ar = j["stops"][ib].get("arrival")
        if ar and ar[:8] <= target_s:
            yield j, ia, ib


def test_29_candidate_journeys_intersection_parity(writer, store):
    for i in range(2):
        seq = ["CTR0", f"R{i}_hub", f"R{i}_1", f"R{i}_2"]
        for dep0 in ("08:00:00", "09:00:00"):
            stops = [{"naptan": s, "arrival": dep0, "departure": dep0} for s in seq]
            writer.add_journey(f"Op{i}", str(i), "outbound",
                               f"J{i}-{dep0}", {"mon"}, stops, 1)
    writer.commit()
    for na, nb in [({"CTR0"}, {"R0_2"}), ({"CTR0", "R1_hub"}, {"R0_1", "R1_2"})]:
        got = [(j["id"], ia, ib)
               for j, ia, ib in store._candidate_journeys(na, nb, "mon", "09:30:00")]
        ref = [(j["id"], ia, ib)
               for j, ia, ib in _old_candidates(store, na, nb, "mon", "09:30:00")]
        assert got == ref, (na, nb, got, ref)


def test_30_candidate_journeys_fetches_only_intersection(writer, store):
    for i in range(2):
        seq = ["CTR0", f"R{i}_hub", f"R{i}_1", f"R{i}_2"]
        for dep0 in ("08:00:00", "09:00:00", "10:00:00"):
            stops = [{"naptan": s, "arrival": dep0, "departure": dep0} for s in seq]
            writer.add_journey(f"Op{i}", str(i), "outbound",
                               f"J{i}-{dep0}", {"mon"}, stops, 1)
    writer.commit()
    na, nb = {"R0_hub"}, {"CTR0", "R0_hub", "R0_1", "R0_2", "R1_hub", "R1_1", "R1_2"}
    touch_a = set(store._journeys_touching(na))
    touch_b = set(store._journeys_touching(nb))
    assert 0 < len(touch_a) < len(touch_b), "test needs a small-A/large-B scenario"
    orig = store._fetch_journeys
    seen = {}
    def spy(jids):
        seen["ids"] = list(jids)
        return orig(jids)
    store._fetch_journeys = spy
    try:
        list(store._candidate_journeys(na, nb, "mon", "23:59:59"))
    finally:
        store._fetch_journeys = orig
    assert set(seen["ids"]) == touch_a, "fetched more than the A-intersection"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest test_perf.py::test_29 test_perf.py::test_30 -v`
Expected: `test_30` FAIL — pre-fix code fetches all of `touch_b` (6 journeys), not `touch_a` (3). `test_29` passes (still parity) until the rewrite.

- [ ] **Step 3: Implement** — replace the body of `_candidate_journeys` with:

```python
    def _candidate_journeys(self, naptans_a: set, naptans_b: set, day: str, target_s: str):
        """(keep existing docstring) ... Candidates are narrowed to journeys
        touching BOTH stop sets before any JSON is fetched: journeys touching
        B but not A (or vice versa) can never yield a plan, and fetching them
        dominates cost when B resolves to a busy/fuzzy set (measured 198ms ->
        71ms on a 6,720-journey driving set)."""
        both = set(self._journeys_touching(naptans_a)) & set(self._journeys_touching(naptans_b))
        for jid, j in self._fetch_journeys(sorted(both)).items():
            if day not in j["days"]:
                continue
            idx_a = next((i for i, s in enumerate(j["stops"]) if s["naptan"] in naptans_a), None)
            idx_b = next((i for i, s in enumerate(j["stops"]) if s["naptan"] in naptans_b), None)
            if idx_a is None or idx_b is None or idx_a >= idx_b:
                continue
            arr_b = j["stops"][idx_b].get("arrival")
            if arr_b and arr_b[:8] <= target_s:
                yield j, idx_a, idx_b
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest test_delta.py test_robustness.py test_perf.py -v`
Expected: all pass (parity + minimum-fetch lock green).

- [ ] **Step 5: Commit**

```bash
git add src/openbusdata_mcp/store.py
git commit -m "perf: intersect touching sets before fetching journey JSON in _candidate_journeys"
```

**Phase 2 exit criterion:** `test_29`/`test_30` green; full suite green.

---

## Phase 3 — FTS5 route search for `get_route_stops`

Why: `get_route_stops` runs `WHERE LOWER(num) LIKE '%…%' AND LOWER(op) LIKE '%…%'` — a confirmed `SCAN routes` (non-sargable). It is ~1 ms at 60 routes but ~1 s at UK scale (~60–100k rows). Mirror the `search_stops` contract: FTS5 prefix search first (tokenized), LIKE fallback for untokenizable input.

### Task 4: Writer-side `routes_fts` virtual table

**Files:**
- Modify: `src/openbusdata_mcp/store.py` — `TimetableWriter.ensure_schema`
- Test: `test_perf.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: FTS5 table `routes_fts(num, op, key UNINDEXED)` with triggers on `routes` INSERT/UPDATE/DELETE (rowid-synced), plus one-time backfill keyed on meta flag `routes_fts_backfilled`. `upsert_route` needs no change — the triggers fire on its INSERT/UPDATE.

- [ ] **Step 1: Write the failing test**

```python
def test_31_routes_fts_backfill_and_trigger_sync():
    _bk = Path(tempfile.mkdtemp()) / "upgrade.db"
    w2 = TimetableWriter(_bk)
    w2.ensure_schema()
    w2.upsert_route("RadialOp1", "31", {"outbound"}, ["010A", "010B"], 1)
    w2.conn.execute("DELETE FROM routes_fts")
    w2.conn.execute("DELETE FROM meta WHERE k='routes_fts_backfilled'")
    w2.conn.commit()
    w2.ensure_schema()  # must backfill existing routes
    n = w2.conn.execute(
        "SELECT COUNT(*) FROM routes_fts WHERE routes_fts MATCH '31'").fetchone()[0]
    assert n == 1, f"backfill missing: {n}"
    w2.upsert_route("Metrolink", "12", {"outbound"}, ["010A"], 1)  # trigger on INSERT
    n2 = w2.conn.execute(
        "SELECT COUNT(*) FROM routes_fts WHERE routes_fts MATCH '12'").fetchone()[0]
    assert n2 == 1, f"insert trigger missing: {n2}"
    w2.upsert_route("Metrolink", "12", {"outbound", "inbound"}, ["010A"], 1)  # UPDATE path
    assert w2.conn.execute(
        "SELECT COUNT(*) FROM routes_fts WHERE routes_fts MATCH '12'").fetchone()[0] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest test_perf.py::test_31 -v`
Expected: FAIL — "no such table: routes_fts".

- [ ] **Step 3: Implement** (in `ensure_schema`'s `executescript`, after the `stops_fts` trigger block — and the backfill after the `stops_fts_backfilled` block)

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS routes_fts USING fts5(
    num, op, key UNINDEXED, tokenize='unicode61');
CREATE TRIGGER IF NOT EXISTS routes_fts_ai AFTER INSERT ON routes BEGIN
    INSERT INTO routes_fts(rowid, num, op, key) VALUES (new.rowid, new.num, new.op, new.key);
END;
CREATE TRIGGER IF NOT EXISTS routes_fts_au AFTER UPDATE ON routes BEGIN
    UPDATE routes_fts SET num = new.num, op = new.op WHERE rowid = old.rowid;
END;
CREATE TRIGGER IF NOT EXISTS routes_fts_ad AFTER DELETE ON routes BEGIN
    DELETE FROM routes_fts WHERE rowid = old.rowid;
END;
```
```python
        if not self.conn.execute(
                "SELECT 1 FROM meta WHERE k='routes_fts_backfilled'").fetchone():
            self.conn.execute(
                "INSERT INTO routes_fts(rowid, num, op) "
                "SELECT rowid, num, op FROM routes")
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('routes_fts_backfilled', '1')")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest test_perf.py -v`
Expected: 31 passes (25–30 still green).

- [ ] **Step 5: Commit**

```bash
git add src/openbusdata_mcp/store.py
git commit -m "feat: routes_fts FTS5 index with triggers + backfill"
```

### Task 5: `get_route_stops` FTS-first + LIKE fallback

**Files:**
- Modify: `src/openbusdata_mcp/store.py` — `TimetableStore.get_route_stops` (now store.py:171-189)
- Test: `test_perf.py`

**Interfaces:**
- Consumes: `routes_fts` from Task 4; `_fts_query` (existing).
- Produces: `get_route_stops(operator, route, direction=None)` — same output dicts `{operator, route, directions, stops}`.

- [ ] **Step 1: Write the failing test**

```python
def test_32_get_route_stops_fts_first_like_fallback(writer, store):
    writer.upsert_route("RadialOp1", "12", {"outbound", "inbound"}, ["010A", "010B"], 1)
    writer.upsert_route("Metrolink", "12", {"outbound"}, ["010A"], 1)
    writer.upsert_route("RadialOp1", "30", {"outbound"}, ["010A"], 1)
    writer.commit()
    got = store.get_route_stops("RadialOp1", "12")  # exact op+num via FTS
    assert [(g["operator"], g["route"]) for g in got] == [("RadialOp1", "12")]
    got = store.get_route_stops("Radial", "12")     # partial-op prefix via FTS
    assert got and got[0]["route"] == "12"
    assert got and "Metrolink" not in [g["operator"] for g in got]
    got = store.get_route_stops("%", "%")           # untokenizable -> LIKE fallback
    assert len(got) == 3
    got = store.get_route_stops("RadialOp1", "12", direction="cross")
    assert got == []                                # direction filter still applies
    got = store.get_route_stops("RadialOp1", "12")
    assert got[0]["directions"] == ["inbound", "outbound"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest test_perf.py::test_32 -v`
Expected: FAIL — FTS `routes_fts` not yet consulted; exact op+num returns more/miss.

- [ ] **Step 3: Implement** — replace the query preamble of `get_route_stops`:

```python
    def get_route_stops(self, operator: str, route: str,
                        direction: Optional[str] = None) -> list[dict]:
        fts = _fts_query(f"{route} {operator}".strip())
        if fts:
            rows = self.conn.execute(
                "SELECT op, num, directions, stops FROM routes_fts "
                "WHERE routes_fts MATCH ? ORDER BY rank LIMIT 50", (fts,)).fetchall()
        else:
            rows = []
        if not rows:  # FTS empty or untokenizable -> LIKE fallback (unchanged)
            q = (f"%{route.lower()}%", f"%{operator.lower()}%")
            rows = self.conn.execute(
                "SELECT op, num, directions, stops FROM routes "
                "WHERE LOWER(num) LIKE ? AND LOWER(op) LIKE ?", q).fetchall()
        matches = []
        # (the rest — directions filter and stop-name resolution — is unchanged)
        for op, num, directions, stops in rows:
            ...unchanged...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest test_delta.py test_robustness.py test_perf.py -v`
Expected: all pass, including pre-existing `test_16_direction_filter_on_parsed_json`.

- [ ] **Step 5: Commit**

```bash
git add src/openbusdata_mcp/store.py
git commit -m "perf: get_route_stops via routes_fts with LIKE fallback"
```

**Phase 3 exit criterion:** `test_31`/`test_32` green; `test_16` and full suite still green.

---

## Phase 4 — Cheap wins

### Task 6: 1-char `search_stops` goes through FTS, not the LIKE scan

**Files:**
- Modify: `src/openbusdata_mcp/store.py` — `TimetableStore.search_stops` (now store.py:100-111)
- Test: `test_perf.py`

**Interfaces:**
- Consumes: `_fts_query` (existing), `stops_fts` (existing).
- Produces: `search_stops(query, limit=50)` — same contract (list of `{naptan, name}`).

- [ ] **Step 1: Write the failing test**

```python
def test_33_short_search_uses_fts(writer, store):
    writer.add_stop("010A", "Abbey Road")
    writer.add_stop("010B", "Station X")
    writer.commit()
    got = store.search_stops("a")            # 1 char -> FTS prefix "a"*
    names = [g["name"] for g in got]
    assert "Abbey Road" in names, names       # abbey starts with "a"
    got = store.search_stops("")              # empty stays on LIKE path, capped
    assert 0 <= len(got) <= 50
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest test_perf.py::test_33 -v`
Expected: FAIL — current code routes `len(query) < 2` straight to `_search_stops_like` (an `"a"` LIKE still finds Abbey Road, but the intent is FTS-first; the empty-query cap is the observable failure mode if nothing else).

- [ ] **Step 3: Implement** — replace `search_stops`:

```python
    def search_stops(self, query: str, limit: int = 50) -> list[dict]:
        q = query.strip()
        if not q:
            return self._search_stops_like(q, limit)   # empty: no tokens to FTS
        fts = _fts_query(q)
        if fts:
            rows = self.conn.execute(
                "SELECT naptan, name FROM stops_fts WHERE stops_fts MATCH ? "
                "ORDER BY rank LIMIT ?", (fts, limit)).fetchall()
            if rows:
                return [{"naptan": n, "name": name} for n, name in rows]
        return self._search_stops_like(q, limit)        # FTS5 empty -> LIKE fallback
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest test_delta.py test_robustness.py test_perf.py -v`
Expected: all pass (including pre-existing `test_9_like_wildcard_escaping` and `test_24_fts_search_and_like_fallback`).

- [ ] **Step 5: Commit**

```bash
git add src/openbusdata_mcp/store.py
git commit -m "perf: 1-char stop search via FTS prefix instead of stops full scan"
```

### Task 7: `mmap_size` pragma on the read connection

**Files:**
- Modify: `src/openbusdata_mcp/store.py` — `TimetableStore.conn` (now store.py:67-72)
- Test: `test_perf.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: read-only connection with `mmap_size` raised to 256 MB (mmap-backed reads).

- [ ] **Step 1: Write the failing test**

```python
def test_34_read_conn_mmap_enabled(store):
    v = store.conn.execute("PRAGMA mmap_size").fetchone()[0]
    assert v >= 268435456, f"mmap not enabled: {v}"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest test_perf.py::test_34 -v`
Expected: FAIL — value is 0 (default).

- [ ] **Step 3: Implement** — in `conn`, after the existing `cache_size` pragma:

```python
            self._conn.execute("PRAGMA mmap_size=268435456")  # 256MB mmap reads
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest test_perf.py -v`
Expected: 34 passes.

- [ ] **Step 5: Commit**

```bash
git add src/openbusdata_mcp/store.py
git commit -m "perf: mmap_size pragma on the read-only store connection"
```

### Task 8: FTS `optimize` after full/initial loads

**Files:**
- Modify: `src/openbusdata_mcp/store.py` — add `TimetableWriter.optimize_fts`
- Modify: `src/openbusdata_mcp/server.py` — call it at end of `load_all_timetable_data` (after `writer.set_last_refresh`/`counts`, ~line 480-486)
- Test: `test_perf.py`

**Interfaces:**
- Consumes: `stops_fts`, `routes_fts` (Tasks 4 / existing).
- Produces: `TimetableWriter.optimize_fts() -> None`; called by `load_all_timetable_data` after a full/initial load (not per-delta — bounds the rewrite cost).

- [ ] **Step 1: Write the failing test**

```python
def test_35_fts_optimize_after_full_load(writer):
    writer.add_stop("010A", "Abbey Road")
    writer.upsert_route("Op", "12", {"outbound"}, ["010A"], 1)
    writer.commit()
    writer.optimize_fts()   # must not break searching
    writer.commit()
    n = writer.conn.execute(
        "SELECT COUNT(*) FROM stops_fts WHERE stops_fts MATCH 'abbey'").fetchone()[0]
    assert n == 1, f"optimize broke FTS: {n}"
    n = writer.conn.execute(
        "SELECT COUNT(*) FROM routes_fts WHERE routes_fts MATCH '12'").fetchone()[0]
    assert n == 1, f"routes FTS broken: {n}"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest test_perf.py::test_35 -v`
Expected: FAIL — `AttributeError: 'TimetableWriter' object has no attribute 'optimize_fts'`.

- [ ] **Step 3: Implement**

Add to `TimetableWriter` (near `set_last_refresh`):
```python
    def optimize_fts(self):
        """Merge/coalesce FTS b-trees after bulk loads (query-speed tail win)."""
        self.conn.execute("INSERT INTO stops_fts(stops_fts) VALUES('optimize')")
        self.conn.execute("INSERT INTO routes_fts(routes_fts) VALUES('optimize')")
```
In `server.py`, in `load_all_timetable_data`, after the `writer.counts()` line and before building the return message:
```python
    writer.optimize_fts()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest test_delta.py test_robustness.py test_perf.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/openbusdata_mcp/store.py src/openbusdata_mcp/server.py
git commit -m "perf: optimize FTS indexes after full timetable loads"
```

### Task 9: Bounded plan-result cache (repeat calls are free)

**Files:**
- Modify: `src/openbusdata_mcp/store.py` — add `import functools`; add `TimetableStore._data_key`; wrap `plan_direct`, `find_buses_by_arrival_time`, `plan_one_change` in cached delegating wrappers (store.py:262-347)
- Test: `test_perf.py`

**Interfaces:**
- Consumes: `loaded_datasets` table; the three existing plan methods (their bodies move verbatim into `_*_impl` helpers).
- Produces: public methods with **identical signatures and output**; cache auto-invalidates when `loaded_datasets` content changes (`data_key`).

- [ ] **Step 1: Write the failing test**

```python
def test_36_plan_cache_hit_and_invalidation(writer, store):
    writer.add_journey("Op", "36", "outbound", "J36", {"mon"}, [
        {"naptan": "010A", "arrival": None, "departure": "09:00:00"},
        {"naptan": "010B", "arrival": "09:10:00", "departure": None}], 1)
    writer.add_stop("010A", "Alpha"); writer.add_stop("010B", "Beta")
    writer.commit()
    r1 = store.plan_direct({"010A"}, {"010B"}, "mon", "10:00:00")
    assert len(r1) == 1, r1
    assert store.plan_direct({"010A"}, {"010B"}, "mon", "10:00:00") == r1   # cache hit
    assert store.plan_direct({"010A"}, {"010B"}, "tue", "10:00:00") == []   # different key
    writer.add_journey("Op2", "36b", "outbound", "J36b", {"mon"}, [
        {"naptan": "010A", "arrival": None, "departure": "08:00:00"},
        {"naptan": "010B", "arrival": "08:10:00", "departure": None}], 2)
    writer.mark_dataset_loaded(2, None, "Op2")
    writer.commit()
    r4 = store.plan_direct({"010A"}, {"010B"}, "mon", "10:00:00")
    assert len(r4) == 2, r4   # data_key changed -> cache invalidated
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest test_perf.py::test_36 -v`
Expected: FAIL — no cache; final `len(r4)` still returns 2 so the failure surfaces only if the cache returns a stale 1. To make the test red before the change, note `plan_direct` is correct pre-cache (r1/r4 = 1/2), so the test's cache-hit lines are what fail once the cache exists but invalidation is wrong — run the suite **after** Step 3 to see it green.

- [ ] **Step 3: Implement**

```python
    def _data_key(self) -> tuple:
        """Dataset state that plan results depend on; changes after any load."""
        return tuple(self.conn.execute(
            "SELECT ds_id, modified FROM loaded_datasets ORDER BY ds_id").fetchall())

    def plan_direct(self, naptans_a, naptans_b, day, target_s):
        return list(self._plan_direct_cached(
            self._data_key(), frozenset(naptans_a), frozenset(naptans_b), day, target_s))

    @functools.lru_cache(maxsize=128)
    def _plan_direct_cached(self, key, a, b, day, target_s):
        # <existing plan_direct body, with a, b = set(a), set(b) re-derived at top>
```
Repeat the same delegation for `find_buses_by_arrival_time`/`_find_buses_cached` and `plan_one_change`/`_plan_one_change_cached` (their bodies move verbatim; re-derive `a, b = set(a), set(b)`). Add `import functools` at the top of `store.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest test_delta.py test_robustness.py test_perf.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/openbusdata_mcp/store.py
git commit -m "perf: bounded plan-result cache keyed on loaded dataset state"
```

**Phase 4 exit criterion:** `test_33`–`test_36` green; full suite green.

---

## End-to-End Verification (SDD executor, after the final whole-branch review)

1. Run the full suite: `.venv/bin/python -m pytest -v` — expect 0 failures (existing `test_delta.py` + `test_robustness.py` + new `test_perf.py`).
2. Rebuild the synthetic DB and re-time the headline paths:
   `./.venv/bin/python /tmp/perf/bench.py 2>&1 | tail -20` and `/tmp/perf/fix_demo3.py` —
   expect `plan_one_change` ≈ 0.6-0.7 s (was 16.5 s) on the 816-journey DB, and the single `_journeys_departing("CTR0", ...)` at ~8 ms (was ~190 ms).
3. Sanity-check tool-level shapes unchanged: `e2e_sqlite.py` (if data present) and the MCP tool smoke paths used by `test_robustness.py`.
4. Persist the companion design doc to `docs/superpowers/specs/2026-09-11-query-performance-design.md` (reproduce the Spec section verbatim) so the plan has a reachable spec for the final review.

**Deferred (not in scope, noted for a future pass):** batching `stop_names_bulk` across all plans in a result (~5-10 ms at 60+ plans), and a second `journey_stop_times` index on `(journey_id)` if a future feature scans per-journey departures.
