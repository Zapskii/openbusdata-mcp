# BUG REPORT: `estimate_live_eta` can never succeed when operator NOC ≠ operator name

**Date:** 2026-09-13 (~13:10–13:20 BST)
**Component:** `estimate_live_eta` MCP tool
**Fork:** openbusdata-fork, HEAD post PR #15 (merged 2026-09-13 09:50)
**Severity:** High — the tool's only documented input (`operator_ref: Operator NOC code`) is guaranteed to fail for every operator whose TXC operator name differs from its NOC. Affects all Arriva companies, and any other operator where name ≠ NOC.
**Verified live against:** Arriva Herts (NOC `ARHE`), routes SB4/SB5, Stevenage.

---

## Summary

`estimate_live_eta` performs its timetable-index lookup and its BODS live-feed
request with the **same** `operator_ref` string, but the two backends key
operators differently:

- The local timetable index stores routes keyed `"{operator_name}|{route_num}"`,
  where the name is `operatorName` from the BODS dataset metadata
  (e.g. `"Arriva UK Bus"`).
- The BODS SIRI-VM datafeed API requires `operatorRef` to be a **NOC code**
  (e.g. `ARHE`); it rejects operator names with HTTP 400.

So the two acceptable inputs are mutually exclusive, and no value of
`operator_ref` can succeed for such operators.

## Failure modes (both verified today)

| Input | Fails at | Error |
|---|---|---|
| `operator_ref="ARHE"` (NOC) | index lookup | `Route ARHE\|SB5 is not in the timetable index. Try get_route_stops().` |
| `operator_ref="Arriva UK Bus"` (name) | BODS API call | `Error fetching live data: HTTPStatusError: Client error '400 Bad Request' for url '...operatorRef=Arriva%20UK%20Bus&lineRef=SB4...'` (key redacted) |

## Repro

```text
estimate_live_eta(stop="210021203360", operator_ref="ARHE", line_ref="SB4", day="sun")
  -> "Route ARHE|SB4 is not in the timetable index. Try get_route_stops()."

estimate_live_eta(stop="210021203360", operator_ref="Arriva UK Bus", line_ref="SB4", day="sun")
  -> Error fetching live data: HTTPStatusError: 400 Bad Request (operatorRef=Arriva UK Bus)

# Contrast — the same NOC works fine when no index join is needed:
get_live_buses_on_route(operator_ref="ARHE", line_ref="SB4")
  -> 200, returns vehicle 3794 (MX12 KWR) with live lat/lon

# And the index does contain the route — keyed by the NAME:
get_route_stops(operator="Arriva", route="SB5")
  -> returns routes keyed "Arriva UK Bus|SB5" (and SB50), i.e. index key uses the name
```

## Root cause (file/line refs, HEAD post PR #15)

1. `server.py:1293` — `coords = store.route_stop_coords(operator_ref, line_ref)`
   passes the user's `operator_ref` straight into the index lookup.
2. `store.py:496-502` — `route_stop_coords()` looks up
   `routes WHERE key = f"{operator}|{route}"`.
3. `server.py:552` — at load time, `operator = meta.get("operatorName", "Unknown")`
   comes from the BODS **dataset metadata** (the human-readable name).
4. `server.py:594-602` + `parse_transxchange()` (`server.py:375`, Route
   construction at `server.py:456-461`) — that name is what `upsert_route()`
   stores as the `routes.op` / key (`store.py:772-783`,
   `key = f"{op}|{num}"`).
5. `server.py:1301` — after the index lookup succeeds, the **same string** is
   reused as `filters = {"operatorRef": operator_ref, "lineRef": line_ref}`
   and sent to BODS by `_fetch_siri_vm()` → 400 for anything but a NOC.

There is no name↔NOC mapping anywhere in the codebase (grep for `noc` in
`src/openbusdata_mcp/` only finds docstrings). The TXC operator code present
in every dataset XML is parsed and discarded — `parse_transxchange()` takes
only `operator_name` and the `Route`/`Journey` dataclasses carry no operator
code field.

