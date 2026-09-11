# OpenBusData MCP — DevOps Runbook

UK bus timetable data (DfT BODS) served to the **travel** agent through a Dockerized
fork of `AndrewAubury/openbusdata-mcp`. The index is SQLite-backed; the gateway
spawns the container per-session over stdio.

- Fork: `github.com/davidmcdavidson/openbusdata-mcp` (origin), upstream kept as `upstream`
- Local checkout: `/home/hermes/openbusdata-fork` — branch `main`, tracks origin
- Image: `openbusdata-mcp:latest` (= `:delta`, 326 MB, python:3.11-slim, entrypoint `openbusdata-mcp`)
- Fork commits `35a3296..1f3bea5`: delta loader, SQLite store, two download-format fixes, spec servers-block fix

## 1. How the container is configured and launched

Hermes launches it automatically — there is no long-running container to babysit.
`~/.hermes/profiles/travel/config.yaml`:

```yaml
mcp_servers:
  openbusdata:
    command: docker
    args:
      - run
      - -i
      - --rm
      - --memory
      - 1g
      - -e
      - OPENBUS_API_KEY
      - -v
      - openbusdata-cache:/root/.cache/openbusdata
      - openbusdata-mcp
    env:
      OPENBUS_API_KEY: ${OPENBUS_API_KEY}
    timeout: 1800
    connect_timeout: 120
    enabled: true
```

What happens per agent session:

1. Gateway spawns `docker run -i --rm ... openbusdata-mcp` (stdio MCP transport).
2. `-e OPENBUS_API_KEY` (no `=`) + `env:` pass the key from the gateway process
   environment, where Hermes expands `${OPENBUS_API_KEY}` from
   `~/.hermes/profiles/travel/.env`. The key lives only in `.env` — never in the
   repo, image, or container layers.
3. `-v openbusdata-cache:/root/.cache/openbusdata` mounts the persistent index so
   every fresh container sees the data instantly (no re-download at startup).
4. Startup banner prints the index counts, e.g.
   `[OpenBusData MCP] SQLite index ready: 253493 stops, 7442 routes, 889864 journeys from 941 datasets.`
   If the volume were empty, it prints a "No timetable index yet" banner instead.

Container lifecycle: exits when the session ends (`--rm`), respawns on the next
tool call. RAM stays ~45 MB idle (SQLite reads are O(query), not O(catalogue) —
the old in-memory design OOM-killed at 9.4 GB).

After changing the image, restart the gateway to pick it up:
`hermes -p travel gateway restart`.

## 2. Initial download (bootstrap)

Full load of all ~941 datasets. Already done once (Sep 9); only needed again if
the volume is wiped or a fresh volume is used. Memory-flat: one XML in RAM at a
time, one transaction per dataset, so a killed run resumes where it stopped.

```bash
set -a; source ~/.hermes/profiles/travel/.env; set +a
docker run --rm --name obd-bootstrap \
  -v openbusdata-cache:/root/.cache/openbusdata \
  -e OPENBUS_API_KEY \
  --entrypoint python3 openbusdata-mcp:latest -c "
import asyncio, sys
sys.path.insert(0, '/build/src')
from openbusdata_mcp.server import load_all_timetable_data
print(asyncio.run(load_all_timetable_data()))"
```

Facts from the real run: ~941/941 in a few hours at ~1–2 datasets/min (big
operators are slow), RAM flat ~450–600 MB, resumable — every dataset commits
transactionally. Progress is visible from a sidecar:

```bash
docker run --rm --entrypoint python3 -v openbusdata-cache:/data openbusdata-mcp:latest -c '
import sqlite3
db = sqlite3.connect("file:/data/index.db?mode=ro", uri=True)
print("datasets:", db.execute("SELECT COUNT(*) FROM loaded_datasets").fetchone()[0],
      "| journeys:", db.execute("SELECT COUNT(*) FROM journeys").fetchone()[0])'
```

## 3. Scheduled diff refresh (delta)

The mechanism exists and is the intended steady-state; **the cron job is not yet
wired** (offered, not confirmed). The tool is `load_timetable_delta(since="",
reconcile=true)`:

- Sweeps the BODS catalogue, re-downloads only datasets with `modified` newer
  than the watermark (`meta.last_refresh` in index.db) and purges withdrawn
  datasets. Weekly churn is ~15–20% → minutes, not hours.
- **Watermark policy:** advances when errors ≤ max(2, 1% of refreshed) — a
  permanently-broken dataset no longer freezes the watermark. Every failure is
  logged to stderr (`[loader] dataset N failed: …`) and the run summary lists
  failed dataset IDs; over-budget runs leave the watermark untouched.
