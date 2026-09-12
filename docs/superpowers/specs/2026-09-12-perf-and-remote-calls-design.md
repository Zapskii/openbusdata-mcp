# Performance & minimal-remote-calls design

Date: 2026-09-12
Status: approved direction (assessment in session; spec written before plan)

## Goals

1. Fewer remote API calls to BODS per unit of useful work (delta sweeps, live tools).
2. Faster local queries (stop/route/journey tools) with memory staying O(query).
3. No change to any MCP tool's JSON output contract.

## Non-goals

- **Connection Scan Algorithm / connections table**: dropped. The `plan_journey`
  contract (multiple plans per destination, operator-level dedup pinned by
  test_robustness test 5, `max_changes` 0/1) requires enumerating candidate
  journeys, which a single-label CSA cannot express and a multi-label CSA makes
  much more complex. The indexed relational schema + SQL self-joins deliver the
  same order of speedup without touching the contract.
- No new runtime dependencies.
- No server-side state beyond `~/.cache/openbusdata/index.db`.

## Changes

### Phase A — remote-call hygiene (server.py)

A1. **Shared pooled httpx client.** Module-level `get_http_client()` used by the
    OpenAPI-spec-generated tools and `get_live_buses_on_route`. One TCP/TLS pool
    for the process instead of a new client (new handshake) per tool call.
    `set_http_client()` is a test seam. Bulk dataset *loaders* keep per-run
    clients (long streams; per-run lifetime is correct there).

A2. **TTL cache on live datafeed responses.** `TTLCache` (monotonic clock,
    bounded to 256 entries) applied to `get_live_buses_on_route` keyed on
    `(operator_ref, line_ref)`, TTL 20s. Agents re-poll live locations within
    seconds; a 20s window is far below any useful staleness threshold for bus
    tracking and halves live-call volume.

A3. **Server-side `modifiedDate` filter on delta sweeps.** The BODS catalogue
    endpoint supports `modifiedDate` (confirmed in the bundled timetables spec).
    `_sweep_catalogue(client, modified_since=...)` passes it when set. A delta
    run uses the filtered sweep (a handful of pages) unless a full sweep is due:
    `reconcile=True` (withdrawal purge needs the full catalogue) or
    `last_full_sweep` older than 7 days. Full sweeps update the watermark meta.

### Phase B — relational query schema (store.py)

B1. **`journeys.days_mask`** (7-bit int, mon=1 … sun=64) with one-time backfill
    from the `days` JSON (meta flag `days_mask_backfilled`). Day filtering moves
    into SQL during candidate pruning.

B2. **`journey_stop_times` v2**: add `seq INT` and `arr TEXT` columns; rows carry
    the full schedule (dep = COALESCE(departure, arrival), arr = arrival). One-time
    rebuild from `journeys.json` via `json_each` (meta flag
    `journey_stop_times_backfilled`, reused). New index `jst_j(journey_id, seq)`;
    `jst_n(naptan, dep)` kept. `add_journey` writes the new columns directly.

B3. **SQL candidate pruning.** `_candidate_journeys` becomes a single indexed SQL
    query over `journey_stop_times` (MIN(seq) first-occurrence semantics preserved)
    yielding a `Candidate` NamedTuple; `find_buses_by_arrival_time` and
    `plan_direct` build output from it. The query path never parses
    `journeys.json` again.

### Phase C — SQL one-change planner + cleanup

C1. **One-change planner as one SQL self-join** over `journey_stop_times`
    (leg1 mid-stop arrival → leg2 same-naptan departure via the `(naptan, dep)`
    index), preserving `plan_one_change`'s public API, cache, and output shape.
    Guarded `LIMIT 2000`; the tool-level dedup + top-15 cap is unchanged.

C2. **Dead-code removal**: `_fetch_journeys`, `_fetch_journey`,
    `_journey_row_to_out`, `_journeys_touching`, `_journeys_departing` and their
    tests, now that nothing reads JSON blobs or walks Python loops.

## Accepted behavior deltas

- `depart` in plan/bus outputs falls back to the stop's arrival when the
  timetable publishes no departure (was `null`). Matches what the one-change
  planner already did for its second leg.
- Delta runs with a fresh watermark no longer purge catalogue withdrawals until
  the next full sweep (reconcile or 7-day cadence). Withdrawn datasets
  self-heal on the next full refresh.

## Constraints carried into the plan

- SQLite pre-3.32 variable limit 999: the two-set queries bind ≤ 400 + 400 + 2.
- Times are zero-padded `'HH:MM:SS'` strings, hours may exceed 24 (TransXChange
  post-midnight); compared as strings, never clamped.
- Every legacy DB upgrade is a one-time meta-flag backfill in `ensure_schema`,
  committed with the dataset transaction pattern already in place.