Note `get_live_buses_on_route` is unaffected because it never touches the
index: `operator_ref` goes straight to BODS (which wants the NOC). The bug is
specifically the tool that needs **both** backends to agree on one identifier.

## Suggested fix

Minimal, robust (two halves; both needed):

1. **Store the NOC at load time.** In `parse_transxchange()` extract the
   operator code from the dataset XML (TXC `Operators/Operator/OperatorCode`
   / `NationalOperatorCode`, and/or per-VJ `OperatorRef` joined via the
   Agents section — verify against a real Arriva TXC file) and persist it
   alongside the name — either as `op_ref` columns on `routes`/`journeys`
   or as a small `operators(name, noc)` map table. This is index-data, so
   existing indexes only gain it after a `force_refresh` / delta of the
   affected datasets.
2. **Accept either identifier at query time.** In `estimate_live_eta` (and
   anywhere else that needs both sides):
   - Resolve the index key by trying the input as name, and as NOC via the
     map, in both directions.
   - Always send the **NOC** to BODS in `operatorRef`.
   - If no mapping exists for the operator, say so explicitly rather than
     feeding the name to BODS (that's the silent-400 today).

Optional fallback while indexes rebuild: a DfT-published National Operator
Codes (NOC) lookup table for name→NOC normalisation.

## Regression test sketch

```python
# NOC input (documented contract) must reach the BODS fetch:
out = await estimate_live_eta(stop="210021203360", operator_ref="ARHE",
                              line_ref="SB4", day="sun")
assert "not in the timetable index" not in out
assert "400 Bad Request" not in out

# Name input must still work (normalised to NOC for BODS):
out = await estimate_live_eta(stop="210021203360",
                              operator_ref="Arriva UK Bus",
                              line_ref="SB4", day="sun")
assert "400 Bad Request" not in out
```

## Related (previously documented, not new)

- `get_departures_board` Sunday pattern at these stops includes journeys the
  current published Sunday schedule doesn't run (stale index /
  superseded datasets). Known issue — requires a full `force_refresh` post
  PR #15 for correct `days_mask`s; the ETA tool's schedule side
  (`journeys_on_route`, `server.py:1311`) inherits this until rebuilt.

---

# RETEST after fix build `openbusdata-mcp:merged-pr16` (2026-09-13 14:17 BST)

## ✅ FIXED: name↔NOC deadlock

Both inputs now work — no "not in the timetable index", no BODS 400, and
error messages now report the resolved index key ("Arriva UK Bus|SB4"):

```text
estimate_live_eta(stop="210021203360", operator_ref="ARHE",     line_ref="SB4", day="sun")  -> OK path
estimate_live_eta(stop="210021203360", operator_ref="Arriva UK Bus", line_ref="SB4", day="sun")  -> OK path
```

Both now return: `No live buses matched to route Arriva UK Bus|SB4 near the
target stop (1 vehicles unmatched to the route).` — which is CORRECT
control flow for this case, but only because of BUG 2 below.

## ❌ BUG 2 (NEW, blocking): stops table has NO coordinates — vehicle→stop matching impossible

Every `estimate_live_eta` call still fails to match vehicles, even when the
live vehicle is 0.34 km from a stop on the route. Root cause:

- ALL 253,581 stops in `index.db` have empty `lat`/`lon` (verified:
  `SELECT COUNT(*) FROM stops WHERE lat='' OR lat IS NULL` → 253581 / 253581).
- The TXC parser never extracts coordinates:
  `server.py:390-397` (`AnnotatedStopPointRef` loop) reads only
  `StopPointRef` + `CommonName` and constructs `Stop(naptan, name)` — it
  ignores the `Location/Latitude`/`Longitude` children that BODS files
  carry (verified present: dataset 15766, `ARHE_4A_ARHEPF111644221010088SB40...`
  — 26/26 AnnotatedStopPointRef elements contain Location/Latitude+Longitude).
- `route_stop_coords` (`store.py:496-515`) joins routes.stops → stops, so
  every stop arrives at the matcher with `lat=None, lon=None`.
