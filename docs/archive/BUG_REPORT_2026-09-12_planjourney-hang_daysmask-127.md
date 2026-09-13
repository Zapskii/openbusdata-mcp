# Bug report: openbusdata MCP — one-change `plan_journey` hangs; day-of-week filtering dead

**Date:** 2026-09-12 · **Reporter:** BigDDs (via Hermes agent) · **Status:** root-caused, fixes drafted

---

## Summary

Two bugs, one blocking:

| # | Severity | Bug |
|---|----------|-----|
| 1 | **Blocker** | One-change `plan_journey` never returns (>9 min, 100% CPU) and blocks every other MCP tool queued behind it |
| 2 | **Major (correctness)** | Every journey is marked as running every day (`days_mask=127`) — day-of-week filtering silently does nothing |

Also noted: an 8.1 GB un-checkpointed WAL on the cache volume (operational issue, see §4).

---

## 1. `plan_journey` one-change hang (blocker)

### Symptom

- `plan_journey(stop_a, stop_b, arrive_by, max_changes=1)` pegs the container at ~100% CPU and does not return within 9+ minutes (MCP client timeout 420 s hit repeatedly).
- While it runs, **all other tools queue behind it and time out too** — even trivial ones (`get_route_stops` timed out at 420 s while a join was in flight).
- `max_changes=0` (direct-only) is instant and correct. Stop search, departures, live feeds: all fine.
- Affects both image builds (see §5), so not a regression from the latest rebuild.

### Repro

```
plan_journey stop_a=269057083 stop_b=269030076 arrive_by=14:00 max_changes=1   # Newcastle → Chelmsford (no plausible itinerary)
plan_journey stop_a=269057083 stop_b=269043035 arrive_by=14:00 max_changes=1   # same-city pair, direct route exists, 16 shared mid-stops
```

Both time out. A pair with a **result set of zero** is worst-case: the join grinds its entire cross-product before proving emptiness.

### Environment

- DB: `openbusdata-cache` volume, `index.db` 10.97 GB (checkpointed copy used for profiling), WAL originally 8.1 GB.
- `journeys` = 1,076,103 rows; `journey_stop_times` = 43,199,686 rows.
- Indexes present and used: `jst_n(naptan, dep)`, `jst_j(journey_id, seq)`. **Not a missing-index problem.**
- Images: `openbusdata-mcp:latest` (reports v1.30.0, built from fork @ `18b016c`) and `:delta` (v1.0.2) — both hang.

### Root cause

`TimetableStore._plan_one_change_cached` (store.py ~L330) is a single SQL self-join driven from the **A-side**. Query plan (measured on a healthy checkpointed copy):

```
SCAN aa                                        -- leg-1 journeys boarding at A
SEARCH sm USING INDEX jst_j (journey_id=? AND seq>?)   -- every downstream stop of every leg-1 journey
SEARCH sm2 USING INDEX jst_n (naptan=? AND dep>?)      -- all departures from that stop, unbounded
CORRELATED SCALAR SUBQUERY                     -- MIN(seq) per (j2, mid) pair
...
USE TEMP B-TREE FOR ORDER BY
```

For every leg-1 journey × every downstream stop, `sm2` fans out over all later departures at that stop (busy corridors: thousands of rows), each row additionally executing a correlated `MIN(seq)` subquery. Over 43.2M stop-times this is unbounded — LIMIT 2000 never gets reached to prune the search.

### Proof it's the join shape, not the data

Staged rewrite (drive from the tiny B-side candidate set, intersect mid-stops first, then bound each leg with index searches) against the same copy, same pair (`269057083 → 269043035`, sat, by 14:00):

| Step | Result | Time |
|------|--------|------|
| A∩B mid-stop candidates | 16 stops | ~6 s |
| Leg-1 journeys via mids (arrive by 14:00) | 2,000 rows (LIMIT) | 2.2 s |
| Leg-2 journeys boarding mids, alight B | 2,000 rows (LIMIT) | 0.6 s |

Original join on the same copy, same pair: killed at **540 s** (alarm). Hopeless pair (`269057083 → 269030076`): direct routes = 0, mid-stop intersection = **0** — verified genuinely un-plannable, yet the original join still runs >9 min to answer "none".

### Suggested fix (store.py)

Two-step planner instead of the single self-join:

