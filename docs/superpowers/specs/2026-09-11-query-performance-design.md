# Query-Performance Design (Phase 8 of the code-health program)

Companion design doc for the Phase 8 implementation plan
(`docs/superpowers/plans/2026-09-11-query-performance-fixes.md`). This reproduces
the plan's **Spec (Evidence & Decisions)** section verbatim so the plan has a
reachable spec. Implemented end-to-end on branch `worktree-fix+code-health-phase-8`
by Tasks 1-9 (commits `5c50340`..`1733cb2`), reviewed task-by-task and by a final
whole-branch review.

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

---

## E2E verification (executed after the whole-branch review)

Headline paths measured on the finished branch with a writer-API-built equivalent of the plan's synthetic DB (816 journeys, 12 radial corridors; the phase-1 `bench.py`/`fix_demo3.py` bypass `add_journey` and hard-code a phase-1 worktree path, so the faithful rebuild uses the writer API and the current branch):

| Path | Baseline (pre-fix) | Phase 8 branch |
|---|---|---|
| `_journeys_departing("CTR0", "08:00:00")` | ~192 ms | **1.5 ms** |
| `plan_one_change(R3_hub → R7_03, mon 17:00)` | **16,492 ms** | **703 ms** (criterion: sub-1s) |
| `plan_one_change` repeat (cached) | — | **0.0 ms** (hits=1/misses=2) |
| `plan_direct(CTR0 → R7_23)` | ~198 ms candidate path | **2.3 ms** |
| `find_buses_by_arrival_time(CTR0 → R7_23)` | ~198 ms candidate path | **2.2 ms** |
| `get_route_stops("Radial","3")` | 0.8 ms @ 60 routes | **0.3 ms** |
| `search_stops("R1 1")` | FTS | **0.3 ms** |

Suite: **52 passed, 0 failures** (`test_delta.py` 24 + `test_robustness.py` 16 + `test_perf.py` 12). Full MCP-protocol `e2e_sqlite.py` smoke not runnable here (no real data in `~/.cache/openbusdata/index.db`; its RSS check reads `/proc/self/status`, which is Linux-only).
