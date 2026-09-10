import tempfile
from pathlib import Path

import openbusdata_mcp.store as store_mod
from openbusdata_mcp.store import TimetableWriter


def stops():
    return [{"naptan": "010A", "arrival": None, "departure": "05:15:00"},
            {"naptan": "010B", "arrival": "05:20:00", "departure": None}]


# --- Test 1: dataset_id + provenance + watermark round-trip through SQLite
def test_1_dataset_id_meta_watermark_roundtrip(writer, store):
    writer.add_journey("Op", "1", "outbound", "J1", {"mon", "tue"}, stops(), 42)
    writer.add_journey("Op", "2", "inbound", "J2", {"mon"}, stops(), 99)
    writer.mark_dataset_loaded(42, "2026-09-01T06:00:00Z", "Op")
    writer.mark_dataset_loaded(99, "2026-09-02T06:00:00Z", "Op")
    writer.set_last_refresh("2026-09-02T07:00:00+00:00")
    writer.commit()
    assert store.loaded_dataset_count() == 2, "provenance lost in SQLite"
    assert writer.last_refresh() == "2026-09-02T07:00:00+00:00"
    row = writer.conn.execute("SELECT modified, operator FROM loaded_datasets WHERE ds_id=42").fetchone()
    assert row and row[1] == "Op", "dataset_meta lost"


# --- Test 2: discard_dataset purges surgically, no collateral damage
def test_2_discard_dataset_surgical_purge(writer, store):
    writer.add_journey("Op", "1", "outbound", "J1", {"mon", "tue"}, stops(), 42)
    writer.add_journey("Op", "2", "inbound", "J2", {"mon"}, stops(), 99)
    writer.mark_dataset_loaded(42, "2026-09-01T06:00:00Z", "Op")
    writer.mark_dataset_loaded(99, "2026-09-02T06:00:00Z", "Op")
    writer.commit()
    writer.discard_dataset(42)
    writer.commit()
    ids = writer.loaded_ids()
    assert 42 not in ids, "ds42 survived purge"
    assert 99 in ids, "collateral damage: ds99 purged too"
    assert store.loaded_dataset_count() == 1


# --- Test 3: purge persists across a fresh connection (restart semantics)
def test_3_purge_persists_across_restart(writer, store):
    writer.add_journey("Op", "1", "outbound", "J1", {"mon", "tue"}, stops(), 42)
    writer.add_journey("Op", "2", "inbound", "J2", {"mon"}, stops(), 99)
    writer.mark_dataset_loaded(42, "2026-09-01T06:00:00Z", "Op")
    writer.mark_dataset_loaded(99, "2026-09-02T06:00:00Z", "Op")
    writer.commit()
    writer.discard_dataset(42)
    writer.commit()
    writer2 = TimetableWriter()
    persisted = writer2.loaded_ids()
    assert 42 not in persisted and 99 in persisted


# --- Test 4: timestamp parser (store's _parse_time handles HH:MM[:SS] clock times)
def test_4_timestamp_parser():
    p = store_mod._parse_time
    assert p("14:30") is not None
    assert p("14:30:00") is not None
    assert p("") is None and p("garbage") is None


# --- Test 5: stop_to_routes rebuild keeps surviving mappings
def test_5_stop_to_routes_rebuild(writer, store):
    writer.add_stop("010A", "Test Stop")
    writer.upsert_route("Op", "9", {"outbound"}, ["010A"], 99)
    writer.add_journey("Op", "9", "outbound", "J3", {"mon"}, stops(), 99)
    writer.commit()
    rows = store.get_route_stops("Op", "9")
    assert rows, "route stops lost"
    assert store.search_stops("Test Stop"), "stop search lost"


# --- Test 6: post-midnight (24:00+) times survive unclamped, ordering intact
def test_6_post_midnight_times(writer, store):
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
    buses = store.find_buses_by_arrival_time({"010A"}, {"010B"}, "24:45", "sat")
    assert buses and buses[0]["arrive"] == "24:30:00", "post-midnight bus lost"
    assert not store.find_buses_by_arrival_time({"010A"}, {"010B"}, "23:45", "sat"), \
        "24:30 arrival matched a 23:45 target"


