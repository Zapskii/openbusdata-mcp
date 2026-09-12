---
name: openbusdata-mcp-endpoints
description: Use when calling, documenting, or extending the openbusdata-mcp MCP server — choosing which of its 17 tools answers a question, looking up a tool's parameters or return shape, or wiring up a new endpoint. Covers both registration paths: the 8 tools written in server.py and the 9 generated at startup from the bundled OpenAPI specs.
---

# OpenBusData MCP Endpoints

## Overview

The server registers **17 tools**. They arrive by **two different routes**, and
that is the single fact worth knowing:

1. **8 hand-written tools** — functions decorated `@mcp.tool()` in `server.py`.
2. **9 generated tools** — built at startup by `register_tools_from_specs()` from
   the five OpenAPI specs in `src/openbusdata_mcp/openapi-schema/*.yml`.

A grep for `@mcp.tool()` finds **8**, not 17. The generated nine come from one
programmatic call — `mcp.tool(name=tool_name)(tool_func)` — so any count derived
from the decorator is wrong by nine. This is the server's own recurring lesson:
enumerate by the property (every registration site), not by a proxy for it.

**The nine generated tools are registered but cannot be called with their
parameters.** Read that section before relying on any of them.

The count moves when a tool or a spec is added — re-run the enumerator below
rather than trusting this document's number.

## Which tool answers your question

| You want | Call |
|---|---|
| Stops matching a name | `search_stops` |
| Routes serving two stops | `find_routes_between_stops` |
| Ordered stop list for a route | `get_route_stops` |
| Scheduled buses between two stops by a time | `find_buses_by_arrival_time` |
| A journey, with or without one change | `plan_journey` |
| Where buses are right now | `get_live_buses_on_route` |
| Timetable dataset catalogue | `timetables_api_v1_dataset` † |
| Timetable dataset metadata | `timetables_api_v1_dataset_by_datasetID` † |
| Fares dataset catalogue | `Data_set_api_v1_fares_dataset` † |
| Fares dataset metadata | `Data_set_api_v1_fares_dataset_by_datasetID` † |
| Raw SIRI-VM / GTFS-RT vehicle feeds | `SIRI_VM_Data_feed_api_v1_datafeed` †, `GTFS_RT_Data_feed_api_v1_gtfsrtdatafeed` † |
| Raw SIRI-SX feeds | `SIRI_SX_Disruptions_siri_sx` †, `SIRI_SX_Cancellations_siri_sx_cancellations` † |

† **Generated tool — takes no usable parameters today.** See below.

## Load first

Every timetable-backed tool returns the string
`No timetable data loaded. Please call load_timetable_index() first.` until the
index exists. Call `load_timetable_index` once (slow: downloads every dataset),
then `load_timetable_delta` for cheap subsequent refreshes.

`get_live_buses_on_route` does not need the index — it reads the live datafeed.

## Return shape — the trap

**Every hand-written tool returns either a JSON string or a prose string.**
A miss is not an exception and not an empty list; it is prose such as
`No stops found matching "x".`, `No route found.`, or `No live buses found.`

Never `json.loads` a result without checking. Guard on the first character:

```python
result = search_stops("High Street")
data = json.loads(result) if result.lstrip()[:1] in "[{" else result
```

## The 8 hand-written tools

| Tool | Parameters | Returns |
|---|---|---|
| `load_timetable_index` | `force_refresh=False` | Status text |
| `load_timetable_delta` | `since=""`, `reconcile=True` | Status text |
| `search_stops` | `query` | JSON list of stops + NaPTAN codes |
| `find_routes_between_stops` | `stop_a`, `stop_b` | JSON list, else "No single route serves both…" |
| `get_route_stops` | `operator`, `route`, `direction=None` | JSON ordered stops, else "No route found." |
| `find_buses_by_arrival_time` | `stop_a`, `stop_b`, `arrive_by`, `day=None` | JSON list |
| `plan_journey` | `stop_a`, `stop_b`, `arrive_by`, `day=None`, `max_changes=1` | JSON, **at most the 15 earliest plans** |
| `get_live_buses_on_route` | `operator_ref`, `line_ref` | JSON vehicles |

Stops are accepted as a **NaPTAN code or a name substring**. Times are `HH:MM`
24h; an unparseable time returns an error string, it does not raise. Days are
`mon`…`sun`, defaulting to today.

Details worth knowing:

- **`plan_journey` caps output at the 15 earliest plans.** `max_changes=0` is
  direct-only, `1` allows a single change.
- **`get_live_buses_on_route` takes no filters here** — operator and line only,
  with the narrowing done upstream by the datafeed. A vehicle missing an element
  renders that field as `N/A` rather than failing the response, and results are
  held in a **20-second cache**, so a repeat call within 20s is served locally.

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

- **The two fares tools differ only by `_by_datasetID`** — the easy mix-up, and
  the same trap exists for the two timetable ones. The bare name lists the
  catalogue; the `_by_datasetID` name returns one dataset's metadata, including
  its download URL.
- The `api_key` is appended by the server. Never pass it yourself.

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

The 8 hand-written tools are unaffected — `search_stops` advertises `query` with
`required=['query']`, exactly as documented.

What follows for callers:

- **Prefer the hand-written tools** for anything timetable- or vehicle-shaped.
- **`docs/RUNBOOK.md`'s smoke test does not work as written.**
  `call tool timetables_api_v1_dataset {limit: 2}` fails validation in mcp 1.30.0.

Attribution, so this is not misread as a fresh regression: the `**kwargs` shape
is long-standing, and `uv.lock` pins mcp 1.30.0 — so the breakage is plausibly a
consequence of the dependency version rather than of the server code. Not proven
here.

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

## Common mistakes

| Mistake | Reality |
|---|---|
| Counting tools by grepping `@mcp.tool()` | Yields 8. The other 9 are registered by a loop. |
| Assuming a tool name from the upstream path | Names are `{tag}_{path}`; specs carry no `operationId`. |
| Passing documented parameters to a generated tool | Its schema accepts only `kwargs`; validation fails. Use a hand-written tool. |
| `json.loads` on every result | Misses and failures return prose, not JSON. |
| Calling a timetable tool before loading | Returns a prose "call `load_timetable_index()` first" string. |
| Passing `api_key` to a passthrough tool | Appended automatically; the caller never supplies it. |
| Using a `_by_datasetID` tool to list a catalogue | That is the by-id metadata call; the bare name lists the catalogue. |

## Adding an endpoint

Hand-written: decorate with `@mcp.tool()` in `server.py`. If the result derives
from a remote call, consider whether the credential could reach the caller
through it — a remote response can echo the request URL, and `api_key` is
appended to that URL by the server.

Spec-driven: add a GET operation to a `.yml` in `openapi-schema/`; it is
registered at startup with no code change. Give it an `operationId` to control
the tool name. Note it will inherit the `**kwargs` schema problem above —
building the generated function with a real signature is what makes its
parameters usable.