- `_haversine_km` (`server.py:177-185`) returns None when either side has
  no coords → `nearest` never set → `skipped += 1` for every vehicle
  (`server.py:1320-1327`) → the "unmatched" dead end.

Effect: vehicle→schedule matching is structurally impossible for the whole
index, so `estimate_live_eta` can never produce an ETA for any route or
operator, and `get_live_buses_on_route`'s positions can never be joined to
schedule. Fix the parser (extract Location coords into `Stop`), then one
`force_refresh` repopulates coordinates index-wide.

Regression sketch:

```python
out = await estimate_live_eta(stop="210021200950", operator_ref="ARHE",
                              line_ref="SB4", day="sun")
assert "unmatched to the route" not in out or "vehicles\"" in out  # ETA present
```

# RETEST round 3 — build `openbusdata-mcp:coords-687385c` (2026-09-13 17:23 BST)

## ✅ BUG 2 FIXED: stop coordinates parsed + repopulated

- Image `openbusdata-mcp:coords-687385c` running; delta load completed
  2026-09-13T15:06:23Z.
- Stops with coords: 125,963 / 253,635 (was 0 / 253,581). All tested
  Stevenage SB stops now have exact lat/lon (e.g. Oaks Cross S-bound
  51.880128, -0.170511 — matches bustimes.org).
- `estimate_live_eta(stop=210021203360, operator_ref=ARHE, line_ref=SB4,
  day=sun)` now matches vehicle 1027 at 0.03 km. The returned
  `eta: null, note: "past the target stop or no onward scheduled time"`
  is CORRECT: the vehicle was sitting at the loop terminus (Stevenage
  Bus Station), already past Oaks Cross. Correct control flow, not a bug.
- Journeys count grew 1,076,103 → 1,103,820 (delta re-parse with new
  parser confirmed).

## ✅ (round 2 fix, still holding) name↔NOC resolution

`operator_ref` accepts NOC (`ARHE`) and operator name; error messages
report the resolved index key. No index misses, no BODS 400s.

## ❌ REMAINING 1: get_departures_board emits every journey TWICE

Same journey_code appears 2× consecutively (observed across three builds
and multiple stops). limit=N counts duplicates, so you get N/2 unique
rows. Dedupe client-side for now.

## ❌ REMAINING 2: Sunday SB4 board times still contradict the source data

Board from MCP (16:10/16:20/16:40/16:50/16:58/17:20/17:50/17:57/18:00/18:20…)
vs the CURRENT published BODS Sunday SB4 file (dataset 15766, file
…409347.xml, parsed directly: 08:56, 09:58, 10:58, 11:58, 12:58, 13:58,
14:58, 15:58, 16:58, 17:57, 18:57, 19:57, 20:57, 21:56) vs bustimes.org
(:57/:58 hourly). The extra :10/:20/:40/:50 journeys either come from a
superseded dataset (index dedup gap) or a day-mask error (days_mask=127
rows not yet re-parsed for this dataset — delta doesn't re-parse
unchanged datasets). Likely resolved by a FULL force_refresh; if not,
it's an index dedup/mask bug worth its own ticket.

## Note for operators consuming the board

The MCP container appears to run UTC: unqualified board queries page from
UTC-now (an hour behind BST). Pass explicit `from_time`/`day` in
Europe/London terms or shift +1h when BST is in force.

---

# FIX applied for BUG 2 (2026-09-13, commit 687385c, image `openbusdata-mcp:coords-687385c`)

Two edits, because item 1 alone was a silent no-op:

1. `parse_transxchange` (server.py) now extracts `<Location><Latitude>` /
   `<Longitude>` from every `AnnotatedStopPointRef` into `Stop.lat/lon`
   (dataclass + schema already had the columns). Absent/half/malformed
   pairs leave coords unset. Verified against live dataset 15766: 62/62
   stops parse with coords, 84 journeys intact.