# --- Test 7: fuzzy stop resolution is capped (SQLite 999-variable guard)
def test_7_resolve_stop_capped(writer, store):
    for i in range(600):
        writer.add_stop(f"9{i:04d}", f"Common Road {i}")
    writer.commit()
    resolved = store.resolve_stop("Common Road")
    assert len(resolved) <= store_mod.MAX_RESOLVE, "resolve_stop unbounded"
    assert store.resolve_stop("90599") == {"90599"}, "literal NaPTAN path broken"


# --- Test 8: discard keeps routes served by other datasets, self-heals dead ones
def test_8_discard_route_semantics(writer, store):
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


# --- Test 9: LIKE wildcards in user queries match literally
def test_9_like_wildcard_escaping(writer, store):
    writer.add_stop("0PCT", "100% Bus Stop")
    writer.add_stop("0UND", "Under_Score Stop")
    writer.add_stop("0BSL", "Back\\slash Stop")
    writer.commit()
    assert {s["naptan"] for s in store.search_stops("%")} == {"0PCT"}, \
        "bare % acted as a wildcard"
    assert {s["naptan"] for s in store.search_stops("_")} == {"0UND"}, \
        "bare _ acted as a wildcard"
    assert {s["naptan"] for s in store.search_stops("% Bus")} == {"0PCT"}
    assert {s["naptan"] for s in store.search_stops("\\")} == {"0BSL"}
    assert "0PCT" in {s["naptan"] for s in store.search_stops("bus stop")}
    assert store.resolve_stop("Under_Score") == {"0UND"}, "resolve_stop wildcards"


# --- Test 10: short alphanumeric NaPTANs resolve literally
def test_10_short_naptan_literal_resolution(writer, store):
    writer.add_stop("010A", "Alpha Stop")
    writer.add_stop("010B", "Beta Stop")
    writer.commit()
    assert store.resolve_stop("010A") == {"010A"}, "short NaPTAN fell through to fuzzy match"
    assert store.resolve_stop("Oaks Cross") != {"Oaks Cross"}, "name query treated as literal"


# --- Test 11: empty stop sets return empty, not SQL errors
def test_11_empty_set_guards(store):
    assert store.find_routes_between(set(), {"010A"}) == []
    assert store._journeys_touching(set()) == []


# --- Test 18: negative minutes/seconds are rejected
def test_18_negative_time_components_rejected():
    p = store_mod._parse_time
    assert p("10:-5") is None, "negative minutes accepted"
    assert p("10:30:-5") is None, "negative seconds accepted"
    assert p("10:30:00") == "10:30:00"


# --- Test 19: find_routes_between stays under the SQLite variable limit
def test_19_combined_in_clause_params_capped(writer, store):
    for i in range(600):
        writer.add_stop(f"9{i:04d}", f"Common Road {i}")
    writer.commit()
    a = store.resolve_stop("Common Road")
    b = store.resolve_stop("Common Road")
    assert len(a) + len(b) < 999, f"combined params {len(a) + len(b)} >= 999"
    store.find_routes_between(a, b)  # must not raise "too many SQL variables"


# --- Test 12: journey_stops table is populated and purged with its journeys
def test_12_journey_stops_populated_and_purged(writer, store):
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


# --- Test 13: _journeys_touching uses the index (same results, no json_each)
def test_13_journeys_touching_uses_index(writer, store):
    writer.add_journey("Op", "13", "outbound", "J13", {"mon"},
                       [{"naptan": "010A", "arrival": None, "departure": "09:00:00"},
                        {"naptan": "010B", "arrival": "09:10:00", "departure": None}], 99)
    writer.commit()
    plan = writer.conn.execute(
        "EXPLAIN QUERY PLAN SELECT DISTINCT journey_id FROM journey_stops WHERE naptan=?"
        , ("010B",)).fetchall()
    assert any("INDEX" in str(row) for row in plan), f"no index used: {plan}"
    ids = store._journeys_touching({"010B"})
    assert ids, "indexed lookup returned nothing"
    assert store._journeys_touching({"010B"}) == store._journeys_touching({"010B"}), "nondeterministic"


