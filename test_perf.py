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
