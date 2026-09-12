---
name: openbusdata-mcp-endpoints
description: Use when calling, documenting, or extending the openbusdata-mcp MCP server — choosing which of its 22 tools answers a question, looking up a tool's parameters or return shape, or wiring up a new endpoint. Covers both registration paths: the 13 tools written in server.py and the 9 generated at startup from the bundled OpenAPI specs.
---

# OpenBusData MCP Endpoints

## Overview

The server registers **22 tools**. They arrive by **two different routes**, and
that is the single fact worth knowing:

1. **13 hand-written tools** — functions decorated `@mcp.tool()` in `server.py`.
2. **9 generated tools** — built at startup by `register_tools_from_specs()` from
   the five OpenAPI specs in `src/openbusdata_mcp/openapi-schema/*.yml`.

A grep for `@mcp.tool()` finds **13**, not 22. The generated nine come from one
programmatic call — `mcp.tool(name=tool_name)(tool_func)` — so any count derived
from the decorator is wrong by nine. This is the server's own recurring lesson:
enumerate by the property (every registration site), not by a proxy for it.

**The nine generated tools are registered but currently cannot be called with
their parameters.** Read that section before relying on any of them.

## Which tool answers your question

| You want | Call |
|---|---|
| Stops matching a name | `search_stops` |
| Routes serving two stops | `find_routes_between_stops` |
| Ordered stop list for a route | `get_route_stops` |
| Scheduled buses between two stops by a time | `find_buses_by_arrival_time` |
| A journey, optionally with changes | `plan_journey` |
| Next departures at one stop | `get_departures_board` |
| Where buses are right now | `get_live_buses_on_route` |
| When a live bus reaches a stop | `estimate_live_eta` |
| What is disrupting services | `get_disruptions` |
| What is cancelled | `get_cancellations` |
| What a journey costs | `get_fare_prices` (id from `Data_set_api_v1_fares_dataset` †) |
| Timetable dataset catalogue | `timetables_api_v1_dataset` † |
| Timetable dataset metadata | `timetables_api_v1_dataset_by_datasetID` † |
| Raw SIRI-VM / GTFS-RT vehicle feeds | `SIRI_VM_Data_feed_api_v1_datafeed` †, `GTFS_RT_Data_feed_api_v1_gtfsrtdatafeed` † |
| Raw SIRI-SX feeds | `SIRI_SX_Disruptions_siri_sx` †, `SIRI_SX_Cancellations_siri_sx_cancellations` † |

† **Generated tool — takes no usable parameters today.** See below.

## Load first

Every timetable-backed tool returns the string
`No timetable data loaded. Please call load_timetable_index() first.` until the
index exists. Call `load_timetable_index` once (slow: downloads every dataset),
then `load_timetable_delta` for cheap subsequent refreshes.

The live and disruption tools do **not** need the index — except
`estimate_live_eta`, which needs it to match vehicles to timetable journeys.

## Return shape — the trap

**Every hand-written tool returns either a JSON string or a prose string.**
A miss is not an exception and not an empty list; it is prose such as
`No stops found matching "x".`, `No live buses found.`, or
`Disruptions feed unavailable (fetch or parse failed).`

Never `json.loads` a result without checking. Guard on the first character:

```python
result = search_stops("High Street")
data = json.loads(result) if result.lstrip()[:1] in "[{" else result
```

## The 13 hand-written tools

| Tool | Parameters | Returns |
|---|---|---|
| `load_timetable_index` | `force_refresh=False` | Status text |
| `load_timetable_delta` | `since=""`, `reconcile=True` | Status text |
| `search_stops` | `query` | JSON list of stops + NaPTAN codes |
| `find_routes_between_stops` | `stop_a`, `stop_b` | JSON list, else "No single route serves both…" |
| `get_route_stops` | `operator`, `route`, `direction=None` | JSON ordered stops, else "No route found." |
| `find_buses_by_arrival_time` | `stop_a`, `stop_b`, `arrive_by`, `day=None` | JSON list |
| `plan_journey` | `stop_a`, `stop_b`, `arrive_by`, `day=None`, `max_changes=1`, `check_disruptions=True` | JSON, **at most the 15 earliest plans** |
| `get_departures_board` | `stop`, `day=None`, `from_time=None`, `limit=20` | JSON board |
| `get_live_buses_on_route` | `operator_ref`, `line_ref`, `origin_ref=None`, `destination_ref=None`, `vehicle_ref=None`, `bounding_box=None` | JSON vehicles |
| `estimate_live_eta` | `stop`, `operator_ref`, `line_ref`, `day=None` | JSON `{target_stop, vehicles}` |
| `get_disruptions` | `operator=None`, `line=None`, `stop=None` | JSON list |
| `get_cancellations` | `operator=None`, `line=None` | JSON list |
| `get_fare_prices` | `dataset_id`, `origin_zone=None`, `destination_zone=None` | JSON prices |