2. `add_stop` (store.py) conflict update now `COALESCE`s lat/lon (fill
   blanks, never blank an existing pair). **Necessary because
   `force_refresh` cannot purge the `stops` table** — `discard_dataset`
   purges journeys/routes/loaded_datasets only (stops are shared across
   datasets) — so the coordinate backfill rides the per-dataset upsert.
   The old ON CONFLICT updated `name` only and would have left all
   253,581 existing rows at NULL despite the re-download.

Verification: 152/152 tests (8 new: tests 37–40 parser, 41–44 upsert +
`route_stop_coords` roundtrip). Pushed origin/main `af968fb..687385c`.
Gateway restarted onto the new image; `force_refresh` re-downloading all
943 datasets (baseline re-probe: coords 0/253,581 = 0.00%). Refresh fixes
both this and the Sunday-staleness item in one pass.

## RETEST 3 after force_refresh completed (2026-09-13, commit 6a75fd9)

Refresh: 941 datasets reloaded, 0 errors. Index 253,635 stops / 7,493
routes / 1,103,820 journeys.

- ✅ Coordinates: 0 → **125,963 stops with lat/lon (49.66%)**. ASDA
  (210021200950) = 51.89917, -0.20077. Route SB4: **21/21 stops**.
  Cross-operator spot check: 100% on sampled Go-Ahead / TfGM / Arriva
  routes; partial or 0% on some Stagecoach / First routes — those TXC
  files genuinely omit `Location`, so coverage is bounded by source data,
  not the parser (absent Location → NULL by design, no crash).
- ✅ `estimate_live_eta` matches vehicles again: both live SB4 vehicles
  resolve with real distances (0.03 / 0.05–0.06 km), NOC and name inputs
  alike. Positive-ETA path stays covered by mocked tests (15 min
  scheduled / 10 min schedule-offset bases). Live vehicles currently sit
  at route termini, so the honest "past the target stop" note is correct
  right now; matching itself is proven live.
- ✅ Sunday board staleness (BUG_REPORT "Related"): `get_departures_board`
  at 210021203360 now returns the Sunday shape including **15:58** (the
  hourly :58 pattern verified by BigDDs against bustimes.org), alongside
  the weekday-pattern entries from other loaded datasets.
---

# RETEST round 4 — Service-level days + validity period (2026-09-13, ~18:00 BST)

## Root cause found: REMAINING 1 and REMAINING 2 are ONE parser defect

Evidence (all from the live index copy + dataset 15766's raw zip, post
force_refresh 6a75fd9):

- All 154 SB4 journeys in the index come from dataset **15766 alone** —
  nothing superseded from older datasets. The "stale index" theory is dead.
- The dataset ships **8 SB4/SB4-related files**: 3 for the expired
  registration 2026-06-28→2026-08-29 and 3 for the current 2026-08-30→open
  (plus 2 route-4A files, same twin structure). Per period: one Sunday-only
  file, one Mon-Fri file, one Saturday file. Current vs expired copies are
  **byte-identical in journey content**.
- The TXC declares days of week and validity on the **<Service>** element;
  0 of 77 SB4 VehicleJourneys carry their own <OperatingProfile>.
- `_parse_days` fell back to all-7-days for every such VJ →
  **337,809 / 1,103,820 index journeys (30.6%) had days_mask=127**. The
  weekday :15/:35 pattern has been polluting the Sunday board all along.
- The expired-period twins loaded alongside the current period →
  **77,513 exact-duplicate journeys index-wide** → every board row twice.

This is the same family as the archived 2026-09-12 report's "days_mask=127
for every journey": PR15 fixed the presence-only `<Monday/>` reading for
per-VJ profiles; the Service-level declaration (and the registration
period) remained unread.

## Fix (this round)

`parse_transxchange` (server.py):

1. Days resolve per-VJ OperatingProfile → **Service <DaysOfWeek>** →
   legacy all-days default (undated/legacy files unchanged).
2. The Service's **<OperatingPeriod> gates the whole file**: an expired or
   not-yet-started registration contributes zero journeys even while BODS
   keeps shipping it inside the operator pack.
3. Exact-duplicate journey profiles (same code + days + full stop times)
   dedupe to one — BODS legitimately packs several registration variants
   of the same file into one dataset zip.

