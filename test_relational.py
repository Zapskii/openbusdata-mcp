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
    # Simulate the true deployed legacy state: old-shape rows (no seq/arr)
    # AND the v1 backfill flag already set to '1' — the upgrade must still
    # run, because the gate is column-driven, not flag-driven.
    writer.conn.execute("DROP TABLE journey_stop_times")
    writer.conn.execute(
        "CREATE TABLE journey_stop_times (naptan TEXT, journey_id INT, dep TEXT)")
    writer.conn.execute(
        "INSERT INTO journey_stop_times (naptan, journey_id, dep) "
        "SELECT json_extract(value, '$.naptan'), j.id, "
        "       COALESCE(json_extract(value, '$.departure'),"
        "                json_extract(value, '$.arrival')) "
        "FROM journeys j, json_each(j.json, '$.stops')")
    writer.conn.execute(
        "INSERT OR REPLACE INTO meta VALUES"
        " ('journey_stop_times_backfilled', '1')")
    writer.conn.commit()
    writer.ensure_schema()  # legacy rows must be upgraded despite flag='1'
    rows = writer.conn.execute(
        "SELECT seq, naptan, dep, arr FROM journey_stop_times "
        "WHERE journey_id=1 ORDER BY seq").fetchall()
    assert rows == [(0, "010A", "08:00:00", None),
                    (1, "010B", "08:10:00", "08:10:00")], rows
    assert writer.conn.execute(
        "SELECT v FROM meta WHERE k='journey_stop_times_backfilled'"
    ).fetchone()[0] == "2"
    # A v2 DB with the flag present must NOT rebuild (idempotence): re-run
    # leaves the rebuilt rows untouched.
    writer.ensure_schema()
    rows = writer.conn.execute(
        "SELECT seq, naptan, dep, arr FROM journey_stop_times "
        "WHERE journey_id=1 ORDER BY seq").fetchall()
    assert rows == [(0, "010A", "08:00:00", None),
                    (1, "010B", "08:10:00", "08:10:00")], rows


def test_v1_shaped_db_upgrade_keeps_queries_working(writer, store):
    """A genuinely v1-shaped DB (3-column table, flag='1') must upgrade at
    ensure_schema without breaking candidate queries or index creation."""
    writer.add_journey("Op", "7", "outbound", "J7", {"mon"}, [
        {"naptan": "020A", "arrival": None, "departure": "07:00:00"},
        {"naptan": "020B", "arrival": "07:15:00", "departure": None}], 5)
    writer.commit()
    writer.conn.execute("DROP TABLE journey_stop_times")
    writer.conn.execute(
        "CREATE TABLE journey_stop_times (naptan TEXT, journey_id INT, dep TEXT)")
    writer.conn.execute(
        "INSERT INTO journey_stop_times (naptan, journey_id, dep) "
        "SELECT json_extract(value, '$.naptan'), j.id, "
        "       COALESCE(json_extract(value, '$.departure'),"
        "                json_extract(value, '$.arrival')) "
        "FROM journeys j, json_each(j.json, '$.stops')")
    writer.conn.execute(
        "INSERT OR REPLACE INTO meta VALUES"
        " ('journey_stop_times_backfilled', '1')")
    writer.conn.commit()
    writer.ensure_schema()
    cols = {r[1] for r in writer.conn.execute(
        "PRAGMA table_info(journey_stop_times)")}
    assert {"seq", "arr"} <= cols
    assert writer.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='jst_j'"
    ).fetchone(), "jst_j index must exist after upgrade"
    # Candidate query must run (previously: "no such column: seq").
    cands = list(store._candidate_journeys({"020A"}, {"020B"}, "mon", "08:00:00"))
    assert len(cands) == 1
    assert (cands[0].code, cands[0].naptan_a, cands[0].naptan_b) == \
        ("J7", "020A", "020B")
    assert cands[0].arrive_b == "07:15:00"


def test_candidate_journeys_sql_parity(writer, store):
    writer.add_journey("Op", "6", "outbound", "J6", {"mon"}, [
        {"naptan": "A1", "arrival": None, "departure": "09:00:00"},
        {"naptan": "A2", "arrival": "09:05:00", "departure": None},
        {"naptan": "B1", "arrival": "09:20:00", "departure": None}], 5)
    writer.add_journey("Op", "6b", "outbound", "J6b", {"tue"}, [
        {"naptan": "A1", "arrival": None, "departure": "09:00:00"},
        {"naptan": "B1", "arrival": "09:20:00", "departure": None}], 5)
    writer.commit()
    cands = list(store._candidate_journeys({"A1"}, {"B1"}, "mon", "09:30:00"))
    assert len(cands) == 1
    c = cands[0]
    assert (c.journey_id, c.code, c.naptan_a, c.naptan_b) == (1, "J6", "A1", "B1")
    assert c.depart_a == "09:00:00" and c.arrive_b == "09:20:00"
    assert (c.seq_a, c.seq_b) == (0, 2)
    # First-occurrence semantics: A2 appears before B1; boarding at A2 works.
    c2 = list(store._candidate_journeys({"A2"}, {"B1"}, "mon", "09:30:00"))
    assert len(c2) == 1 and c2[0].seq_a == 1 and c2[0].seq_b == 2
    # Day mask filters in SQL: the tue journey only on tue.
    tue = list(store._candidate_journeys({"A1"}, {"B1"}, "tue", "09:30:00"))
    assert [x.code for x in tue] == ["J6b"]
    # Arrival target filters: nothing arrives by 09:19.
    assert list(store._candidate_journeys({"A1"}, {"B1"}, "mon", "09:19:00")) == []


def test_next_departures_orders_and_names_destination(seeded_route):
    board = seeded_route.next_departures({"010A"}, "mon", "08:00:00", 10)
    assert [d["depart"] for d in board] == ["09:00:00", "09:30:00"]
    assert board[0] == {"operator": "OPX", "route": "12", "direction": "outbound",
                        "journey_code": "J1", "depart": "09:00:00",
                        "destination": "Charlie Road"}


def test_next_departures_respects_day_and_limit(seeded_route):
    assert seeded_route.next_departures({"010A"}, "tue", "08:00:00", 10) == []
    only_one = seeded_route.next_departures({"010A"}, "mon", "08:00:00", 1)
    assert [d["depart"] for d in only_one] == ["09:00:00"]


def test_next_departures_unknown_day_and_empty_set(seeded_route):
    assert seeded_route.next_departures({"010A"}, "bogus", "08:00:00", 10) == []
    assert seeded_route.next_departures(set(), "mon", "08:00:00", 10) == []


def test_next_departures_keeps_departure_when_terminus_has_no_stop_row(writer, store):
    """A journey whose final stop has no `stops` row must still be reported:
    the origin resolves, so dropping the row would silently under-report."""
    writer.add_stop("010A", "Alpha Street", 51.5, -0.1)
    writer.upsert_route("OPX", "12", {"outbound"}, ["010A", "010Z"], 1)
    writer.add_journey("OPX", "12", "outbound", "J9", {"mon"}, [
        {"naptan": "010A", "arrival": None, "departure": "09:00:00"},
        {"naptan": "010Z", "arrival": "09:20:00", "departure": None}], 1)
    writer.commit()
    board = store.next_departures({"010A"}, "mon", "08:00:00", 10)
    assert [d["depart"] for d in board] == ["09:00:00"]
    assert board[0]["destination"] == "Unknown"
