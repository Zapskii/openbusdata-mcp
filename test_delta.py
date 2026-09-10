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

# --- Test 9: LIKE wildcards in user queries match literally
writer.add_stop("0PCT", "100% Bus Stop")
writer.add_stop("0UND", "Under_Score Stop")
writer.add_stop("0BSL", "Back\\slash Stop")
writer.commit()
assert {s["naptan"] for s in store2.search_stops("%")} == {"0PCT"}, \
    "bare % acted as a wildcard"
assert {s["naptan"] for s in store2.search_stops("_")} == {"0UND"}, \
    "bare _ acted as a wildcard"
assert {s["naptan"] for s in store2.search_stops("% Bus")} == {"0PCT"}
assert {s["naptan"] for s in store2.search_stops("\\")} == {"0BSL"}
assert "0PCT" in {s["naptan"] for s in store2.search_stops("bus stop")}
assert store2.resolve_stop("Under_Score") == {"0UND"}, "resolve_stop wildcards"
print("9. LIKE wildcard escaping: OK")

# --- Test 10: short alphanumeric NaPTANs resolve literally
writer.add_stop("010A", "Alpha Stop")
writer.add_stop("010B", "Beta Stop")
writer.commit()
assert store2.resolve_stop("010A") == {"010A"}, "short NaPTAN fell through to fuzzy match"
assert store2.resolve_stop("Oaks Cross") != {"Oaks Cross"}, "name query treated as literal"
print("10. short NaPTAN literal resolution: OK")

# --- Test 11: empty stop sets return empty, not SQL errors
assert store2.find_routes_between(set(), {"010A"}) == []
assert store2._journeys_touching(set()) == []
print("11. empty-set guards: OK")

# --- Test 18: negative minutes/seconds are rejected
p = store_mod._parse_time
assert p("10:-5") is None, "negative minutes accepted"
assert p("10:30:-5") is None, "negative seconds accepted"
assert p("10:30:00") == "10:30:00"
print("18. negative time components rejected: OK")

# --- Test 19: find_routes_between stays under the SQLite variable limit
a = store2.resolve_stop("Common Road")
b = store2.resolve_stop("Common Road")
assert len(a) + len(b) < 999, f"combined params {len(a) + len(b)} >= 999"
store2.find_routes_between(a, b)  # must not raise "too many SQL variables"
print("19. combined IN-clause params capped: OK")

# --- Test 12: journey_stops table is populated and purged with its journeys
# NOTE: uses unique naptans (12A/12B) — '010A' is shared by surviving ds99
# journeys from earlier tests, so a purge assertion on it could never reach 0.
writer.add_journey("Op", "12", "outbound", "J12", {"mon"},
                   [{"naptan": "12A", "arrival": None, "departure": "09:00:00"},
                    {"naptan": "12B", "arrival": "09:10:00", "departure": None}], 42)
writer.commit()
n = writer.conn.execute(
    "SELECT COUNT(*) FROM journey_stops WHERE naptan='12A'").fetchone()[0]
assert n >= 1, "journey_stops not populated"
writer.discard_dataset(42)
writer.commit()
n = writer.conn.execute(
    "SELECT COUNT(*) FROM journey_stops WHERE naptan='12A'").fetchone()[0]
assert n == 0, "journey_stops not purged with dataset"
print("12. journey_stops populated + purged: OK")

# --- Test 13: _journeys_touching uses the index (same results, no json_each)
writer.add_journey("Op", "13", "outbound", "J13", {"mon"},
                   [{"naptan": "010A", "arrival": None, "departure": "09:00:00"},
                    {"naptan": "010B", "arrival": "09:10:00", "departure": None}], 99)
writer.commit()
plan = writer.conn.execute(
    "EXPLAIN QUERY PLAN SELECT DISTINCT journey_id FROM journey_stops WHERE naptan=?"
    , ("010B",)).fetchall()
assert any("INDEX" in str(row) for row in plan), f"no index used: {plan}"
ids = store2._journeys_touching({"010B"})
assert ids, "indexed lookup returned nothing"
assert store2._journeys_touching({"010B"}) == store2._journeys_touching({"010B"}), "nondeterministic"
print("13. _journeys_touching via index: OK")

# --- Test 14: _fetch_journeys batches (results match per-id fetch)
writer.add_journey("Op", "14", "outbound", "J14", {"mon"},
                   [{"naptan": "010A", "arrival": None, "departure": "09:00:00"},
                    {"naptan": "010B", "arrival": "09:10:00", "departure": None}], 99)
writer.commit()
ids = store2._journeys_touching({"010A"})
batch = store2._fetch_journeys(ids)
assert set(batch) == set(ids), "batch fetch missing ids"
for jid in ids:
    assert batch[jid]["journey_code"] == store2._fetch_journey(jid)["journey_code"]
print("14. _fetch_journeys batch: OK")

# --- Test 15: _candidate_journeys yields the same journeys both tools use
# NOTE: uses unique naptans (15A/15B) — '010A'/'010B' are shared by surviving
# ds99 journeys from earlier tests, so a len==1 assertion could never hold.
writer.add_journey("Op", "15", "outbound", "J15", {"mon"},
                   [{"naptan": "15A", "arrival": None, "departure": "09:00:00"},
                    {"naptan": "15B", "arrival": "09:10:00", "departure": None}], 99)
writer.commit()
cands = list(store2._candidate_journeys({"15A"}, {"15B"}, "mon", "09:30:00"))
assert len(cands) == 1 and cands[0][0]["journey_code"] == "J15"
assert list(store2._candidate_journeys({"15A"}, {"15B"}, "tue", "09:30:00")) == []
print("15. _candidate_journeys shared helper: OK")

# --- Test 20: discard_dataset surviving-key scan uses the j_oproute index
plan = writer.conn.execute(
    "EXPLAIN QUERY PLAN SELECT DISTINCT op, route FROM journeys").fetchall()
assert any("j_oproute" in str(row) for row in plan), f"index not used: {plan}"
print("20. discard_dataset index-only scan: OK")

# --- Test 21: ensure_schema backfills journey_stops for pre-existing journeys
# A pre-Phase-2 DB has journeys but no journey_stops rows and no backfill flag;
# ensure_schema must populate the index table once (in-place upgrade, no rebuild).
_bk = Path(tempfile.mkdtemp()) / "upgrade.db"
w2 = TimetableWriter(_bk)
w2.ensure_schema()
w2.add_journey("Op", "21", "outbound", "J21", {"mon"},
               [{"naptan": "010A", "arrival": None, "departure": "09:00:00"},
                {"naptan": "010B", "arrival": "09:10:00", "departure": None}], 5)
w2.conn.execute("DELETE FROM journey_stops")  # simulate a pre-Phase-2 DB
w2.conn.execute("DELETE FROM meta WHERE k='journey_stops_backfilled'")
w2.conn.commit()
w2.ensure_schema()  # must backfill
n = w2.conn.execute(
    "SELECT COUNT(*) FROM journey_stops WHERE journey_id=1").fetchone()[0]
assert n == 2, f"backfill missing: {n}"
print("21. ensure_schema backfills journey_stops: OK")

print("ALL UNIT TESTS PASS")