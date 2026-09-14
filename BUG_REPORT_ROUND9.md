# BUG REPORT — round 9: build `71f9fd6` (PR #19, deployed 2026-09-14)

**Tested by:** independent E2E MCP session against the live deployed gateway
(`obd-gateway`, image `openbusdata-mcp:71f9fd6`), 18:40–19:20 BST. Full tool
census via `tools/list`: **22 tools registered** (13 decorated + 9 dynamic
passthroughs) — matches the PR-#18 expectation.

PR #19's bug-F fix holds: `get_cancellations` unfiltered returns a **782 KB
structured payload** (was "No matching cancellation entries."); filtered
`operator=NOPE` → exact-match no-match message; `operator=SCEK` post-deploy
earlier this evening → 1,115 entries.

## Round-9 bugs

---

### BUG G (LOW) — plan_journey plans a round trip for same origin/destination

**Reproduced:** `plan_journey(stop_a=210021200945, stop_b=210021200945, day=mon,
from_time=16:00, arrive_by=17:00)` returns **15 "change" plans**, each a
two-leg loop *away from the stop and back*: e.g. leg 1 SB12
Stevenage Rail St Stop N → Stevenage Bus Stn A (07:16→07:18), leg 2 route 80
Stevenage Bus Stn A → Stevenage Rail St Stop N (07:20→07:21). The caller asked
for a journey from a stop to itself — the sane answer is an empty/zero-leg plan
(or a trivial "you are already there" result), not 15 round trips through the
interchange. `total_changes: 1` for a self-journey also misreports (0 changes).

**Root cause (code inspection, consistent with the repro):** the planner seeds
candidate legs from stop_a and accepts any leg chain that reaches stop_b; a
leg pair (A→X, X→A) terminates at stop_b trivially. No early exit for
`stop_a == stop_b` and no filter rejecting legs that re-board at the origin.

**Suggested fix (server.py, planner):** if `stop_a == stop_b` (resolved
NaPTANs), return a single zero-leg plan (`type: "none", legs: [],
total_changes: 0`) before entering the search; independently, skip
two-leg combinations whose second leg boards at stop_a (a self-loop is
pointless for any origin). Regression test in the planner's topical test file
with a seeded two-route graph.

**Severity rationale:** LOW — the answer is still "possible journeys between
these stops", and real callers pass distinct stops; but it's semantically
wrong and wastes a 9 KB payload.

---

### BUG H (MED) — get_fare_prices reads only the FIRST .xml member of a
fares zip (round-8's "210 prices" was the same artifact)

**Reproduced:** `get_fare_prices("16583")` → **171 prices**. The ds-16583
download zip carries **19 NeTEx files totalling 2,857 price rows** (verified by
parsing every member with the real `parse_fare_prices` in the gateway
container). `extract_xml_bytes` (`fares.py:73-81`) returns the **first**
`*.xml` member only; the other 18 files are silently dropped.

**Correction to round 8:** round-8's report cited "210 prices extracted" and
called the count data-true. A 210-row member EXISTS in the same zip
(`FX-..._14_O_Adult-single_..._4b01.xml`), i.e. the earlier count was this same
first-member artifact on a different first file — not upstream drift. The
round-8 "Verified NOT bugs" note about ds-16583 zone shape (`fs@*`) is
**unaffected** (zone refs are genuinely `fs@...` in all members; re-verified:
2857 rows, 2,375 unique, all zone prefixes `fs`).

**Note:** the zip member order was byte-identical across two downloads
tonight, so the count is *stable for a given serve* but *arbitrary across
serves/datasets* — any fares count from this tool is "rows of whatever file
BODS happens to list first".

**Suggested fix (fares.py `extract_xml_bytes`, or a zip-aware wrapper at the
`parse_fare_prices` call site in `server.py:1557`):** when content is a PK
zip, concatenate the price rows of EVERY `.xml` member (per-file
`parse_fare_prices`, extend), not just the first member. Keep
`extract_xml_bytes` for genuine single-document callers or add
`extract_all_xml_bytes() -> list[bytes]`. Tool layer (`get_fare_prices`,
zone filters) unchanged — filters run over the aggregated rows.

**Regression test:** multi-member zip fixture (2 files, different zone refs);
assert the tool sees rows from BOTH members. Real-data check: ds-16583 count
must equal 2,857 (recompute at run time; the fixture guards the code path).

---

## Verified NOT bugs (data notes, this round)

- **BODS catalogue shrank 943 → 941 datasets; delta purged 2.**
  `load_timetable_delta` → "0 refreshed, 2 purged, 0 errors"; totals
  763,304 → 763,290 journeys, 9,749 → 9,748 routes. BODS direct
  `dataset/?limit=1` count = **941** — the tool's purge matches upstream
  truth exactly. Not a bug.
- **BODS fares catalogue count = 803** (`Data_set_api_v1_fares_dataset`
  count 803 == direct curl 200 with count 803). Not a bug.
- ds-16583 fare-row count change 210 → 171 is NOT upstream drift: same zip
  member set both rounds; round-8's 210 was the first-member artifact
  (bug H above). Round-8's zone-shape conclusion stands.
- Index health on `71f9fd6` post-delta: 253,646 stops, 9,748 routes,
  763,290 journeys, 941 datasets; exact twins **0**; ds_id=0 orphans **0**;
  flat-journey guard **0**; stop_to_routes dup groups **0**; coord coverage
  125,963/253,646 = **49%** (source-data-bounded, unchanged family).
- GTFS-RT passthrough → BODS 403 bot wall: unchanged from prior builds,
  environment-level (same raw UA/key gets 403 via curl from the container);
  not a tool defect. SIRI-VM passthrough works
  (`boundingBox=0.1%2C51.0%2C0.2%2C51.1` → real VehicleMonitoringDelivery XML).
- **kwargs comma trap re-confirmed on this build** (round-9 candidate G from
  the mid-round report): raw `boundingBox=0.1,51.0,0.2,51.1` →
  "unknown parameter(s) 0.2, 51.0, 51.2"; percent-encoded `%2C` → success.
  LOW, docstring-level; candidate for a doc note, not necessarily a code fix.

## Error contracts (all correct)

- `get_departures_board("Nowhere Street")` → "Could not resolve stop"
- `get_departures_board(..., from_time="25:99")` → "Invalid time format"
- `search_stops("")` → "No stops found matching"
- `plan_journey(stop_a == stop_b)` → the BUG G payload (see above)
- `get_cancellations(operator="NOPE")` → exact-match no-match message (correct)
- `get_route_stops("Stagecoach", "53")` → 65 KB multi-ds sibling payload (correct)
- passthrough unknown kwargs → loud rejection with the valid list (correct)

## Environment for repro

- Build: `openbusdata-mcp:71f9fd6` (PR #19 merge), gateway `obd-gateway`
  recreated 18:33 BST, restarts=0; index ready 253,646 stops / 9,749 routes /
  763,279 journeys / 943 datasets (pre-delta; 941 datasets post-delta).
- All live-feed probes ~18:40–19:20 BST 2026-09-14; fares zip probed from the
  gateway container (19 members, 373,601 bytes, member order stable tonight).

## Parked

- Bug E (delta timeout/watermark): unchanged, parked ops.
- Bug G, Bug H: awaiting Simon's verdict before any fix PR.
- Optional `limit` param on `get_cancellations` (782 KB payload tonight):
  still a caller decision, not implemented.
- GTFS-RT 403: upstream bot wall, not actionable in the fork.