Stops are accepted as a **NaPTAN code or a name substring**. Times are `HH:MM`
24h; an unparseable time returns an error string, it does not raise. Days are
`mon`…`sun`, defaulting to today.

Details worth knowing:

- **`plan_journey` caps output at 15 plans**, with or without disruption
  annotation. `check_disruptions=True` adds a `disruption_alerts` list; it never
  drops, reorders, or changes the plan count, and a feed failure degrades
  silently.
- **`get_disruptions` / `get_cancellations` filter client-side and match exactly.**
  The upstream endpoint takes no query parameters. `"OPX"` matches a message
  carrying `<OperatorRef>OPX</OperatorRef>` — never a substring.
- **`get_live_buses_on_route` filters server-side**, so the datafeed does the
  narrowing rather than the client.
- **`estimate_live_eta` is honest estimation, not prediction** — it assumes
  vehicles run to schedule from their matched position.

## The 9 generated passthrough tools

Built from the specs; **none of the specs define `operationId`**, so every name
comes from the fallback `{tag}_{path}` rule and is therefore long and ugly above.
That naming is mechanical, and adding an `operationId` to a spec would rename
the tool — a breaking change to anyone calling it.

| Tool | Upstream | Useful parameters |
|---|---|---|
| `SIRI_VM_Data_feed_api_v1_datafeed` | `GET /api/v1/datafeed` | `operatorRef`, `lineRef`, `vehicleRef`, `boundingBox`, `producerRef`, `originRef`, `destinationRef` |
| `SIRI_VM_Data_feed_api_v1_datafeed_by_datafeedID` | `GET /api/v1/datafeed/{datafeedID}/` | `datafeedID` (required) |
| `GTFS_RT_Data_feed_api_v1_gtfsrtdatafeed` | `GET /api/v1/gtfsrtdatafeed/` | `boundingBox`, `routeId`, `startTimeAfter`, `startTimeBefore` |
| `SIRI_SX_Disruptions_siri_sx` | `GET /siri-sx` | (none) |
| `SIRI_SX_Cancellations_siri_sx_cancellations` | `GET /siri-sx/cancellations` | (none) |
| `Data_set_api_v1_fares_dataset` | `GET /api/v1/fares/dataset` | `noc`, `status`, `boundingBox`, `limit`, `offset` |
| `Data_set_api_v1_fares_dataset_by_datasetID` | `GET /api/v1/fares/dataset/{datasetID}` | `datasetID` (required) |
| `timetables_api_v1_dataset` | `GET /api/v1/dataset` | `adminArea`, `noc`, `limit`, `offset`, `search`, `status`, `modifiedDate`, `dqRag`, `bodsCompliance`, date ranges |
| `timetables_api_v1_dataset_by_datasetID` | `GET /api/v1/dataset/{datasetID}` | `datasetID` (required) |

- **The two fares tools differ only by `_by_datasetID`** — the easy mix-up. The
  bare one lists the catalogue (this is where a `dataset_id` for
  `get_fare_prices` comes from); the `_by_datasetID` one returns that dataset's
  metadata, including the download URL. Same shape of trap for timetables.
- The `api_key` is appended automatically. Never pass it yourself.
- Responses always pass through credential redaction, success and failure alike.

### These nine take no usable parameters

Verified against the live registry under the pinned **mcp 1.30.0**. Every one of
the nine advertises this schema:

```
properties={'kwargs': {'type': 'string'}}   required=['kwargs']
```

