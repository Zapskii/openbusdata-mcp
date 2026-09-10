import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

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

# --- Test 6: post-midnight (24:00+) times survive unclamped, ordering intact
p = store_mod._parse_time
assert p("24:05") == "24:05:00", "post-midnight time clamped"
assert p("23:59") == "23:59:00"
assert p("25:70") is None and p("23:60") is None, "bad minutes/seconds accepted"
assert p("garbage") is None and p("") is None
writer.add_stop("010A", "Night A")
writer.add_stop("010B", "Night B")
writer.add_journey("Op", "N1", "outbound", "JN", {"sat"},
                   [{"naptan": "010A", "arrival": None, "departure": "24:05:00"},
                    {"naptan": "010B", "arrival": "24:30:00", "departure": None}], 99)
writer.commit()
buses = store2.find_buses_by_arrival_time({"010A"}, {"010B"}, "24:45", "sat")
assert buses and buses[0]["arrive"] == "24:30:00", "post-midnight bus lost"
assert not store2.find_buses_by_arrival_time({"010A"}, {"010B"}, "23:45", "sat"), \
    "24:30 arrival matched a 23:45 target"
print("6. post-midnight 24:00+ times: OK")

# --- Test 7: fuzzy stop resolution is capped (SQLite 999-variable guard)
for i in range(600):
    writer.add_stop(f"9{i:04d}", f"Common Road {i}")
writer.commit()
resolved = store2.resolve_stop("Common Road")
assert len(resolved) <= store_mod.MAX_RESOLVE, "resolve_stop unbounded"
assert store2.resolve_stop("90599") == {"90599"}, "literal NaPTAN path broken"
print("7. resolve_stop capped: OK")

# --- Test 8: discard keeps routes served by other datasets, self-heals dead ones
writer.add_journey("Op", "Shared", "outbound", "JS", {"mon"}, stops(), 99)
writer.add_journey("Op", "Shared", "outbound", "JS2", {"mon"}, stops(), 42)
writer.upsert_route("Op", "Shared", {"outbound"}, ["010A", "010B"], 42)
writer.upsert_route("Op", "Gone", {"outbound"}, ["010A"], 42)
writer.add_journey("Op", "Gone", "outbound", "JG", {"mon"}, stops(), 42)
writer.mark_dataset_loaded(42, "2026-09-03T06:00:00Z", "Op")
writer.commit()
writer.discard_dataset(42)
writer.commit()
assert writer.conn.execute(
    "SELECT 1 FROM routes WHERE key='Op|Shared'").fetchone(), \
    "route row deleted though ds99 still serves it"
assert writer.conn.execute(
    "SELECT COUNT(*) FROM stop_to_routes WHERE key='Op|Shared'").fetchone()[0] == 2, \
    "discoverability lost for a surviving shared route"
assert writer.conn.execute(
    "SELECT 1 FROM routes WHERE key='Op|Gone'").fetchone(), \
    "dead route row should self-heal via re-download, not delete"
assert not writer.conn.execute(
    "SELECT 1 FROM stop_to_routes WHERE key='Op|Gone'").fetchone(), \
    "dead route still discoverable"
print("8. discard_dataset route semantics: OK")

print("ALL UNIT TESTS PASS")