- Falls back to a full load if no watermark/cache exists.
- Manual override: `since=YYYY-MM-DDTHH:MM:SS`; `force_refresh: true` on
  `load_all_timetable_data` re-downloads everything.

How it actually triggers today: the travel agent calls the MCP tool
`load_timetable_delta` in conversation. To make it hands-off, wire a cron job
that talks to the travel agent on Sunday (e.g. "run the weekly bus data delta")
— `hermes -p travel cron create ...` — not yet done; ask in #travel to set it up.

## 4. Data layout on the volume (`openbusdata-cache`)

| File | State |
|---|---|
| `index.db` | **Live** SQLite index, 3.48 GB — 941 datasets, 889,864 journeys, 253,493 stops, 7,442 routes (Sep 9 full load) |
| `index.db-shm/-wal` | SQLite WAL sidecars (empty when quiescent) |

(The legacy `timetable_cache.json` was deleted on Sep 10 — 2.5 GB reclaimed. The
`save_cache`/`load_cache` JSON methods were removed from the server in the same
cleanup; pre-SQLite scripts live in `dev-legacy/`.)

SQLite schema (created by `store.py`, migrated from the legacy JSON cache by
`convert_to_sqlite.py`): `stops`, `routes`, `journeys` (+ `journey_stops`),
`stop_to_routes`, `loaded_datasets` (provenance), `meta` (watermark + misc).
Indexes on `journeys(ds_id)`, `stop_to_routes(naptan)`, `journeys(op, route)`.

## 5. Image build

```bash
cd /home/hermes/openbusdata-fork
docker build -f Dockerfile.image -t openbusdata-mcp:latest .   # 10 lines: COPY . /build; pip install /build "mcp<2"
```

The `/build` dir is just the in-image copy of the repo used as the pip source —
not a runtime path users touch. Rebuild + `hermes -p travel gateway restart`
whenever the source changes.

## 6. Verification / smoke tests

```bash
# Tools + index through a fresh container (what the gateway itself runs):
docker run --rm -i -e OPENBUS_API_KEY -v openbusdata-cache:/root/.cache/openbusdata \
  openbusdata-mcp  <<'EOF'   # speak MCP over stdin, or simply talk to the agent in #travel
EOF

# Passthrough API tools (fares/disruptions/AVL/cancellations) — must return JSON,
# not HTML (HTML = the /api/v1 double-prefix regression, fixed in 1f3bea5):
#   call tool timetables_api_v1_dataset {limit: 2} → expect {"count": 941, ...}

# Unit + E2E (from the repo):
cd /home/hermes/openbusdata-fork
~/.local/bin/uv run --with 'mcp<2' --with httpx --with pyyaml --with pytest pytest -q   # 37/37
~/.local/bin/uv run --with 'mcp<2' --with httpx --with pyyaml python e2e_sqlite.py  # vs real index
```

- `ensure_schema` now creates the `stops_fts` FTS5 index, backfills it once on upgrade, and `search_stops`/`resolve_stop` query it first with a LIKE fallback. **Semantics:** FTS5 is token+prefix (AND of `"token"*` terms); a query can return a strict subset of what the old substring LIKE would have matched when FTS5 finds *something* (the fallback only runs when FTS5 returns nothing). Whitespace-only queries return nothing.

Key-validity probe (200 vs 401, value never printed):
`curl -s -o /dev/null -w '%{http_code}' "https://data.bus-data.dft.gov.uk/api/v1/dataset/?limit=1&api_key=$OPENBUS_API_KEY"`.

## 7. Key rotation

1. Mint the new key at data.bus-data.dft.gov.uk (Account → API Keys).
2. Edit `~/.hermes/profiles/travel/.env`: replace the `OPENBUS_API_KEY=…` value.
3. `hermes -p travel gateway restart`.
4. Probe 200 (above) and one tool call in #travel; then confirm the old key 401s.

## 8. Ops notes & history

- **Why SQLite:** the JSON cache needed the whole index in RAM (~9.4 GB peak;
  3× exit-137 OOMKills in a 7.7 GB cgroup). SQLite reads keep the container at
  ~45 MB and make startup instant. Tool surface unchanged (17 tools).
- **Download quirks** (both fixed in the fork): not all BODS downloads are zips
  (many are bare TransXChange XML); parsed journey times are `datetime.time`
  and must be isoformat'd before JSON insertion.
- **SIRI-VM live data** is never cached — it flows through passthrough tools
  per-call.
- Refresh cadence guidance: weekly delta, monthly full reconcile (withdrawals
  beyond the delta's reconcile are covered by `load_all_timetable_data`).
- Git identity for the fork is the profile-scoped coder setup (`bin/gh`,
  `bin/gh-credential-coder`); never `gh auth login` on this box.
- Housekeeping: `timetable_cache.json` (2.5 GB) can be removed from the volume.