Their real upstream parameters are **absent from the schema**, because the
generated function's signature is `async def tool_func(**kwargs)`. A conforming
MCP client therefore cannot pass `limit`, `noc`, `routeId`, or even the
`{datasetID}` path parameter:

Measured on the real registered tool, not a replica:

```
call_tool("timetables_api_v1_dataset", {"limit": 2})   → REJECTED
call_tool("timetables_api_v1_dataset", {})             → REJECTED
call_tool("timetables_api_v1_dataset", {"kwargs":"x"}) → ACCEPTED, and requests
    https://data.bus-data.dft.gov.uk/api/v1/dataset?api_key=…
```

So the only accepted shape is `{"kwargs": "<string>"}`. It succeeds, and the
request that leaves the process is the **bare path** — `kwargs` holds one string
that matches no entry in the spec's parameter loop, so the tool contributes no
query parameter of its own (the `api_key` is appended by the server, not by the
caller). The two `_by_datasetID` tools never get that far: with no way to supply
`datasetID`, they are rejected at validation, so they cannot address a dataset
at all.

Hand-written tools are unaffected — `get_departures_board` advertises
`stop, day, from_time, limit` with `required=['stop']`, exactly as documented.

What follows for callers:

- **Prefer the hand-written tools.** `get_disruptions`, `get_cancellations`,
  `get_departures_board`, `estimate_live_eta`, `get_live_buses_on_route` and
  `get_fare_prices` all accept their documented parameters.
- **`get_fare_prices` needs an id the server cannot give it.** The catalogue
  tool that lists fares datasets is one of the nine, so ids must come from a
  bare (unfiltered) call to it or from outside the server.
- **The RUNBOOK smoke test does not work as written.**
  `call tool timetables_api_v1_dataset {limit: 2}` fails validation in mcp 1.30.0.

Attribution, so nobody misreads this as new: the `**kwargs` shape is unchanged
from the release base, but `uv.lock` pinning mcp 1.30.0 arrived with the
dependency-bound fix — so the breakage is plausibly a consequence of that
upgrade rather than of the server code. Not proven here.

Fix direction: give the generated function an explicit signature built from the
spec's parameters (or attach a per-tool JSON schema) so FastMCP derives a real
schema instead of the `**kwargs` placeholder.

## Enumerating the surface yourself

```bash
# The authoritative list — the runtime registry, both paths at once.
# list_tools() is SYNC in mcp 1.x (no await). Import runs ensure_schema() and
# creates a cache DB, so point HOME at a temp dir if you do not want that.
uv run python -c "
import openbusdata_mcp.server as s
tools = s.mcp._tool_manager.list_tools()
print(len(tools), [t.name for t in tools])
"

# Static derivation, no server import, no key, no network:
uv run python .claude/skills/openbusdata-mcp-endpoints/scripts/list_endpoints.py
```

Corroboration that costs nothing: `docs/RUNBOOK.md` states the surface as 17 + 5,
and `test_robustness.py` resolves a generated tool by name from the live manager.

## Common mistakes

| Mistake | Reality |
|---|---|
| Counting tools by grepping `@mcp.tool()` | Yields 13. The other 9 are registered by a loop. |
| Assuming a tool name from the upstream path | Names are `{tag}_{path}`; specs carry no `operationId`. |
| Passing documented parameters to a generated tool | Its schema accepts only `kwargs`; validation fails. Use a hand-written tool. |
| `json.loads` on every result | Misses and failures return prose, not JSON. |
| Calling a timetable tool before loading | Returns a prose "call `load_timetable_index()` first" string. |
| Passing `api_key` to a passthrough tool | Appended automatically; the caller never supplies it. |
| Using `Data_set_api_v1_fares_dataset_by_datasetID` to list fares datasets | That is the by-id metadata call; the bare name lists the catalogue. |

## Adding an endpoint

Hand-written: decorate with `@mcp.tool()` in `server.py`. If the result derives
from a remote call, pass it through `_redact` on **every** return — success as
well as failure.

Spec-driven: add a GET operation to a `.yml` in `openapi-schema/`; it is
registered at startup with no code change. Give it an `operationId` to control
the tool name. Note it will inherit the `**kwargs` schema problem above —
building the generated function with a real signature is what makes its
parameters usable.