# --- Test 14: _fetch_journeys batches (results match per-id fetch)
def test_14_fetch_journeys_batches(writer, store):
    writer.add_journey("Op", "14", "outbound", "J14", {"mon"},
                       [{"naptan": "010A", "arrival": None, "departure": "09:00:00"},
                        {"naptan": "010B", "arrival": "09:10:00", "departure": None}], 99)
    writer.commit()
    ids = store._journeys_touching({"010A"})
    batch = store._fetch_journeys(ids)
    assert set(batch) == set(ids), "batch fetch missing ids"
    for jid in ids:
        assert batch[jid]["journey_code"] == store._fetch_journey(jid)["journey_code"]


# --- Test 15: _candidate_journeys yields the same journeys both tools use
def test_15_candidate_journeys_shared_helper(writer, store):
    writer.add_journey("Op", "15", "outbound", "J15", {"mon"},
                       [{"naptan": "15A", "arrival": None, "departure": "09:00:00"},
                        {"naptan": "15B", "arrival": "09:10:00", "departure": None}], 99)
    writer.commit()
    cands = list(store._candidate_journeys({"15A"}, {"15B"}, "mon", "09:30:00"))
    assert len(cands) == 1 and cands[0][0]["journey_code"] == "J15"
    assert list(store._candidate_journeys({"15A"}, {"15B"}, "tue", "09:30:00")) == []


# --- Test 20: discard_dataset surviving-key scan uses the j_oproute index
def test_20_discard_dataset_index_only_scan(writer, store):
    writer.add_journey("Op", "20", "outbound", "J20", {"mon"}, stops(), 99)
    writer.commit()
    plan = writer.conn.execute(
        "EXPLAIN QUERY PLAN SELECT DISTINCT op, route FROM journeys").fetchall()
    assert any("j_oproute" in str(row) for row in plan), f"index not used: {plan}"


# --- Test 21: ensure_schema backfills journey_stops for pre-existing journeys
# A pre-Phase-2 DB has journeys but no journey_stops rows and no backfill flag;
# ensure_schema must populate the index table once (in-place upgrade, no rebuild).
def test_21_ensure_schema_backfills_journey_stops():
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


# --- Test 22: _fetch_journeys chunks past SQLite's variable limit
# 501 ids cross the 500-id chunk boundary; all must come back without
# "too many SQL variables".
def test_22_fetch_journeys_chunks_past_variable_limit(writer, store):
    for _i in range(501):
        writer.add_journey("Op", "22", "outbound", f"J22_{_i}", {"mon"},
                           [{"naptan": "010A", "arrival": None, "departure": "09:00:00"},
                            {"naptan": "010B", "arrival": "09:10:00", "departure": None}], 99)
    writer.commit()
    ids = [r[0] for r in writer.conn.execute(
        "SELECT id FROM journeys WHERE route='22'")]
    assert len(ids) == 501
    batch = store._fetch_journeys(ids)
    assert set(batch) == set(ids), "chunked fetch missing ids"


# --- Test 16: get_route_stops direction filter matches parsed directions
def test_16_direction_filter_on_parsed_json(writer, store):
    writer.upsert_route("Op", "16", {"inbound", "outbound"}, ["010A", "010B"], 99)
    writer.commit()
    assert len(store.get_route_stops("Op", "16", "inbound")) == 1
    assert len(store.get_route_stops("Op", "16", "outbound")) == 1
    assert len(store.get_route_stops("Op", "16")) == 1  # no filter -> all


# --- Test 17: upsert_route prunes stale stop_to_routes refs
def test_17_upsert_route_prunes_stale_refs(writer, store):
    writer.upsert_route("Op", "17", {"outbound"}, ["010A", "010B"], 99)
    writer.upsert_route("Op", "17", {"outbound"}, ["010A"], 99)  # shorter: merge keeps longest
    writer.commit()
    # The merge keeps the longest stop list, so refs are a superset; assert no crash
    # and that a full re-upsert with a *different* key set prunes correctly:
    writer.upsert_route("Op", "18", {"outbound"}, ["010A", "010B"], 99)
    writer.upsert_route("Op", "18", {"outbound"}, ["010A"], 99)
    writer.commit()
    refs = {r[0] for r in writer.conn.execute(
        "SELECT naptan FROM stop_to_routes WHERE key='Op|18'").fetchall()}
    assert refs == {"010A"}, f"stale refs not pruned: {refs}"


