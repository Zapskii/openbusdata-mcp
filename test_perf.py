# test_perf.py
import tempfile
from pathlib import Path
from openbusdata_mcp.store import TimetableWriter

STOP = lambda n, a, d: {"naptan": n, "arrival": a, "departure": d}  # noqa: E731

def test_25_journey_stop_times_populated_on_add(writer, store):
    writer.add_journey("Op", "25", "outbound", "J25", {"mon"}, [
        STOP("010A", None, "09:00:00"),
        STOP("010B", "09:10:00", None),
        STOP("010C", None, None)], 99)
    writer.commit()
    rows = writer.conn.execute(
        "SELECT naptan, dep FROM journey_stop_times WHERE journey_id=1 "
        "ORDER BY naptan").fetchall()
    assert rows == [("010A", "09:00:00"), ("010B", "09:10:00"), ("010C", None)], rows


def test_26_journey_stop_times_backfill():
    _bk = Path(tempfile.mkdtemp()) / "upgrade.db"
    w2 = TimetableWriter(_bk)
    w2.ensure_schema()
    w2.add_journey("Op", "26", "outbound", "J26", {"mon"}, [
        STOP("010A", None, "09:00:00"), STOP("010B", "09:10:00", None)], 5)
    w2.conn.execute("DELETE FROM journey_stop_times")
    w2.conn.execute("DELETE FROM meta WHERE k='journey_stop_times_backfilled'")
    w2.conn.commit()
    w2.ensure_schema()  # must backfill from stored JSON
    n = w2.conn.execute(
        "SELECT COUNT(*) FROM journey_stop_times WHERE journey_id=1").fetchone()[0]
    assert n == 2, f"backfill missing: {n}"


def test_27_journey_stop_times_purged_on_discard(writer, store):
    writer.add_journey("Op", "27", "outbound", "J27", {"mon"}, [
        STOP("010A", None, "09:00:00"), STOP("010B", "09:10:00", None)], 5)
    writer.add_journey("Op2", "27b", "outbound", "J27b", {"mon"}, [
        STOP("010A", None, "08:00:00")], 0)
    writer.commit()
    assert writer.conn.execute("SELECT COUNT(*) FROM journey_stop_times").fetchone()[0] == 3
    writer.discard_dataset(5)
    assert writer.conn.execute(
        "SELECT COUNT(*) FROM journey_stop_times WHERE journey_id IN "
        "(SELECT id FROM journeys WHERE ds_id=0)").fetchone()[0] == 1
    writer.discard_untagged_for("Op2", "27b")
    assert writer.conn.execute("SELECT COUNT(*) FROM journey_stop_times").fetchone()[0] == 0


def _old_candidates(store, na, nb, day, target_s):
    """Pre-change implementation — frozen reference reading journeys.json."""
    import json as _json
    rows = store.conn.execute("SELECT id, days, json FROM journeys").fetchall()
    for jid, days, raw in rows:
        if day not in _json.loads(days):
            continue
        stops = _json.loads(raw)["stops"]
        ia = next((i for i, s in enumerate(stops) if s["naptan"] in na), None)
        ib = next((i for i, s in enumerate(stops) if s["naptan"] in nb), None)
        if ia is None or ib is None or ia >= ib:
            continue
        ar = stops[ib].get("arrival")
        if ar and ar[:8] <= target_s:
            yield jid, ia, ib


def test_29_candidate_journeys_intersection_parity(writer, store):
    # _candidate_journeys rows are unordered SQL output; both sides compared
    # sorted on (journey_id, seq_a, seq_b) triples.
    for i in range(2):
        seq = ["CTR0", f"R{i}_hub", f"R{i}_1", f"R{i}_2"]
        for dep0 in ("08:00:00", "09:00:00"):
            stops = [{"naptan": s, "arrival": dep0, "departure": dep0} for s in seq]
            writer.add_journey(f"Op{i}", str(i), "outbound",
                               f"J{i}-{dep0}", {"mon"}, stops, 1)
    writer.commit()
    for na, nb in [({"CTR0"}, {"R0_2"}), ({"CTR0", "R1_hub"}, {"R0_1", "R1_2"})]:
        got = [(c.journey_id, c.seq_a, c.seq_b)
               for c in store._candidate_journeys(na, nb, "mon", "09:30:00")]
        ref = list(_old_candidates(store, na, nb, "mon", "09:30:00"))
        assert sorted(got) == sorted(ref), (na, nb, got, ref)


