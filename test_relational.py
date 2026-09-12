# test_relational.py
from openbusdata_mcp.store import DAY_BITS, _days_mask


def test_days_mask_bit_values():
    assert DAY_BITS == {"mon": 1, "tue": 2, "wed": 4, "thu": 8,
                        "fri": 16, "sat": 32, "sun": 64}
    assert _days_mask({"mon", "tue"}) == 3
    assert _days_mask({"sun"}) == 64
    assert _days_mask({"mon", "wed", "fri"}) == 1 | 4 | 16
    assert _days_mask(set()) == 0
    assert _days_mask({"bogus"}) == 0, "unknown day names contribute nothing"


def test_add_journey_writes_days_mask(writer):
    writer.add_journey("Op", "4", "outbound", "J4", {"sat", "sun"},
                       [{"naptan": "010A", "arrival": None, "departure": "09:00:00"},
                        {"naptan": "010B", "arrival": "09:10:00", "departure": None}], 5)
    writer.commit()
    mask = writer.conn.execute(
        "SELECT days_mask FROM journeys WHERE id=1").fetchone()[0]
    assert mask == 96


def test_days_mask_backfilled_on_legacy_db(writer):
    # Simulate a legacy row written before days_mask existed. The per-test
    # fixture already ran ensure_schema on an empty DB (setting the flag), so
    # clear the flag first — exactly the state of a real legacy DB.
    writer.conn.execute(
        "INSERT INTO journeys (id, ds_id, op, route, direction, code, days, json) "
        "VALUES (10, 7, 'Op', '4b', 'outbound', 'J4b', '[\"thu\"]', '{}')")
    writer.commit()
    writer.conn.execute("DELETE FROM meta WHERE k='days_mask_backfilled'")
    writer.conn.commit()
    writer.ensure_schema()  # must backfill days_mask from the days JSON
    mask = writer.conn.execute(
        "SELECT days_mask FROM journeys WHERE id=10").fetchone()[0]
    assert mask == 8
    # Second ensure_schema with the flag present must not touch the row.
    writer.ensure_schema()
    assert writer.conn.execute(
        "SELECT days_mask FROM journeys WHERE id=10").fetchone()[0] == 8


def test_journey_stop_times_v2_rows(writer):
    writer.add_journey("Op", "5", "outbound", "J5", {"mon"}, [
        {"naptan": "010A", "arrival": None, "departure": "09:00:00"},
        {"naptan": "010B", "arrival": "09:10:00", "departure": "09:11:00"},
        {"naptan": "010C", "arrival": "09:20:00", "departure": None}], 5)
    writer.commit()
    rows = writer.conn.execute(
        "SELECT seq, naptan, dep, arr FROM journey_stop_times "
        "WHERE journey_id=1 ORDER BY seq").fetchall()
    assert rows == [(0, "010A", "09:00:00", None),
                    (1, "010B", "09:11:00", "09:10:00"),
                    (2, "010C", "09:20:00", "09:20:00")], rows


def test_journey_stop_times_v2_backfill(writer):
    writer.add_journey("Op", "5b", "outbound", "J5b", {"mon"}, [
        {"naptan": "010A", "arrival": None, "departure": "08:00:00"},
        {"naptan": "010B", "arrival": "08:10:00", "departure": None}], 5)
    writer.commit()
    # Simulate a pre-v2 DB: old-shape rows, no seq/arr, flag present.
    writer.conn.execute("DELETE FROM journey_stop_times")
    writer.conn.execute(
        "INSERT INTO journey_stop_times (naptan, journey_id, dep) "
        "SELECT json_extract(value, '$.naptan'), j.id, "
        "       COALESCE(json_extract(value, '$.departure'),"
        "                json_extract(value, '$.arrival')) "
        "FROM journeys j, json_each(j.json, '$.stops')")
    writer.conn.commit()
    writer.ensure_schema()  # flag present -> no v2 rebuild: rows stay old-shape
    assert writer.conn.execute(
        "SELECT seq FROM journey_stop_times LIMIT 1").fetchone()[0] is None
    # the real legacy path is exercised by resetting the flag:
    writer.conn.execute(
        "DELETE FROM meta WHERE k='journey_stop_times_backfilled'")
    writer.conn.commit()
    writer.ensure_schema()
    rows = writer.conn.execute(
        "SELECT seq, naptan, dep, arr FROM journey_stop_times "
        "WHERE journey_id=1 ORDER BY seq").fetchall()
    assert rows == [(0, "010A", "08:00:00", None),
                    (1, "010B", "08:10:00", "08:10:00")], rows