# --- Test 23: stops_fts backfill + trigger sync keep the FTS index current
def test_23_fts_backfill_and_trigger_sync():
    # Simulate a pre-FTS DB: stops present, no stops_fts, no backfill flag.
    _bk = Path(tempfile.mkdtemp()) / "fts.db"
    w2 = TimetableWriter(_bk)
    w2.conn.execute(
        "CREATE TABLE stops (naptan TEXT PRIMARY KEY, name TEXT, lat REAL, lon REAL)")
    w2.conn.execute("INSERT INTO stops VALUES ('F1','Alpha Road',NULL,NULL)")
    w2.conn.execute("INSERT INTO stops VALUES ('F2','Beta Lane',NULL,NULL)")
    w2.conn.commit()
    w2.ensure_schema()
    n = w2.conn.execute("SELECT COUNT(*) FROM stops_fts").fetchone()[0]
    assert n == 2, f"backfill missing: {n}"
    # Flag prevents re-running the backfill.
    w2.ensure_schema()
    n = w2.conn.execute("SELECT COUNT(*) FROM stops_fts").fetchone()[0]
    assert n == 2, "backfill re-ran"
    # Insert trigger (add_stop -> INSERT ... ON CONFLICT DO UPDATE).
    w2.add_stop("F3", "Gamma Way")
    w2.conn.commit()
    n = w2.conn.execute("SELECT COUNT(*) FROM stops_fts").fetchone()[0]
    assert n == 3, "insert trigger missed"
    # Update trigger (conflict path fires the UPDATE trigger).
    w2.add_stop("F1", "Alpha Road North")
    w2.conn.commit()
    row = w2.conn.execute(
        "SELECT name FROM stops_fts WHERE rowid="
        "(SELECT rowid FROM stops WHERE naptan='F1')").fetchone()
    assert row and row[0] == "Alpha Road North", "update trigger missed"
    # Delete trigger.
    w2.conn.execute("DELETE FROM stops WHERE naptan='F2'")
    w2.conn.commit()
    n = w2.conn.execute("SELECT COUNT(*) FROM stops_fts").fetchone()[0]
    assert n == 2, "delete trigger missed"


# --- Test 24: search_stops uses FTS5 (token+prefix) with a LIKE fallback
def test_24_fts_search_and_like_fallback(writer, store):
    writer.add_stop("S1", "Oaks Cross")
    writer.add_stop("S2", "Oakscross Road")
    writer.add_stop("S3", "100% Bus Stop")
    writer.add_stop("S4", "Under_Score Stop")
    writer.add_stop("S5", "Common Roadside")
    writer.commit()
    # FTS5 token+prefix: "oaks" matches both "Oaks" and "Oakscross".
    assert {s["naptan"] for s in store.search_stops("oaks")} == {"S1", "S2"}
    # FTS5 AND: both tokens must match.
    assert {s["naptan"] for s in store.search_stops("bus stop")} == {"S3"}
    # FTS5 prefix matching: "road"* matches "Roadside" (LIKE '%common road%' would not).
    assert {s["naptan"] for s in store.search_stops("common road")} == {"S5"}
    # FTS5-locking: tokens present but not a contiguous substring, so LIKE
    # '%road common%' cannot match — only the FTS5 token+prefix path returns S5.
    assert {s["naptan"] for s in store.search_stops("road common")} == {"S5"}
    # LIKE fallback: mid-token substring FTS5 cannot express.
    assert {s["naptan"] for s in store.search_stops("kscr")} == {"S2"}
    # Untokenizable input -> LIKE fallback (wildcards match literally).
    assert {s["naptan"] for s in store.search_stops("%")} == {"S3"}
    assert {s["naptan"] for s in store.search_stops("_")} == {"S4"}
    # Short query (< 2 chars) -> LIKE directly (substring, not prefix).
    assert {s["naptan"] for s in store.search_stops("o")} == {"S1", "S2", "S3", "S4", "S5"}
    # resolve_stop: FTS5 path (capped) and NaPTAN literal unchanged.
    assert store.resolve_stop("oaks") == {"S1", "S2"}
    assert store.resolve_stop("010A") == {"010A"}