Verified against the real 15766 zip: expired files → 0 journeys; Sunday
file → 14 journeys, all days={sun}, Oaks Cross deps 08:56→21:56 hourly
(:56/:58, **15:58 present**) — exact match to the bustimes.org ground
truth. Dataset-wide 304 files → 7,258 journeys kept.

Tests: 157/157 (5 new: tests 41-45 — Service days, per-VJ precedence,
period gating, dedupe, legacy default; 1 updated: `_parse_days(None)` is
now `{}` at VJ level, Service level decides).

**Note:** existing indexes keep the wrong masks until a FULL
force_refresh — a delta cannot see an unchanged dataset, and 30.6% of rows
carry mask=127. Rebuild is required; boards will self-heal afterwards.

## Round 4 follow-up (same day, post-refresh): dedupe must be DATASET-scope

Full force_refresh with 90b48d8 landed (113 min, 940/941 datasets, 1
error: 19731 Stowmarket Minibus, 4 journeys intact via transaction
rollback — retry queued). Results: 1,103,820 → 778,304 journeys
(expired twins gone), SB4 board now exactly bustimes.org (15:58 present,
no :10/:20/:40/:50 phantoms), mask=127 rows 337,809 → 11,318.

Residual: 17,319 exact-twin rows remained, all WITHIN single datasets —
Go-Ahead packs ship 4 byte-identical copies per journey across 4 files
of one zip. The round-4 dedupe ran per-file, so cross-file twins inside
one dataset survived. Fix: loader accumulates all files of a dataset,
then applies _dedupe_journeys at dataset scope (parse_transxchange no
longer dedupes). Tests 157/157 (test_44 recut to the new contract).

---

# RETEST round 5 — build `openbusdata-mcp:latest` = orphans-clean 4def6eb (2026-09-13, ~21:30 BST)

Scope: full functionality + live-tracking pass on the three-commit build
(svc-days 90b48d8, dsdedupe 7443e95, orphans-clean 4def6eb) after the
19:14:20Z delta. Index state: 758,490 journeys / 253,635 stops (125,963
with coords) / 7,493 route rows / 0 orphan (ds_id=0) journeys /
journey_stops 30,371,948 rows.

## ✅ All previously-open items verified closed

1. **Board double-emission (round-3 REMAINING 1)** — gone. A 50-departure
   pull at Stevenage Bus Station Stop L contains zero duplicate
   journey_codes; every journey appears exactly once.
