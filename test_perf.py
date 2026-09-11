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
