# BUG REPORT — round 7: build `33fb090f` (= tag `105ee1d`, deployed 2026-09-14)

**Tested by:** E2E MCP session against the live deployed container, 14:45–15:15 BST
**Build:** `openbusdata-mcp:latest` = `33fb090f9c8a`, identical to fork HEAD `105ee1d`
(md5 of `src/openbusdata_mcp/server.py` matches inside the container — line refs below are valid)
**Reader assumptions:** you do NOT have the live database. Every DB number in this
report was produced with the docker one-liner shown inline, and every MCP repro is a
plain tool call. You only need: this repo + docker + a BODS API key.

**Fixed & verified this build (no action needed):** round-5 route collision — routes now
keyed `(op, num, ds_id)`; `routes` has a `ds_id` column, `meta.routes_rekeyed=1`;
`get_route_stops(Arriva, 101)` returns all four packs (ds 15766/22528/22533/22537) at once;
`find_routes_between_stops` and `find_buses_by_arrival_time` work; raw SIRI-SX Disruptions
passthrough now returns the feed (2 MB) instead of the old 403.

---

## BUG A (HIGH) — 58.7% of journeys have flat times: every stop carries the same single time

**Scope (measured on the live index, 2026-09-14 ~14:50Z):**

```
449,538 / 766,135 journeys (58.7%) have ONE distinct time repeated across every
stop of the journey (arrival == departure == the journey's departure time).
```

Worst datasets: ds 20441 (44,615 flat journeys), 18510 (36,946), 18422 (28,806),
18450 (14,630), 24617 (14,597), 18509 (13,032), 18047 (11,876).

**User-visible symptoms (all reproduced via MCP against the live build):**

1. `find_buses_by_arrival_time(210021200945 → 210021109680, mon, arrive_by=16:00)`
   returns 10 phantom Stagecoach **9A** rows like `VJ321: depart 08:00:00, arrive
   08:00:00` (zero-duration ride, alighting "Hitchin, ASDA" — a stop route 9A never
   calls at per its own route row). The 9A journeys that really run Stevenage→Bedford
   (ds 18512, codes VJ226–VJ273) are all flat-time rows, and their stop list happens
   to include both naptans, so the planner accepts them as valid A→B legs.
2. `plan_journey` can emit zero-duration legs built from the same journeys.
3. Any board/planner row sourced from the flat datasets has meaningless times.

**Root cause (verified in the raw TXC, not inferred):**

The parser reads pattern timings ONLY from `JourneyPatternSection` blocks:

- `server.py:475-492` — builds `jps_links[jps_id] = [(from_ref, to_ref, runtime)]`
  from each `JourneyPatternSection`'s `JourneyPatternTimingLink/RunTime`.
- `server.py:552-598` — per-VehicleJourney accumulation loop (`server.py:580-591`):
  walks `jp_data["section_ids"]`, adding each link's `runtime` to `current_seconds`.

But a large family of BODS files (Stagecoach regional packs; ds 18512 verified,
file `603-None--SCCM-CAPE-...-PF0000459_165_20260830-...xml`) publishes:

- `JourneyPatternSection > JourneyPatternTimingLink > RunTime` = **`PT0M0S` for all
  720 links** (placeholders), and
- the real per-journey runtimes as **`VehicleJourneyTimingLink` overrides** —
  7,648 of them in that one file, each
  `<VehicleJourneyTimingLink id="VJTL…"><JourneyPatternTimingLinkRef>JPTL484</…>
  <RunTime>PT1M0S</RunTime>` (values PT1M0S…PT11M0S etc.).

