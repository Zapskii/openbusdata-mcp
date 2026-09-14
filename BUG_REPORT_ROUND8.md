
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