2. **Sunday SB4 times contradict the source (round-3 REMAINING 2)** —
   resolved and now verified against the raw file, not just bustimes.org:
   the current 2026-08-30→open Sunday file (…2409347.xml) yields Stop-L
   times 09:09 / 10:11 / 11:11 / 12:11 / 13:11 / 14:11 / 15:11 / 16:11 /
   17:11 / 18:10 / 19:10 / 20:10 / 21:10 / 22:09 (14 Sunday journeys,
   08:45→21:45 hourly departures from the loop's first Bus Station stop),
   and the board returns exactly those times. Day-split files are clean
   in the index too: `route='SB4'` rows carry `[sun]`, `[sat]`,
   `[mon..fri]` as three separate day sets (77 journeys), not mask=127.
3. **Orphan journeys** — 0 rows with ds_id=0.
4. **Dataset-scope dedupe (round-4 follow-up)** — journeys 1,076,103 →
   758,490 after the delta re-parse; the residual cross-file twins are
   gone. Boards/planner output is duplicate-free as a consequence.
5. **One-change planner** — instant on empty results (PR #15's hang stays
   fixed) and correct on the positive path: Bus Station→Poplars
   (210021200005→210021201520, sun, by 13:00) returns 15 one-change plans
   with correct transfer pairing (leg-2 always departs at/after leg-1's
   mid arrival; SB4/SB5 → SB1 via ASDA/Rockingham Way).
   Two of its "No journey found" answers were verified data-true against
   the DB (mids_a ∩ mids_b empty at SQL level): 210021200011 is a Sunday
   TERMINUS in this dataset (all Sunday journeys touching it end there),
   so Sunday one-change plans via Stop L are structurally impossible —
   not a planner defect.
6. **Live tracking** — `get_live_buses_on_route(ARHE, SB4)` returns
   multiple vehicles with lat/lon/bearing; `estimate_live_eta` matches
   all of them (distance_km 0.0–0.1) with schedule-offset ETAs carrying
   journey refs. Raw passthrough `SIRI_VM_Data_feed_api_v1_datafeed`
   still hits BODS's 403 bot wall (the wrapped tools are the interface).

## ❌ NEW BUG (HIGH): cross-region route-row collision — `routes` keyed `op|num` globally while `operatorName` is not region-unique

`upsert_route` (store.py:838) keys route rows `key = f"{op}|{num}"` with
`op` = the BODS dataset `operatorName`, GLOBALLY unique across datasets.
But operatorName is not a region-unique identifier: every Arriva region
publishes as `"Arriva UK Bus"` — and BODS's `noc` metadata is
operator-level too, so ds 15766 (Herts/Shires) and ds 22533 (Telford/
Shropshire) carry BYTE-IDENTICAL 40+-NOC lists. The merge rule "union
directions, keep the longest stop sequence" (store.py:850-856) then lets
one region's route row evict another's, and `ds_id` records the winner
only.

Live evidence (index post 19:14Z delta):

```sql
SELECT key, ds_id FROM routes WHERE key IN (...)
  Arriva UK Bus|101 -> ds_id=22533   -- Telford pack (stops 3590E*),
                                     -- 12,220 unique journey stops
  Arriva UK Bus|301 -> ds_id=15766   -- correct (its row is longest today)
  Arriva UK Bus|SB4 -> ds_id=15766
-- stop_to_routes composition for 'Arriva UK Bus|101':
--   101 rows, ALL '3590%' (Telford), ZERO '2100%' (Shires)
-- yet ds 15766 still has 72 journeys for route 101, all serving
--   Stevenage stops (210021203880 AND 210021200011 on every one)
-- benign contrast: 'Arriva UK Bus|55' -> ds 15954, whose 55 journeys
--   use 2100* stops too — a same-family registration merge, working
--   as designed.
```

So ds 15766's Shires 101 `routes` + `stop_to_routes` rows are displaced
index-wide by the Telford pack, while its 72 journeys (serving Stop L 72
times) remain intact — the two layers disagree.

**User-facing impact (all verified):**

- `get_route_stops("Arriva UK Bus", "101")` returns the TELFORD stop
  list (Princess Royal Hospital → Madeley Centre) — wrong answer with no
  error; the Shires 101 is unqueryable.
- `find_routes_between_stops` loses every pair that only the Shires 101
  serves (returns "No single route serves both") even when
  `plan_journey` finds direct 101 journeys between the same stops —
  contradictory answers between tools on the same input.
- `estimate_live_eta` resolves the route via `routes` → wrong/missing
  region stop list for `route_stop_coords` → wrong nearest-stop matching
  or a false "not in the timetable index".
- **NOT affected** (journey-level, keyed by ds_id): `get_departures_board`,
  `plan_journey` (direct + one-change), `find_buses_by_arrival_time`.

**Same family, minor:** `stop_to_routes(naptan, key)` has no unique
constraint/PK, so `INSERT OR IGNORE` is a no-op guard and duplicate rows
accumulate (e.g. 16 rows for Arriva SB4×stop 210021200005). Cosmetic but
inflates scans and dedupe work.

## Root cause (file/line refs, HEAD 4def6eb)

1. `server.py:700-701` — `writer.upsert_route(route.operator, …, ds_id)`;
   `route.operator` is the dataset's `operatorName` (parse_transxchange
   passes it through; the TXC operator *code* is present in the files but
   unused for route identity).
2. `store.py:841` — `key = f"{op}|{num}"`, and `routes.key` is the table's
   PRIMARY KEY → one row per name+number worldwide.
3. `store.py:850-856` — cross-dataset merge keeps the longest stop list,
   silently overwriting `directions`/`stops` (and leaving `ds_id` on the
   longest-writer) instead of treating different regions as different
   routes.

