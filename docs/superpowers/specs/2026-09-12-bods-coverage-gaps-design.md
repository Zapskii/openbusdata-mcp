# BODS Coverage Gaps — Design

**Date:** 2026-09-12
**Status:** Approved (user selected all 6 gaps from the coverage analysis)
**Base:** upstream `main` @ `9e37b52` (merge of PR #12, 2026-09-12)

## Problem

The MCP server exposes every documented BODS v1 endpoint (9 spec-generated passthrough tools + 8 hand-written tools), but six feature-level gaps remain for the travel agent. This design closes all six.

## The six gaps and their designs

### G1. Server-side AVL filtering in `get_live_buses_on_route`
Today the tool fetches the full AVL datafeed and filters nothing; the API accepts `operatorRef`/`lineRef`/`originRef`/`destinationRef`/`vehicleRef`/`boundingBox` server-side. The tool gains optional parameters (`origin_ref`, `destination_ref`, `vehicle_ref`, `bounding_box` — bounding box as `"minLon,minLat,maxLon,maxLat"` per the AVL spec), passed straight through to the datafeed URL. The 20s TTL cache key becomes the full filter tuple, and the cache stores the raw response bytes (so other tools can reuse the same fetch).

### G2. SIRI-SX disruptions & cancellations as structured JSON
Both feeds pass through as raw XML. New module `src/openbusdata_mcp/siri.py` parses them into flat dicts using **descendant search** (`.//{ns}Tag`) so the parsers tolerate BODS's nesting; a live-probe step during implementation records the real element structure in the module docstring and adjusts the module-local tag constants if reality differs. Two new tools:

- `get_disruptions(operator=None, line=None, stop=None)` — one record per `InfoMessage`: recorded_at, valid_until, channel, severity, operators, lines, stops, summary, description. The endpoints accept no query params, so filtering is client-side.
- `get_cancellations(operator=None, line=None)` — one record per cancellation entry: recorded_at, vehicle_journey_ref, operator, line, origin, destination, reason.

Both carry a 60s TTL cache (`TTLCache`). SIRI-VM parsing moves from inline code in `get_live_buses_on_route` into `siri.parse_siri_vm(content)` (same output shape, lat/lon as floats or `None`; the tool renders `None` as `"N/A"` so its output shape is unchanged).

### G3. Departures board
`store.next_departures(naptans, day, from_time, limit)` — indexed SQL over `journey_stop_times` (the `jst_n(naptan, dep)` index) joined to `journeys.days_mask`, destination = name of each journey's final stop (correlated `MAX(seq)` subquery, `jst_j` index). New tool `get_departures_board(stop, day=None, from_time=None, limit=20)`; day/from_time default to today/now; limit clamped 1–50.

### G4. Live ETA at a stop
New tool `estimate_live_eta(stop, operator_ref, line_ref, day=None)`. Honest schedule-offset estimation, not prediction:

1. `store.route_stop_coords(operator, route)` — the route's ordered stop list with lat/lon (routes.stops JSON joined to stops).
2. Fetch AVL server-side-filtered by `operatorRef`+`lineRef` (reuses G1's fetch + cache).
3. For each vehicle with coordinates: nearest route stop by haversine (skip if > 2 km away).
4. `store.journeys_on_route(operator, route, day, limit=300)` — today's profiles with per-stop dep/arr.
5. Match the running journey: among profiles whose scheduled time at the nearest stop is ≤ now, take the latest; if none, take the earliest profile (next service, "scheduled" basis).
6. ETA at the target stop = now + (scheduled target arrival − scheduled nearest-stop time), clamped 0–180 min, labeled `"schedule-offset"`. Vehicles already past the target stop are reported as such.

Known simplifications, documented in the tool docstring: the route stop list is direction-merged, service-day times past midnight are compared as published (`24:xx` sorts after now before midnight), and the estimate assumes the vehicle runs to schedule from its matched position.

### G5. Disruption-aware journey planning
`plan_journey` gains `check_disruptions: bool = True`. After plans are built it fetches the cached disruptions payload (G2), collects affected `LineRef`/`OperatorRef` sets, and **annotates** matching plans with a `disruption_alerts` list (route-level match takes priority over operator-level). Annotation only — plans are never dropped, and a disrupted-looking route remains visible. If the disruptions feed is unreachable, planning proceeds silently unannotated; a live-feed error must never fail journey planning.

### G6. Fares lookup (NeTEx price extraction)
New module `src/openbusdata_mcp/fares.py`:

- `extract_xml_bytes(content)` — unzips `PK`-magic payloads (first `.xml` member), else passes through.
- `parse_fare_prices(content)` — walks every element whose local name is a fare-price tag (`FarePrice`/`farePrice`), extracts amount (`Amount`/`PriceAmount` child or `amount` attribute), currency (attribute, default `"GBP"`), start/end zone refs (`StartTariffZoneRef`/`EndTariffZoneRef`/`TariffZoneRef` found by walking up via a parent map), and the enclosing product's `Name`. Namespace-agnostic (local-name matching) because BODS NeTEx namespaces vary.

New tool `get_fare_prices(dataset_id, origin_zone=None, destination_zone=None)`: metadata `GET /api/v1/fares/dataset/{id}/` → `url` field → download (mirrors the timetable loader's `?api_key=` pattern) → parse → filter by zone membership when zones are given. Scope limit, stated openly: this extracts published price points; mapping tariff zones onto geographic stops is not attempted (zone→stop topology is a separate dataset concern).

## New tool surface (5 new tools, 1 extended)

| Tool | New/Changed |
|---|---|
| `get_live_buses_on_route` | +4 optional filter params; output shape unchanged |
| `get_disruptions`, `get_cancellations`, `get_departures_board`, `estimate_live_eta`, `get_fare_prices` | new |

17 → 22 tools. README and RUNBOOK updated in the final task.

## Live-structure uncertainty and the probe pattern

The exact element structure of the live SIRI-SX feeds and NeTEx fares documents can't be pinned from the bundled OpenAPI specs alone (the SX specs document no parameters and the NeTEx payloads aren't spec'd at all). Every such parser therefore follows a **probe pattern**: the implementation task fetches one real document (`curl`, key from env, never printed), records the real element paths in the module docstring, and keeps its tag names in module-level constants so a structure change is a one-place fix. Tests always use synthetic fixtures — the live probe never gates the test suite.

## Architecture rules (cont.)

- **One new dependency: `defusedxml`.** All XML parsing of remote data goes through `defusedxml.ElementTree.fromstring` (billion-laughs / entity-expansion safe). stdlib `zipfile`, `io`, `math` for the rest.
- **SIRI-VM raw fetch is shared.** `_fetch_siri_vm(filters)` in `server.py` (raw bytes + `_LIVE_CACHE`, 20s, empty feeds not cached — preserves the existing ruling) serves both `get_live_buses_on_route` and `estimate_live_eta`.
- **Feed failures degrade, never crash tools.** Every live-feed consumer returns a readable error string or silently unannotated output.
- **Store reads stay read-only** (`TimetableStore`); no schema changes, no migrations — every query rides existing indexes.
- **Tests:** TDD per task; suite currently 68, target ~95; run via `~/.local/bin/uv run --extra dev pytest -q`.
- **stdio hygiene:** diagnostics to stderr only.