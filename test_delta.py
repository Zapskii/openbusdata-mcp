import sys, json, tempfile
from pathlib import Path
sys.path.insert(0, "/home/hermes/openbusdata-fork/src")

import openbusdata_mcp.server as server  # noqa: E402

# Redirect cache to a temp dir before touching persistence
tmp = tempfile.mkdtemp()
server.CACHE_DIR = Path(tmp)

idx = server.TimetableIndex()

# --- Test 1: dataset_id + provenance + watermark round-trip through the cache
j1 = server.Journey(operator="Op", route_num="1", direction="outbound",
                    journey_code="J1", dataset_id=42)
j2 = server.Journey(operator="Op", route_num="2", direction="inbound",
                    journey_code="J2", dataset_id=99)
idx.add_journey(j1)
idx.add_journey(j2)
idx.loaded_datasets = {42, 99}
idx.dataset_meta = {42: {"modified": "2026-09-01T06:00:00Z", "operator": "Op"},
                    99: {"modified": "2026-09-02T06:00:00Z", "operator": "Op"}}
idx.last_refresh = "2026-09-02T07:00:00+00:00"
idx.save_cache()
idx2 = server.TimetableIndex()
assert idx2.load_cache()
assert idx2.journeys[0].dataset_id == 42, "dataset_id lost in cache round-trip"
assert idx2.dataset_meta[42]["operator"] == "Op"
assert idx2.last_refresh == "2026-09-02T07:00:00+00:00"
print("1. dataset_id + meta + watermark round-trip: OK")

# --- Test 2: discard_dataset purges surgically, no collateral damage
idx2.discard_dataset(42)
assert all(j.dataset_id != 42 for j in idx2.journeys), "journey from ds42 survived purge"
assert 42 not in idx2.loaded_datasets and 42 not in idx2.dataset_meta
assert 99 in idx2.loaded_datasets, "collateral damage: ds99 purged too"
assert idx2.journeys[0].dataset_id == 99
print("2. discard_dataset surgical purge: OK")

# --- Test 3: purge survives a save/load cycle
idx2.save_cache()
idx3 = server.TimetableIndex()
idx3.load_cache()
assert all(j.dataset_id != 42 for j in idx3.journeys)
assert 99 in idx3.loaded_datasets
print("3. purge persists across restart: OK")

# --- Test 4: timestamp parser
p = server._parse_bods_ts
assert p("2026-09-01T06:00:00+00:00") is not None
assert p("2026-09-01T06:00:00Z") is not None
assert p(None) is None and p("") is None and p("garbage") is None
print("4. timestamp parser: OK")

# --- Test 5: stop_to_routes rebuild keeps surviving mappings
s = server.Stop(naptan="010A", name="Test Stop")
idx3.add_stop(s)
r1 = server.Route(operator="Op", route_num="9", directions={"outbound"}, stops=["010A"])
idx3.add_route(r1)
jj = server.Journey(operator="Op", route_num="9", direction="outbound",
                    journey_code="J9", stops=["010A"], dataset_id=7)
idx3.add_journey(jj)
assert "010A" in idx3.stop_to_routes
idx3.discard_dataset(7)
assert "010A" not in idx3.stop_to_routes, "dangling stop_to_routes entry after purge"
print("5. stop_to_routes rebuild: OK")

print("ALL UNIT TESTS PASS")