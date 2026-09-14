"""Cross-region route-row collision regression (BUG_REPORT round 5).

Route rows are per-dataset (key = op|num|ds_id): two regions publishing the
same operatorName + route number must never evict each other's route row or
stop_to_routes refs, whatever the load order. NOC lists cannot discriminate
the regions (BODS publishes them at operator level — byte-identical lists),
so nothing here may depend on NOC.
"""
import json
import sqlite3

from openbusdata_mcp.store import TimetableWriter


def test_regions_keep_distinct_route_rows_either_load_order(writer, store):
    # Region A (ds 15766, Stevenage 2100* stops) and region B (ds 22533,
    # Telford 3590* stops) publish as the same operatorName.
    writer.upsert_route("Arriva UK Bus", "101", {"outbound"},
                        ["210021203880", "210021200011"], 15766)
    writer.upsert_route("Arriva UK Bus", "101", {"outbound"},
                        ["3590E063600", "3590E002500"], 22533)
    writer.commit()
    keys = {r[0] for r in store.conn.execute("SELECT key FROM routes")}
    assert keys == {"Arriva UK Bus|101|15766", "Arriva UK Bus|101|22533"}, keys
    # Reverse load order must land in the same rows (no eviction, no merge).
    writer.upsert_route("Arriva UK Bus", "101", {"outbound"},
                        ["3590E063600", "3590E002500"], 22533)
    writer.upsert_route("Arriva UK Bus", "101", {"outbound"},
                        ["210021203880", "210021200011"], 15766)
    writer.commit()
    rows = dict(store.conn.execute("SELECT key, stops FROM routes").fetchall())
    assert json.loads(rows["Arriva UK Bus|101|15766"]) == [
        "210021203880", "210021200011"], "Shires row corrupted by Telford pack"
    assert json.loads(rows["Arriva UK Bus|101|22533"]) == [
        "3590E063600", "3590E002500"], "Telford row corrupted by Shires pack"


def test_stop_to_routes_keeps_both_regions_discoverable(writer, store):
    writer.upsert_route("Arriva UK Bus", "101", {"outbound"},
                        ["210021203880", "210021200011"], 15766)
    writer.upsert_route("Arriva UK Bus", "101", {"outbound"},
                        ["3590E063600", "3590E002500"], 22533)
    writer.commit()
    shires = {r[0] for r in store.conn.execute(
        "SELECT naptan FROM stop_to_routes WHERE key='Arriva UK Bus|101|15766'")}
    assert shires == {"210021203880", "210021200011"}
    telford = {r[0] for r in store.conn.execute(
        "SELECT naptan FROM stop_to_routes WHERE key='Arriva UK Bus|101|22533'")}
    assert telford == {"3590E063600", "3590E002500"}
    # find_routes_between must serve both regions from the same index.
    assert store.find_routes_between({"210021203880"}, {"210021200011"})
    assert store.find_routes_between({"3590E063600"}, {"3590E002500"})


def test_get_route_stops_reports_each_region(writer, store):
    writer.upsert_route("Arriva UK Bus", "101", {"outbound"},
                        ["210021203880"], 15766)
    writer.upsert_route("Arriva UK Bus", "101", {"outbound"},
                        ["3590E063600"], 22533)
    writer.commit()
    matches = store.get_route_stops("Arriva UK Bus", "101")
    by_id = {m["ds_id"]: m for m in matches}
    assert set(by_id) == {15766, 22533}, matches
    assert by_id[15766]["stops"][0]["naptan"] == "210021203880"
    assert by_id[22533]["stops"][0]["naptan"] == "3590E063600"
    # Re-upserting one pack (registration variants) must not duplicate a match.
    writer.upsert_route("Arriva UK Bus", "101", {"outbound"},
                        ["210021203880"], 15766)
    writer.commit()
    assert len(store.get_route_stops("Arriva UK Bus", "101")) == 2


def test_route_stop_coords_prefers_region_containing_stop(writer, store):
    writer.upsert_route("Arriva UK Bus", "101", {"outbound"},
                        ["210021203880", "210021200011"], 15766)
    writer.upsert_route("Arriva UK Bus", "101", {"outbound"},
                        ["3590E063600", "3590E002500"], 22533)
    writer.commit()
    shires = store.route_stop_coords(
        "Arriva UK Bus", "101", prefer_stops={"210021200011"})
    assert [c["naptan"] for c in shires] == ["210021203880", "210021200011"]
    telford = store.route_stop_coords(
        "Arriva UK Bus", "101", prefer_stops={"3590E002500"})
    assert [c["naptan"] for c in telford] == ["3590E063600", "3590E002500"]
    # No preference: deterministic (first sibling); unknown route still None.
    assert store.route_stop_coords("Arriva UK Bus", "101") is not None
    assert store.route_stop_coords("Arriva UK Bus", "999") is None


