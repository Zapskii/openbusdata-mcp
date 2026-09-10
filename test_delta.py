import sys, tempfile
from pathlib import Path
sys.path.insert(0, "/home/hermes/openbusdata-fork/src")

import openbusdata_mcp.store as store_mod  # noqa: E402

# Redirect the store DB to a temp file before touching persistence
tmp = tempfile.mkdtemp()
store_mod.DB_PATH = Path(tmp) / "index.db"

from openbusdata_mcp.store import TimetableStore, TimetableWriter  # noqa: E402

writer = TimetableWriter()
writer.ensure_schema()
store = TimetableStore()


def stops():
    return [{"naptan": "010A", "arrival": None, "departure": "05:15:00"},
            {"naptan": "010B", "arrival": "05:20:00", "departure": None}]


# --- Test 1: dataset_id + provenance + watermark round-trip through SQLite
writer.add_journey("Op", "1", "outbound", "J1", {"mon", "tue"}, stops(), 42)
writer.add_journey("Op", "2", "inbound", "J2", {"mon"}, stops(), 99)
writer.mark_dataset_loaded(42, "2026-09-01T06:00:00Z", "Op")
writer.mark_dataset_loaded(99, "2026-09-02T06:00:00Z", "Op")
writer.set_last_refresh("2026-09-02T07:00:00+00:00")
writer.commit()

store2 = TimetableStore()
assert store2.loaded_dataset_count() == 2, "provenance lost in SQLite"
assert writer.last_refresh() == "2026-09-02T07:00:00+00:00"
row = writer.conn.execute("SELECT modified, operator FROM loaded_datasets WHERE ds_id=42").fetchone()
assert row and row[1] == "Op", "dataset_meta lost"
print("1. dataset_id + meta + watermark round-trip (SQLite): OK")

# --- Test 2: discard_dataset purges surgically, no collateral damage
writer.discard_dataset(42)
writer.commit()
store3 = TimetableStore()
ids = writer.loaded_ids()
assert 42 not in ids, "ds42 survived purge"
assert 99 in ids, "collateral damage: ds99 purged too"
assert store3.loaded_dataset_count() == 1
print("2. discard_dataset surgical purge: OK")

# --- Test 3: purge persists across a fresh connection (restart semantics)
store4 = TimetableStore()
writer2 = TimetableWriter()
persisted = writer2.loaded_ids()
assert 42 not in persisted and 99 in persisted
print("3. purge persists across restart: OK")

# --- Test 4: timestamp parser (store's _parse_time handles HH:MM[:SS] clock times)
p = store_mod._parse_time
assert p("14:30") is not None
assert p("14:30:00") is not None
assert p("") is None and p("garbage") is None
print("4. timestamp parser: OK")

# --- Test 5: stop_to_routes rebuild keeps surviving mappings
writer.add_stop("010A", "Test Stop")
writer.upsert_route("Op", "9", {"outbound"}, ["010A"], 99)
writer.add_journey("Op", "9", "outbound", "J3", {"mon"}, stops(), 99)
writer.commit()
rows = store3.get_route_stops("Op", "9")
assert rows, "route stops lost"
assert store3.search_stops("Test Stop"), "stop search lost"
print("5. stop_to_routes rebuild: OK")

print("ALL UNIT TESTS PASS")