def test_31_routes_fts_backfill_and_trigger_sync():
    _bk = Path(tempfile.mkdtemp()) / "upgrade.db"
    w2 = TimetableWriter(_bk)
    w2.ensure_schema()
    w2.upsert_route("RadialOp1", "31", {"outbound"}, ["010A", "010B"], 1)
    w2.conn.execute("DELETE FROM routes_fts")
    w2.conn.execute("DELETE FROM meta WHERE k='routes_fts_backfilled'")
    w2.conn.commit()
    w2.ensure_schema()  # must backfill existing routes
    n = w2.conn.execute(
        "SELECT COUNT(*) FROM routes_fts WHERE routes_fts MATCH '31'").fetchone()[0]
    assert n == 1, f"backfill missing: {n}"
    # Backfill must carry the UNINDEXED key column, mirroring the AI trigger.
    real_key = w2.conn.execute(
        "SELECT key FROM routes WHERE num='31'").fetchone()[0]
    b_key = w2.conn.execute(
        "SELECT key FROM routes_fts WHERE routes_fts MATCH '31'").fetchone()[0]
    assert b_key == real_key, f"backfill dropped key col: {b_key} != {real_key}"
    w2.upsert_route("Metrolink", "12", {"outbound"}, ["010A"], 1)  # trigger on INSERT
    n2 = w2.conn.execute(
        "SELECT COUNT(*) FROM routes_fts WHERE routes_fts MATCH '12'").fetchone()[0]
    assert n2 == 1, f"insert trigger missing: {n2}"
    # Drift the index, then re-fire the UPDATE path: routes_fts_au must repair it.
    w2.conn.execute(
        "UPDATE routes_fts SET num='99' WHERE rowid="
        "(SELECT rowid FROM routes WHERE key='Metrolink|12')")
    w2.conn.commit()
    w2.upsert_route("Metrolink", "12", {"outbound", "inbound"}, ["010A"], 1)  # UPDATE path
    au = w2.conn.execute(
        "SELECT num, key FROM routes_fts WHERE routes_fts MATCH '12'").fetchall()
    assert au == [("12", "Metrolink|12")], f"update trigger missing: {au}"
    # DELETE trigger: discard_dataset leaves route rows in place by design, so
    # delete the route row directly and assert the FTS row follows.
    w2.conn.execute("DELETE FROM routes WHERE key='Metrolink|12'")
    w2.conn.commit()
    assert w2.conn.execute(
        "SELECT COUNT(*) FROM routes_fts WHERE routes_fts MATCH '12'").fetchone()[0] == 0, \
        "delete trigger left a routes_fts row"
    assert w2.conn.execute(
        "SELECT COUNT(*) FROM routes_fts WHERE routes_fts MATCH '31'").fetchone()[0] == 1


def test_32_get_route_stops_fts_first_like_fallback(writer, store):
    writer.upsert_route("RadialOp1", "12", {"outbound", "inbound"}, ["010A", "010B"], 1)
    writer.upsert_route("Metrolink", "12", {"outbound"}, ["010A"], 1)
    writer.upsert_route("RadialOp1", "30", {"outbound"}, ["010A"], 1)
    writer.upsert_route("RadialOp1", "Cross City", {"outbound"}, ["010A"], 1)
    writer.commit()
    got = store.get_route_stops("RadialOp1", "12")  # exact op+num via FTS
    assert [(g["operator"], g["route"]) for g in got] == [("RadialOp1", "12")]
    got = store.get_route_stops("Radial", "12")     # partial-op prefix via FTS
    assert got and got[0]["route"] == "12"
    assert got and "Metrolink" not in [g["operator"] for g in got]
    got = store.get_route_stops("%", "%")           # untokenizable -> LIKE fallback
    assert len(got) == 4  # all four seeded routes (12/12/30/Cross City)
    got = store.get_route_stops("RadialOp1", "City Cross")  # tokens in reversed order
    assert [(g["operator"], g["route"]) for g in got] == [("RadialOp1", "Cross City")], got
    got = store.get_route_stops("RadialOp1", "12", direction="cross")
    assert got == []                                # direction filter still applies
    got = store.get_route_stops("RadialOp1", "12")
    assert got[0]["directions"] == ["inbound", "outbound"]


def test_33_short_search_uses_fts(writer, store):
    writer.add_stop("010A", "Abbey Road")
    writer.add_stop("010B", "Station X")
    writer.commit()
    got = store.search_stops("a")            # 1 char -> FTS prefix "a"*
    names = [g["name"] for g in got]
    assert "Abbey Road" in names, names       # abbey starts with "a"
    got = store.search_stops("")              # empty -> no rows, must stay capped
    assert 0 <= len(got) <= 50


def test_34_read_conn_mmap_enabled(store):
    v = store.conn.execute("PRAGMA mmap_size").fetchone()[0]
    assert v >= 268435456, f"mmap not enabled: {v}"


def test_35_fts_optimize_after_full_load(writer):
    writer.add_stop("010A", "Abbey Road")
    writer.upsert_route("Op", "12", {"outbound"}, ["010A"], 1)
    writer.commit()
    writer.optimize_fts()   # must not break searching
    writer.commit()
    n = writer.conn.execute(
        "SELECT COUNT(*) FROM stops_fts WHERE stops_fts MATCH 'abbey'").fetchone()[0]
    assert n == 1, f"optimize broke FTS: {n}"
    n = writer.conn.execute(
        "SELECT COUNT(*) FROM routes_fts WHERE routes_fts MATCH '12'").fetchone()[0]
    assert n == 1, f"routes FTS broken: {n}"


def test_36_plan_cache_hit_and_invalidation(writer, store):
    writer.add_journey("Op", "36", "outbound", "J36", {"mon"}, [
        {"naptan": "010A", "arrival": None, "departure": "09:00:00"},
        {"naptan": "010B", "arrival": "09:10:00", "departure": None}], 1)
    writer.add_stop("010A", "Alpha"); writer.add_stop("010B", "Beta")
    writer.commit()
    r1 = store.plan_direct({"010A"}, {"010B"}, "mon", "10:00:00")
    assert len(r1) == 1, r1
    assert store.plan_direct({"010A"}, {"010B"}, "mon", "10:00:00") == r1   # cache hit
    assert store._plan_direct_cached.cache_info().hits == 1, "repeat call must reuse the cache"
    assert store.plan_direct({"010A"}, {"010B"}, "tue", "10:00:00") == []   # different key
    writer.add_journey("Op2", "36b", "outbound", "J36b", {"mon"}, [
        {"naptan": "010A", "arrival": None, "departure": "08:00:00"},
        {"naptan": "010B", "arrival": "08:10:00", "departure": None}], 2)
    writer.mark_dataset_loaded(2, None, "Op2")
    writer.commit()
    r4 = store.plan_direct({"010A"}, {"010B"}, "mon", "10:00:00")
    assert len(r4) == 2, r4   # data_key changed -> cache invalidated
    assert store._plan_direct_cached.cache_info().hits == 1, "dataset change must invalidate (recompute is a miss)"