```python
# 1) candidate mid-stops: served by an A-journey AND before B on some B-journey
mids = conn.execute("""
    SELECT DISTINCT m.naptan
    FROM journey_stop_times m
    WHERE m.journey_id IN (SELECT journey_id FROM journey_stop_times WHERE naptan IN (...a...))
      AND m.naptan IN (
          SELECT sm2.naptan FROM journey_stop_times sm2
          WHERE sm2.journey_id IN (SELECT journey_id FROM journey_stop_times WHERE naptan IN (...))
            AND sm2.seq < (SELECT MIN(seq) FROM journey_stop_times
                           WHERE journey_id = sm2.journey_id AND naptan = sm2.naptan))
""").fetchall()
# 2) per-mid leg queries using jst_n(naptan, dep) / jst_j(journey_id, seq),
#    both legs time-bounded (dep >= leg1 arrival, arr <= target), then pair+dedupe in Python.
```

Measured ~9 s worst-case vs >540 s. (First-cut single-SQL B-driven rewrite is tempting but I got the join order wrong once — the staged version is what was actually verified. Semantics to preserve: leg-1 boards at first A occurrence, leg-2 boards at first mid occurrence with `dep >= leg1 mid arrival`, `j2 != j1`, ORDER BY final arrival, LIMIT 2000, tool-level dedupe/top-15 unchanged.)

---

## 2. `days_mask` is 127 for every journey (major)

### Symptom

```sql
SELECT days_mask, COUNT(*) FROM journeys GROUP BY days_mask;
-- 127 | 1076103
```

`days_mask & DAY_BITS[day]` matches **every** row for any day, so day-filtered results include services that don't run that day (e.g. a Sunday-only service appears in "Saturday" plans). Direct, one-change, departures-board and route queries are all affected.

### Root cause

`_parse_days` (server.py:348) only adds a day when the element has non-empty text `"true"/"1"`:

```python
if tag in day_map and regular.text and regular.text.lower() in ("true", "1"):
    days.add(day_map[tag])
```

But TransXChange encodes regular operating days as **presence-only empty elements** (`<Monday/>`), so `regular.text` is `None` for virtually every feed; `days` stays empty and the fallback at line 363-364 (`if not days: return set(day_map.values())`) marks every journey as running daily.

### Suggested fix (server.py `_parse_days`)

```python
text = (regular.text or "").strip().lower()   # presence-only <Monday/> -> text is None/empty
if tag in day_map and (regular.text is None or text in ("true", "1")):
    days.add(day_map[tag])
```

Empty element = day applies. After patching, a delta/reload rebuilds `days_mask`; verify with the GROUP BY above (should no longer be a single 127 row).

---

## 3. Blocking interaction between tools (design note)

`plan_journey`'s heavy CPU-bound join runs synchronously in the single-container stdio MCP server, so one bad query head-of-line-blocks everything. Short term the §1 rewrite shrinks the worst case to seconds; longer term consider a per-request statement timeout (`sqlite3.interrupt` / progress handler) so a pathological query fails fast instead of stalling the server.

Related ops hazard: after a client timeout + reconnect, the abandoned container keeps spinning as a zombie; repeated reconnects stack multiple containers (observed 3 concurrent). Kill strays with `docker rm -f` — the manager respawns on demand.

---

## 4. Cache volume WAL bloat (operational)

- `index.db-wal` reached **8,148,696,712 bytes** (index.db 9.86 GB), last written 2026-09-12 ~10:11 UTC — consistent with an interrupted `load_timetable_delta` that never checkpointed.
- Did **not** cause the join hang (checkpointed copy still hung), but adds 8 GB of volume bloat and slow container starts.
- Cleanup (done on a copy; safe to repeat on the live volume while no container is running):
  ```
  docker run --rm -v openbusdata-cache:/d alpine sh -c \
    'apk add -q sqlite && sqlite3 /d/index.db "PRAGMA wal_checkpoint(TRUNCATE);"'
  ```

---

## Verification checklist (post-fix)

1. `plan_journey` 269057083 → 269043035, sat, by 14:00, `max_changes=1` — returns within seconds.
2. Same pair, `max_changes=0` — unchanged, instant.
3. Hopeless pair (269057083 → 269030076) one-change — returns "No journey found" fast (no 9-min grind).
4. `SELECT days_mask, COUNT(*) FROM journeys GROUP BY days_mask` — no single 127 row; per-day counts plausible.
5. While a one-change plan runs, `search_stops` still responds (no head-of-line blocking).
6. Zombie-container sweep after forced timeouts: `docker ps` shows ≤1 openbusdata-mcp container.