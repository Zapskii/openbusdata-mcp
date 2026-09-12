# test_relational.py
import openbusdata_mcp.store as store_mod
from openbusdata_mcp.store import DAY_BITS, _days_mask


def test_days_mask_bit_values():
    assert DAY_BITS == {"mon": 1, "tue": 2, "wed": 4, "thu": 8,
                        "fri": 16, "sat": 32, "sun": 64}
    assert _days_mask({"mon", "tue"}) == 3
    assert _days_mask({"sun"}) == 64
    assert _days_mask({"mon", "wed", "fri"}) == 1 | 4 | 16
    assert _days_mask(set()) == 0
    assert _days_mask({"bogus"}) == 0, "unknown day names contribute nothing"


def test_add_journey_writes_days_mask(writer, store):
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