def test_resolve_operator_across_region_siblings(writer, store):
    writer.upsert_route("Arriva UK Bus", "101", {"outbound"},
                        ["210021203880"], 15766)
    writer.upsert_route("Arriva UK Bus", "101", {"outbound"},
                        ["3590E063600"], 22533)
    writer.mark_dataset_loaded(15766, None, "Arriva UK Bus", "ARHE,ARHB")
    writer.mark_dataset_loaded(22533, None, "Arriva UK Bus", "ARHT")
    writer.commit()
    # Name input: the index name is shared by the regions; the NOC list is
    # dataset-level, so either sibling resolves to the same (name, NOCs).
    assert store.resolve_operator("Arriva UK Bus", "101")[0] == "Arriva UK Bus"
    # NOC input: each region's NOC finds its own pack.
    assert store.resolve_operator("ARHT", "101") == ("Arriva UK Bus", ["ARHT"])
    assert store.resolve_operator("ARHE", "101") == ("Arriva UK Bus", ["ARHE", "ARHB"])


def test_discard_dataset_purges_only_its_region(writer, store):
    writer.upsert_route("Arriva UK Bus", "101", {"outbound"},
                        ["210021203880"], 15766)
    writer.upsert_route("Arriva UK Bus", "101", {"outbound"},
                        ["3590E063600"], 22533)
    writer.mark_dataset_loaded(15766, None, "Arriva UK Bus", "ARHE")
    writer.mark_dataset_loaded(22533, None, "Arriva UK Bus", "ARHT")
    writer.commit()
    writer.discard_dataset(22533)
    writer.commit()
    assert store.conn.execute(
        "SELECT 1 FROM routes WHERE key='Arriva UK Bus|101|15766'").fetchone(), \
        "sibling region's route row lost"
    assert not store.conn.execute(
        "SELECT 1 FROM routes WHERE key='Arriva UK Bus|101|22533'").fetchone()
    assert store.conn.execute(
        "SELECT 1 FROM stop_to_routes WHERE key='Arriva UK Bus|101|15766'"
    ).fetchone(), "sibling region's discoverability lost"
    assert not store.conn.execute(
        "SELECT 1 FROM stop_to_routes WHERE key='Arriva UK Bus|101|22533'"
    ).fetchone()