The parser never looks at `VehicleJourneyTimingLink`, so every link contributes
0 seconds and the loop stamps `DepartureTime` (`server.py:561`, e.g. VJ223 = 06:16)
into every stop of the journey. `server.py:593` still ships the journey because it
has ≥2 stops. This is the same SCCM placeholder pattern already documented in the
`uk-bus-timetables` skill ("every row shows arrival == departure → parse the
`VehicleJourneyTimingLink` overrides instead").

**Repro without the DB:**

```bash
# 1. MCP repro (phantom rows): find_buses_by_arrival_time
#    stop_a=210021200945 stop_b=210021109680 day=mon arrive_by=16:00
#    -> Stagecoach 9A rows with depart == arrive at exact hours
# 2. Raw-file proof (needs BODS key, ~6.5 MB):
KEY=... # OPENBUS_API_KEY
curl -s "https://data.bus-data.dft.gov.uk/api/v1/dataset/18512/?api_key=$KEY" | jq -r .url
curl -s "<url>?api_key=$KEY" -o ds18512.zip && python3 -m zipfile -e ds18512.zip x/
grep -c 'PT0M0S' 603-*.xml            # 720 in JPS timing links
grep -c 'VehicleJourneyTimingLink' 603-*.xml   # 7648 overrides with real runtimes
```

```sql
-- index-side proof (any built index):
SELECT COUNT(*) FROM (SELECT journey_id FROM journey_stop_times
  GROUP BY journey_id HAVING COUNT(DISTINCT COALESCE(dep,arr))=1 AND COUNT(*)>5);
-- 449538
```

**Suggested fix (in `parse_transxchange`, server.py ~475-498):**

1. Collect `VehicleJourneyTimingLink` overrides per journey while iterating the VJ:
   `override[JPTL_ref] = runtime` (they carry `JourneyPatternTimingLinkRef`).
2. Build a global map `jptl_runtime[JPTL_id] = runtime` while walking
   `JourneyPatternSection > JourneyPatternTimingLink` (currently the id is discarded).
3. In the accumulation loop, for each `(from, to, runtime)` looked up from
   `jps_links`, use `override.get(jptl_id, runtime)`; if the whole pattern's
   pattern-level runtimes are all zero AND overrides exist for the VJ, the overrides
   take over entirely. Journey-level `Layover`/`Delay` handling unchanged.
4. Guard: if a journey ends with all-identical times despite the above, drop the
   journey (or tag it) rather than publishing a phantom — a flat journey cannot be
   boarded meaningfully and today produces false planner results.

**Regression test sketch:**

```python
# ds 18512 (or any SCCM pack) must not yield zero-duration A->B legs:
rows = find_buses_by_arrival_time("210021200945", "210021109680", day="mon",
                                  arrive_by="16:00")
assert not [r for r in rows if r["depart"] == r["arrive"]]
# and the Stevenage→Bedford 9A journeys must have monotonic times again
```

---

## BUG B (MED-HIGH) — `get_disruptions` ALWAYS returns "No matching disruption messages"

**Reproduced:** unfiltered call, and filtered `operator="TfGM"` — both return
"No matching disruption messages." Meanwhile the raw passthrough
(`SIRI_SX_Disruptions_siri_sx`) returns the same feed with **196
`PtSituationElement` records** (participants include TfGM, SYMCA, WYCA,
WestofEngland, Cornwall, Devon, Hampshire, Merseytravel, NorthLincolnshire).

**Root cause:** the DfT disruptions feed does not use `InfoMessage` wrappers at all —
situations sit directly under `SituationExchangeDelivery > Situations >
PtSituationElement` (verified: 0 occurrences of `InfoMessage`, 196 of
`PtSituationElement` in today's feed). `parse_siri_sx()` (`siri.py:105-133`) iterates
`root.iter(f"{{{SIRI_NS}}}InfoMessage")` (`siri.py:113`), so it returns `[]` — a
successful parse of an empty list, which `_fetch_sx` (`server.py:155-173`) caches and
`get_disruptions` (`server.py:230-232`) reports as "no matches".

**Fix:** parse `PtSituationElement` (descendant search is already the house style):
per situation map `CreationTime/RecordedAtTime`, `ParticipantRef`, `SituationNumber`,
`ValidityPeriod` (StartTime/EndTime), `Severity`, `Progress`, and the refs
(`OperatorRef`, `LineRef`, `StopPointRef`) that the filter contract in
`get_disruptions` (`server.py:201-235`) expects. Note `parse_cancellations`
(`siri.py:140-167`) iterates by element tag and is unaffected — cancellations work.

**Regression test:** fetch the live disruptions feed bytes (or a stored sample),
assert `parse_siri_sx(content)` yields > 0 messages and that `operator="TfGM"`
filtering keeps ≥ 1 when the feed contains TfGM.

---

## BUG C (MED) — OpenAPI passthrough tools silently ignore the `kwargs` string

**Reproduced twice:** `Data_set_api_v1_fares_dataset(kwargs="noc=SCCM,limit=5,status=published")`
and `(kwargs="limit=3")` both return the identical default page — `count: 803`,
25 results starting at Blackpool Transport. Same for
`timetables_api_v1_dataset(kwargs="search=Stevenage,limit=3")` → `count: 941`,
default page (Kinch Buses). The BODS API itself honors these params (verified direct:
`…/fares/dataset/?api_key=…&noc=BLAC&limit=5` → `count: 2`, Blackpool only).

**Root cause:** the generated tool function receives the client's arguments as ONE
string parameter `kwargs` (that's how the `**kwargs`-signature tools surface over
MCP), but `tool_func` (`server.py:1007-1028`) only forwards *named* OpenAPI
parameters — it never parses the `kwargs` string, so every query param is dropped and
the request goes out bare (`…/dataset/?api_key=…`).

**Fix:** at the top of `tool_func`, if `kwargs.get("kwargs")` is a string, parse it
(`parse_qsl` after comma→`&` normalisation, or `shlex` key=value pairs) and merge the
result into the kwargs dict before the named-param loop. Validate keys against
`params_def` and reject unknown ones loudly instead of silently.

**Regression test:** `fares(kwargs="noc=BLAC,limit=5")` must return `count == 2`.

---

## BUG D (MED) — board duplicate rows are back (day-mask twins + cross-dataset same-code)

**Reproduced post-delta today:** `get_departures_board(210021200945, mon, 14:45, 15)`
shows `Arriva UK Bus 55 vj_1 14:45 Southfields` **twice** (identical rows) and the
50-row version shows more pairs (390 vj_13 @14:50 ×2; near-dupes 56 vj_1 @15:22/15:25,
907A vj_2 @16:31/16:41). Round-4/5 verification had this at zero.

**Root cause (DB-level, no byte-twins exist):** the two 14:45 rows are journeys
`5189748` (days_mask 15 = Mon–Thu) and `5190048` (days_mask 31 = Mon–Fri) in ds 15766
— same route/direction/code, **identical stop-time signatures** (md5 equal, verified),
both matching Monday. `next_departures` groups only `BY jst.journey_id`
(`store.py:512`), so per-journey rows can never collapse semantic duplicates. The
index has **53,313 `(op, route, direction, code)` groups spanning more than one
dataset** (same operator name across packs), which multiplies the effect
(e.g. 55 vj_1 also exists in ds 15954; 390 vj_4 in 15654+16467).

```sql
-- byte-identical twins: 0 (so the round-4 exact-dup guard finds nothing)
SELECT COUNT(*) FROM (SELECT 1 FROM journeys
  GROUP BY ds_id,op,route,direction,code,days_mask,json HAVING COUNT(*)>1);
-- semantic groups spanning datasets: 53313
SELECT COUNT(*) FROM (SELECT op,route,direction,code FROM journeys
  GROUP BY 1,2,3,4 HAVING COUNT(DISTINCT ds_id)>1);
```

**Fix direction:** dedupe at the board/planner layer on the *effective* tuple —
`(op, route, direction, code, dep, dest)` after day filtering (same journey
republished under two day-masks, or in two sibling datasets, collapses to one row).
Keep `LIMIT` applied after dedupe. (Journey-level tools are affected the same way;
`plan_journey`'s B-side one-change output shows the 15-plan cap absorbing some of it.)

---

## BUG E (LOW, ops) — `load_timetable_delta` times out client-side; watermark not advanced

Observed sequence today: MCP call times out at 420 s; the delta **keeps running
server-side and completes ~5 min in** (journey count 766,135 → 766,088; WAL growth
stops at 14:48:44Z; worker container stays healthy; every other tool stayed
responsive throughout — the old zombie-container failure mode is gone). The gateway
then suppresses the late response ("Request 34 cancelled - duplicate response
suppressed"), so the caller never learns the outcome. **`meta.last_refresh` was NOT
advanced** (still `2026-09-13T19:14:20Z` after the delta), so per the error-budget
policy (`server.py:923-936`) the run must have exceeded its error budget — but the
caller can't see the failure list because the response was dropped; the next delta
will redo the same sweep.

Suggestions: (a) return early from the tool with a job/progress handle instead of
sweeping inside one MCP call; (b) on timeout, persist the delta summary (updated /
purged / errors / failed_ids) to a file in the volume so it survives the suppressed
response; (c) log the failed_ids at WARN so post-hoc inspection is possible from
container logs alone.

---

## Verified NOT bugs (behavior notes)

- `plan_journey(arrive_by=16:00)` returning 06:01 arrivals: documented "at most the
  15 earliest-arriving plans" — all 15 satisfy the deadline. surprising UX; callers
  should pass realistic deadlines or use `find_buses_by_arrival_time`.
- SIRI-VM and GTFS-RT raw passthroughs still hit the BODS 403 bot wall (unchanged
  from prior builds; wrapped live tools are unaffected).
  **[CORRECTED 2026-09-14, round 9:]** withdrawn — both passthroughs re-verified
  WORKING on `c23f093` (SIRI-VM: VehicleMonitoringDelivery XML; GTFS-RT: 30 KB
  protobuf); the historic failures match the kwargs comma trap +
  trailing-slash 301, not a bot wall. Full note in BUG_REPORT_ROUND9.md.
- `get_departures_board` Stop M (210021200780) having no rows: correct — the stop
  only appears as an `AnnotatedRef` declaration in ds 15766, no timing links.

## Environment for repro

- Build: `openbusdata-mcp:latest` = `33fb090f9c8a` (tag `105ee1d`), fork HEAD.
- Index: 943 datasets, 253,640 stops, 9,706 routes, 766,088 journeys (after today's delta).
- London local time used for all board queries (container is UTC — pass explicit
  `from_time`/`day`).