## Suggested fix

1. **Make route identity per-dataset(-group).** Key `(op, num, ds_id)` —
   journeys already carry ds_id, so the planner/boards need no change.
   NOTE: keying by NOC is NOT viable — 15766 and 22533 share the
   identical 40+-NOC `loaded_datasets.noc` list, so NOC cannot separate
   the regions (BODS publishes the NOC list at operator level, not
   dataset level).
2. **Merge only within a same-region family** (same op + overlapping
   stop corpus, or same ds lineage across refreshes): union directions,
   keep longest sequence — as today. Across regions, keep separate rows.
   A cheap discriminator: only merge when the incoming stop list shares
   a meaningful fraction of stops with the stored one (e.g. ≥50%
   Jaccard), else insert a sibling row.
3. **Query side:** `get_route_stops`, `find_routes_between`,
   `resolve_operator`/`route_stop_coords` should treat (op, num) as
   possibly multi-region: return all matches grouped by region/ds, or
   prefer the region whose dataset actually contains the queried stops,
   with an explicit multi-region note rather than a silent pick.
4. **Add `UNIQUE`/PK on `stop_to_routes(naptan, key)`** and dedupe
   existing rows in the migration.
5. **Migration:** existing indexes need re-upserting (delta of affected
   datasets, or force_refresh) once the key changes; journey data is
   already correct so boards/planner need nothing.

Regression test sketch:

```python
# Shires 101 must survive a Telford 101 pack regardless of load order
out = await get_route_stops(operator="Arriva UK Bus", route="101")
assert any(s["naptan"].startswith("2100212")
           for m in out for s in m["stops"]), "Shires 101 displaced"

out = await find_routes_between_stops("210021203880", "210021200011")
assert any(r["route"] == "101" for r in out), "stop_to_routes lost 101"

# estimate_live_eta must resolve the region that contains the stop
out = await estimate_live_eta(stop="210021200011",
                              operator_ref="ARHE", line_ref="101", day="sun")
assert "not in the timetable index" not in out
```

## Ops caveat observed during testing (unproven mechanism, low priority)

While the 19:14Z delta was settling (604 MB WAL, last write 20:15), the
same board query returned different results minutes apart; the current
gateway's answers are stable and match the raw data. A long-lived MCP
process pinning a WAL read snapshot can answer from pre-delta state while
a delta is mid-flight — worth either restarting containers after load
jobs or documenting that boards can straddle a concurrent delta. (Not
investigated further per request; flagged for awareness only.)

## Test-log integrity notes (methods used this round)

- Ground truth computed by parsing dataset 15766's TXC directly. Two
  pitfalls worth recording for future rounds: (a) SB4's day-variant files
  REUSE `JourneyPatternSection` ids (js_1…) with different runtimes —
  parse one file at a time or times corrupt; (b) Stop 210021200780
  (Railway Stop M) appears in the current files ONLY as an
  `AnnotatedStopPointRef` declaration, never inside timing links → zero
  board rows at M is CORRECT; any M departures observed earlier were
  served from pre-delta state (see ops caveat above).

---

# FIX round 6 — cross-region route-row collision (2026-09-14)

`upsert_route` keys route rows per dataset now: `key = op|num|ds_id`
(store.py). One (operator, route) can carry sibling rows — every Arriva
region publishes as "Arriva UK Bus", and NOC lists are operator-level
(byte-identical for ds 15766 vs 22533), so they cannot discriminate the
regions. Merge semantics (union directions, longest sequence) apply only
WITHIN one dataset's pack (registration variants across its files).

- `get_route_stops` reports every region sibling (each with `ds_id`).
- `route_stop_coords(prefer_stops=...)` picks the sibling containing the
  queried stop (`estimate_live_eta` passes the resolved stop).
- `resolve_operator` matches on (op, num) columns — identical (name, NOC)
  across siblings since both are dataset-level metadata.
