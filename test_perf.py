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


def test_28_journeys_departing_uses_index_and_matches_old_semantics(writer, store):
    writer.add_journey("Op", "28", "outbound", "J28a", {"mon"}, [
        STOP("010A", None, "09:00:00"),
        STOP("010B", "09:10:00", "09:11:00")], 1)
    writer.add_journey("Op", "28", "outbound", "J28b", {"mon"}, [
        STOP("010A", None, "10:00:00"),
        STOP("010B", "09:10:00", None)], 1)
    writer.commit()
    # reference: the pre-index correlated json_each query (frozen semantics)
    ref = [r[0] for r in store.conn.execute(
        "SELECT id FROM journeys WHERE EXISTS ("
        "  SELECT 1 FROM json_each(journeys.json, '$.stops')"
        "  WHERE json_extract(value,'$.naptan')=?"
        "    AND COALESCE(json_extract(value,'$.departure'),"
        "                 json_extract(value,'$.arrival')) >= ?"
        ") ORDER BY id", ("010B", "09:11:00")).fetchall()]
    real_conn = store.conn
    captured: list[str] = []

    class _SpyConn:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args, **kw):
            captured.append(sql)
            return self._real.execute(sql, *args, **kw)

        def __getattr__(self, name):  # delegate everything else to the real conn
            return getattr(self._real, name)

    try:
        store._conn = _SpyConn(real_conn)   # conn property returns the spy
        assert store._journeys_departing("010B", "09:11:00") == ref, "parity with old semantics"
    finally:
        store._conn = real_conn
    assert any("journey_stop_times" in s and "json_each" not in s for s in captured), (
        f"_journeys_departing did not query the index table: {captured}")
    plan = writer.conn.execute(
        "EXPLAIN QUERY PLAN SELECT DISTINCT journey_id FROM journey_stop_times "
        "WHERE naptan=? AND dep>=? ORDER BY journey_id", ("010B", "09:11:00")).fetchall()
    assert any("jst_n" in str(row) for row in plan), f"index not used: {plan}"


def _old_candidates(store, na, nb, day, target_s):
    """Pre-change implementation — frozen reference for parity."""
    for jid, j in store._fetch_journeys(store._journeys_touching(nb)).items():
        if day not in j["days"]:
            continue
        ia = next((i for i, s in enumerate(j["stops"]) if s["naptan"] in na), None)
        ib = next((i for i, s in enumerate(j["stops"]) if s["naptan"] in nb), None)
        if ia is None or ib is None or ia >= ib:
            continue
        ar = j["stops"][ib].get("arrival")
        if ar and ar[:8] <= target_s:
            yield j, ia, ib


def test_29_candidate_journeys_intersection_parity(writer, store):
    # _journeys_touching returns UNORDERED ids (its query has no ORDER BY), so
    # both sides are compared sorted: (id, idx_a, idx_b) triples are orderable.
    for i in range(2):
        seq = ["CTR0", f"R{i}_hub", f"R{i}_1", f"R{i}_2"]
        for dep0 in ("08:00:00", "09:00:00"):
            stops = [{"naptan": s, "arrival": dep0, "departure": dep0} for s in seq]
            writer.add_journey(f"Op{i}", str(i), "outbound",
                               f"J{i}-{dep0}", {"mon"}, stops, 1)
    writer.commit()
    for na, nb in [({"CTR0"}, {"R0_2"}), ({"CTR0", "R1_hub"}, {"R0_1", "R1_2"})]:
        got = [(j["id"], ia, ib)
               for j, ia, ib in store._candidate_journeys(na, nb, "mon", "09:30:00")]
        ref = [(j["id"], ia, ib)
               for j, ia, ib in _old_candidates(store, na, nb, "mon", "09:30:00")]
        assert sorted(got) == sorted(ref), (na, nb, got, ref)


def test_30_candidate_journeys_fetches_only_intersection(writer, store):
    for i in range(2):
        seq = ["CTR0", f"R{i}_hub", f"R{i}_1", f"R{i}_2"]
        for dep0 in ("08:00:00", "09:00:00", "10:00:00"):
            stops = [{"naptan": s, "arrival": dep0, "departure": dep0} for s in seq]
            writer.add_journey(f"Op{i}", str(i), "outbound",
                               f"J{i}-{dep0}", {"mon"}, stops, 1)
    writer.commit()
    na, nb = {"R0_hub"}, {"CTR0", "R0_hub", "R0_1", "R0_2", "R1_hub", "R1_1", "R1_2"}
    touch_a = set(store._journeys_touching(na))
    touch_b = set(store._journeys_touching(nb))
    assert 0 < len(touch_a) < len(touch_b), "test needs a small-A/large-B scenario"
    orig = store._fetch_journeys
    seen = {}
    def spy(jids):
        seen["ids"] = list(jids)
        return orig(jids)
    store._fetch_journeys = spy
    try:
        list(store._candidate_journeys(na, nb, "mon", "23:59:59"))
    finally:
        store._fetch_journeys = orig
    assert set(seen["ids"]) == touch_a, "fetched more than the A-intersection"


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