def test_migration_rekeys_legacy_keys_and_dedupes_s2r(tmp_path):
    # Simulate the pre-migration live index: global keys, duplicate s2r rows,
    # a ds_id=0 orphan pair, and a stale ref to a nonexistent route.
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE routes (key TEXT PRIMARY KEY, op TEXT, num TEXT,
                             directions TEXT, stops TEXT, ds_id INT DEFAULT 0);
        CREATE TABLE stop_to_routes (naptan TEXT, key TEXT);
        CREATE TABLE journeys (id INTEGER PRIMARY KEY, ds_id INT, op TEXT,
                               route TEXT, direction TEXT, code TEXT,
                               days TEXT, json TEXT, days_mask INT DEFAULT 0);
        CREATE TABLE stops (naptan TEXT PRIMARY KEY, name TEXT, lat REAL, lon REAL);
        CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE loaded_datasets (ds_id INTEGER PRIMARY KEY, modified TEXT,
                                      operator TEXT, noc TEXT DEFAULT '');
        CREATE TABLE journey_stops (naptan TEXT, journey_id INT);
        CREATE TABLE journey_stop_times (journey_id INT, seq INT, naptan TEXT,
                                         dep TEXT, arr TEXT);
        INSERT INTO routes VALUES ('Arriva UK Bus|101','Arriva UK Bus','101',
                                   '["outbound"]','["210021203880"]',22533);
        INSERT INTO routes VALUES ('Arriva UK Bus|301','Arriva UK Bus','301',
                                   '["outbound"]','["210021203880"]',15766);
        INSERT INTO routes VALUES ('Orphan Op|9','Orphan Op','9','[]','[]',0);
        INSERT INTO stop_to_routes VALUES ('210021203880','Arriva UK Bus|101');
        INSERT INTO stop_to_routes VALUES ('210021203880','Arriva UK Bus|101');
        INSERT INTO stop_to_routes VALUES ('3590E063600','Arriva UK Bus|101');
        INSERT INTO stop_to_routes VALUES ('999X','Orphan Op|9');
        INSERT INTO stop_to_routes VALUES ('999Y','Ghost|1');
    """)
    conn.commit()
    conn.close()
    w = TimetableWriter(db)
    w.ensure_schema()
    keys = {r[0] for r in w.conn.execute("SELECT key FROM routes")}
    assert keys == {"Arriva UK Bus|101|22533", "Arriva UK Bus|301|15766"}, keys
    refs = {(a, b) for a, b in w.conn.execute(
        "SELECT naptan, key FROM stop_to_routes")}
    # Rekeyed to the surviving ds's key, duplicates collapsed, ds_id=0 pair
    # and the Ghost ref swept.
    assert refs == {("210021203880", "Arriva UK Bus|101|22533"),
                    ("3590E063600", "Arriva UK Bus|101|22533")}, refs
    # Idempotent: a second run must not re-rekey (all keys now carry 2 pipes).
    w.ensure_schema()
    assert w.conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0] == 2
    assert w.conn.execute(
        "SELECT v FROM meta WHERE k='routes_rekeyed'").fetchone()


def test_migration_rebuilds_legacy_s2r_table_with_pk(tmp_path):
    # The live index's s2r table predates the PK: CREATE TABLE IF NOT EXISTS
    # cannot retrofit one, so re-upserts duplicate rows again after a plain
    # dedupe. The migration must rebuild the table with the PK.
    db = tmp_path / "legacy_s2r.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE routes (key TEXT PRIMARY KEY, op TEXT, num TEXT,
                             directions TEXT, stops TEXT, ds_id INT DEFAULT 0);
        CREATE TABLE stop_to_routes (naptan TEXT, key TEXT);
        CREATE TABLE journeys (id INTEGER PRIMARY KEY, ds_id INT, op TEXT,
                               route TEXT, direction TEXT, code TEXT,
                               days TEXT, json TEXT, days_mask INT DEFAULT 0);
        CREATE TABLE stops (naptan TEXT PRIMARY KEY, name TEXT, lat REAL, lon REAL);
        CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE loaded_datasets (ds_id INTEGER PRIMARY KEY, modified TEXT,
                                      operator TEXT, noc TEXT DEFAULT '');
        CREATE TABLE journey_stops (naptan TEXT, journey_id INT);
        CREATE TABLE journey_stop_times (journey_id INT, seq INT, naptan TEXT,
                                         dep TEXT, arr TEXT);
        INSERT INTO routes VALUES ('Op|1','Op','1','[]','["010A"]',1);
        INSERT INTO stop_to_routes VALUES ('010A','Op|1');
        INSERT INTO stop_to_routes VALUES ('010A','Op|1');
        INSERT INTO stop_to_routes VALUES ('010A','Op|1');
    """)
    conn.commit()
    conn.close()
    w = TimetableWriter(db)
    w.ensure_schema()
    shape = {(r[1], r[5]) for r in w.conn.execute(
        "PRAGMA table_info(stop_to_routes)")}
    assert shape == {("naptan", 1), ("key", 2)}, \
        f"s2r still has no real PK: {shape}"
    assert w.conn.execute("SELECT COUNT(*) FROM stop_to_routes").fetchone()[0] == 1
    # With the PK in place, re-upserts must NOT duplicate rows again.
    w.upsert_route("Op", "1", {"outbound"}, ["010A", "010B"], 1)
    w.upsert_route("Op", "1", {"outbound"}, ["010A", "010B"], 1)
    w.commit()
    assert w.conn.execute(
        "SELECT COUNT(*) FROM stop_to_routes WHERE key='Op|1|1'"
    ).fetchone()[0] == 2, "PK-less table re-accumulated duplicates"
    # A fresh DB (table born with the PK) never triggers the rebuild.
    db2 = tmp_path / "fresh.db"
    w2 = TimetableWriter(db2)
    w2.ensure_schema()
    assert w2.conn.execute(
        "SELECT COUNT(*) FROM stop_to_routes").fetchone()[0] == 0
    w2.upsert_route("Op", "2", {"outbound"}, ["010A"], 3)
    w2.upsert_route("Op", "2", {"outbound"}, ["010A"], 3)
    assert w2.conn.execute(
        "SELECT COUNT(*) FROM stop_to_routes").fetchone()[0] == 1