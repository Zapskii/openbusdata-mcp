# BUG REPORT — round 8: build `06b1e54` (PR #18, deployed 2026-09-14)

**Tested by:** independent E2E MCP session against the live deployed gateway,
17:45–18:15 BST. PR #18's four fixes (A–D) were re-verified and all hold:

- A flat journeys: DB census returns **0** flat rows; index 763,279 journeys,
  253,646 stops, 9,749 routes, 943 datasets. `find_buses_by_arrival_time`
  (210021200945 → 210021109680, mon, 16:00): 9A VJ321 08:02→08:18 etc., zero
  `depart==arrive` rows; `plan_journey` has no zero-duration legs.
- B disruptions: unfiltered `get_disruptions` → **417 messages**;
  `operator=FBRI` exact filter → 60 messages with operators/lines/stops populated.
- C kwargs: `fares(kwargs="noc=BLAC,limit=5")` → count 2;
  `timetables(kwargs="noc=SCCM,limit=3")` → count 15; unknown kwargs rejected
  loudly (`"unknown parameter(s) bogus_param. Valid: boundingBox, limit, noc, offset, status"`).
- D board dupes: `55 vj_1 @14:45` ×1, `390 vj_13 @14:50` ×1, 15 unique rows.
- No regressions: search_stops, plan_journey (direct+one-change), route stops
  (all four Arriva 101 ds packs), get_fare_prices (210 prices), live buses,
  estimate_live_eta (vehicle matched, schedule-offset ETA).

The only functional bug found this round is below. Bug E (delta
timeout/watermark) is unchanged and stays parked.

---

## BUG F (MED) — `get_cancellations` ALWAYS returns "No matching cancellation entries."

**Reproduced:** unfiltered call on the live build → `"No matching cancellation
entries."` Meanwhile the raw passthrough (`SIRI_SX_Cancellations_siri_sx_cancellations`)
returns the same feed at **27,441,166 bytes** containing **1,856
`PtSituationElement` records** — 821 `Progress=open`, 1,035 `closed`
(probe at ~18:10Z, 2026-09-14).

The PR #18 body's claim that "get_cancellations was unaffected (it iterates by
element tag and is unaffected)" is **false**: the tag it iterates does not
occur in the feed at all.

**Root cause (verified in the live feed bytes, not inferred):**

`parse_cancellations()` (`siri.py:164-191`) iterates elements and keeps only
tags in `CANCELLATION_TAGS` (`siri.py:160-161`):

```python
CANCELLATION_TAGS = {"VehicleJourneyCancellation",
                     "EstimatedVehicleJourneyCancellations"}
```

The DfT /siri-sx/cancellations feed carries **zero** occurrences of either tag
(verified: `VehicleJourneyCancellation` = 0, `EstimatedVehicleJourneyCancellations`
= 0, `InfoMessage` = 0). Every record is a **`PtSituationElement`** (same
shape family as round-7 bug B), carrying one `Affects > VehicleJourneys >
AffectedVehicleJourney` per situation:

- 1,856 situations, **1,856 AffectedVehicleJourney** (exactly one each —
  no multi-AVJ situations today, so per-situation == per-vehicle-journey)
- every AVJ carries: `DatedVehicleJourneyRef`, `OperatorRef` + `OperatorName`
  (1,856/1,856), `LineRef` + `PublishedLineName`, `DirectionRef`,
  `OriginAimedDepartureTime`, and a `Calls` stop list (`StopPointRef`,
  `StopPointName`, `Order`, `AimedDepartureTime`)
- situation level: `CreationTime`, `ParticipantRef`, `SituationNumber`,
  `Progress` (open/closed), `ValidityPeriod` (StartTime/EndTime),
  `MiscellaneousReason`
- **no `Summary`/`Description`/`Report` anywhere** (0/1,856) — these are
  data-only records; `MiscellaneousReason` is `"unknown"` in all 1,856

So the keep-filter matches nothing → `parse_cancellations` returns `[]` →
`_fetch_sx("cancellations")` caches the empty list (a *successful* parse, so
no "unavailable" branch) → `get_cancellations` (`server.py:239-267`) reports
"No matching cancellation entries."

Top operators in the live feed: SCEK 1,093, SYRK 129, SCNE 93, SCSO 93,
SDVN 57, SCEM 56 (Stagecoach family dominates — these are real, filterable NOCs).

**Repro without the DB (key comes from the container env; do not print it):**

```bash
docker cp probe.py obd-gateway:/tmp/probe.py   # probe.py below
docker exec obd-gateway python3 /tmp/probe.py
```

```python
# probe.py
import os, urllib.request, xml.etree.ElementTree as ET
key = os.environ["OPENBUS_API_KEY"]
url = f"https://data.bus-data.dft.gov.uk/api/v1/siri-sx/cancellations/?api_key={key}"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 openbusdata-mcp"})
content = urllib.request.urlopen(req, timeout=120).read()
print("bytes:", len(content))                       # 27,441,166
SIRI = "http://www.siri.org.uk/siri"
root = ET.fromstring(content)
sits = list(root.iter("{%s}PtSituationElement" % SIRI))
print("PtSituationElement:", len(sits))             # 1856
print("VehicleJourneyCancellation:",
      sum(1 for el in root.iter() if el.tag.rsplit("}",1)[-1]
          in ("VehicleJourneyCancellation","EstimatedVehicleJourneyCancellations")))  # 0
```

MCP-side contrast on the deployed build:

```text
get_cancellations()                                   -> "No matching cancellation entries."
SIRI_SX_Cancellations_siri_sx_cancellations(kwargs="") -> 27.4 MB XML, 1856 PtSituationElement
```

**Suggested fix (in `parse_cancellations`, siri.py ~164-191):**