- `discard_dataset` purges routes exactly (`routes WHERE ds_id=?` + their
  s2r refs); the merged-state self-heal workaround and
  `discard_untagged_for` are gone (untagged journeys purge via
  `discard_dataset(0)`, as the force_refresh reconcile already did).
- `stop_to_routes(naptan, key)` gained its PK; ensure_schema runs a
  one-time, column-gated migration: rekey legacy `op|num` rows via a temp
  mapping table, drop ds_id=0 rows, rekey s2r, collapse duplicates, sweep
  orphans, rebuild routes_fts.
- Migration is intentionally NOT lossless across multi-ds families: only
  the longest-writer's stop list survived the old merge, so each affected
  dataset is re-upserted (surgical reload) to restore its own region row.

Live-index blast radius (read-only probe, post 19:14Z delta): 1,164
(op,route) families shared one row across 328 ds_ids (worst: Stagecoach|1
merged 12 datasets, First Bus|1 merged 10); stop_to_routes carried 695,022
rows vs 265,035 distinct pairs (429,987 surplus); Arriva|101 s2r refs were
2490*/2400* (Shires-area codes) while routes.stops JSON held Telford
3590* stops — the two layers disagreed ACROSS regions.

Tests: test_region_collision.py (8 regressions: sibling rows either load
order, s2r discoverability both regions, multi-region get_route_stops,
prefer_stops, resolve_operator across siblings, exact discard, legacy-DB
migration + idempotence). 165 passed, secrets-stripped.

## Round 6 E2E (live index post surgical reload, MCP stdio probe)

- get_route_stops("Arriva UK Bus", "101"): 5 sibling rows returned
  (ds 15766 Shires 38 stops incl. Stop L; ds 22528 the 3590* Telford
  pack; ds 22533 49 stops; ds 22537 101A + 101) — every region
  queryable, zero duplicate (op,route,ds) rows.
- CORRECTION to this report's evidence: the 3590* Telford pack is ds
  22528, not 22533 (22533's 101 is Stevenage-area 2490*/2400* — same
  correction as the s2r prefixes). BODS has FOUR sibling packs for
  101 (22528/22533/22537 + 15766).
- find_routes_between_stops(210021203880, 210021200011): returns 100 +
  SB9 (correct at pattern level). FURTHER CORRECTION to the report's
  expectation: route 101 NEVER belonged in that answer — its 72
  dual-stop journeys serve the pair at JOURNEY level, but neither stop
  sits in any 101 JourneyPattern (source semantics, pre-collision).
  The tool's answer is data-true against the healthy index.
- estimate_live_eta(210021200011, ARHE, 101): returns the data-true
  answer "not served by route Arriva UK Bus|101" (same reason: stop
  not on any 101 pattern). With a stop ON the pattern (210021109740,
  Stop L), the tool resolves the SHIRES region and returns real
  vehicles with schedule-offset ETAs — verified directly against the
  live index; the same call answers over MCP stdio when a prior live
  call primed the session, but the FIRST SIRI-VM fetch inside a
  one-shot stdio server session can hang (>300s). Not reproducible
  outside the stdio session; not a regression of this fix (fix touches
  no live-fetch path). OPEN: needs a repro against the persistent
  gateway before treating it as a real defect.

## Round 6 follow-up (5372d2e): s2r table rebuild

The live index's stop_to_routes table predated the PK — CREATE TABLE IF
NOT EXISTS cannot retrofit one, so after the first migration's dedupe the
surgical reload re-accumulated 117,812 duplicate groups (INSERT OR IGNORE
stayed a no-op guard). Caught by post-reload verification, fixed in
5372d2e: the migration detects the legacy shape via PRAGMA table_info pk
flags and rebuilds the table (rename -> create with PK -> DISTINCT copy ->
drop -> reindex). Live re-run: 695k+ rows -> 357,119 distinct, dup_groups=0,
orphans=0, PK verified in place. 166 tests pass. Cosmetic known issue: the
migration's log line says "rekeyed N route rows" where N is the total
routes count, not the rekeyed subset (rekey is a no-op when already
migrated; the gate is column-driven so nothing re-corrupts).