Mirror the round-7 bug-B fallback in `parse_siri_sx` (`siri.py:133-157`): a
successful parse of zero cancellations is not an empty feed — fall back to the
PtSituationElements. Concretely:

1. After the existing loop, `if not out:` iterate
   `root.iter(f"{{{SIRI_NS}}}PtSituationElement")`; for each, emit one entry
   per `AffectedVehicleJourney` (house style: descendant search, stripped):
   - `recorded_at` = `CreationTime` (or `RecordedAtTime`)
   - `vehicle_journey_ref` = `DatedVehicleJourneyRef`
   - `operator` = `OperatorRef` (under the AVJ's `Operator`)
   - `line` = `LineRef` (fall back to `PublishedLineName`)
   - `origin` = `OriginAimedDepartureTime` (keeps the existing key's meaning:
     "where/when the cancelled journey starts"; `OriginRef` not present in this feed)
   - `destination` = `DestinationRef`/`DestinationName` if present (absent here)
   - `reason` = `MiscellaneousReason` (present but "unknown" in practice)
   - NEW: `progress` (`open`/`closed`), `valid_from`/`valid_until` from
     `ValidityPeriod`, `published_line` (`PublishedLineName`),
     `first_stop`/`first_stop_name` from the first `Call` (the stop list is
     per-journey and can be long — don't inline all Calls)
2. `get_cancellations` (`server.py:239-267`) keeps its exact-match
   `operator`/`line` filter contract — the entry keys above already fit
   (`e["operator"]`, `e["line"]`). No tool-layer change needed.
3. Optionally surface `progress` so callers can drop stale `closed` rows
   (1,035 of 1,856 at probe time); do NOT hard-drop closed rows inside the
   parser — keep it a caller decision.
4. Size note: 1,856 entries is a large success payload (~1-2 MB JSON). If
   desired, add an optional `limit` (default e.g. 50) applied after filtering,
   mirroring `get_departures_board`; unfiltered behaviour unchanged for
   programmatic callers passing a large limit.

**Regression test:**

```python
# stored fixture: trim the live feed to ~2 PtSituationElements (open + closed)
entries = parse_cancellations(open("tests/fixtures/cancellations_pse.xml","rb").read())
assert len(entries) == 2
assert all(e["vehicle_journey_ref"] and e["operator"] and e["line"] for e in entries)
assert {e["progress"] for e in entries} == {"open", "closed"}

# live: parse must be non-empty on today's feed bytes
assert len(parse_cancellations(fetch_cancellations_bytes())) > 0
# and the wrapped tool's operator filter keeps real NOCs:
out = await get_cancellations(operator="SCEK")
assert "No matching cancellation entries." not in out
```

---

## Verified NOT bugs (behavior notes, this round)

- `plan_journey` `disruption_alerts` annotated Stevenage Arriva plans with
  "route 100/101 has an active disruption": the FBRI (Bristol) feed carries
  line refs `"100"`/`"101"` and annotation matches by line name across
  regions. By design — annotation never drops or reorders plans. Worth a
  docstring note someday; not a defect.
- `get_fare_prices(901, origin_zone="CEN", destination_zone="BIR")` →
  "No fare prices match the given zones (210 prices extracted)" is data-true:
  ds 901's Blackpool zones are `fs@*` refs (verified by the unfiltered ds
  16583 call returning 210 rows with `fs@...` zones).
- Raw SIRI-VM / GTFS-RT passthroughs still hit the BODS 403 bot wall
  (unchanged from prior builds; wrapped live tools unaffected).

## Environment for repro

- Build: `openbusdata-mcp:latest` = `06b1e54` (PR #18 merge), image
  `0b1bc380f952`, gateway `obd-gateway` started 2026-09-14T16:48Z, restarts=0.
- Index: 943 datasets, 253,646 stops, 9,749 routes, 763,279 journeys,
  0 flat-time journeys (post 290/290 flat-ds reload).
- Live-feed numbers probed ~17:45–18:15Z 2026-09-14 (disruptions 417 msgs /
  ~2 MB; cancellations 1,856 situations / 27.4 MB).
---

## RETEST (2026-09-14, post-fix)

**Bug F: FIXED & DEPLOYED.** PR #19 (merge `71f9fd6`) adds the PtSituationElement
fallback to `parse_cancellations` (`siri.py`): on a successful zero-tag parse,
one entry per `AffectedVehicleJourney` with `recorded_at`, `vehicle_journey_ref`,
`operator`, `line` (+`published_line` fallback), `origin` =
`OriginAimedDepartureTime`, `destination`, `reason` = `MiscellaneousReason`, plus
`progress`, `valid_from`/`valid_until` (ValidityPeriod), `first_stop`/
`first_stop_name` (first Call only). Tag-record path unchanged; fallback fires
only when it matched nothing. `get_cancellations` untouched — exact-match
`operator`/`line` filter contract preserved.

- Suite: 175 passed secrets-stripped (172 pre-existing + 3 new: parser PSE
  fallback keys, tag-records-win guard, wrapped-tool filter on the PSE shape).
- Live-data proof pre-deploy (new parser vs real feed bytes): 28.8 MB →
  1,958 entries (0 before), SCEK exact-match → 1,113, open/closed 859/1,099.
- Deploy: image `openbusdata-mcp:71f9fd6` tagged `latest`, gateway recreated
  `-i --restart unless-stopped`, restarts=0, index ready line unchanged
  (943 datasets / 763,279 journeys). Post-deploy MCP stdio proof:
  `get_cancellations(operator="SCEK")` → **1,115 entries**, all 13 keys present.

Still parked: bug E (delta timeout/watermark). Optional `limit` param on
`get_cancellations` (~1-2 MB unfiltered payload) remains a caller decision,
not implemented.
