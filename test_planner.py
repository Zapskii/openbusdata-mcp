# test_planner.py
from openbusdata_mcp.store import _days_mask  # noqa: F401  (import sanity)

A_TO_M = [{"naptan": "A1", "arrival": None, "departure": "09:00:00"},
          {"naptan": "M1", "arrival": "09:10:00", "departure": "09:12:00"},
          {"naptan": "B1", "arrival": "09:20:00", "departure": None}]
M_TO_B = [{"naptan": "M1", "arrival": None, "departure": "09:15:00"},
          {"naptan": "B1", "arrival": "09:25:00", "departure": None}]


def _seed(writer):
    # Seed stop names so board/alight resolve (store looks names up in `stops`).
    for n in ("A1", "M1", "B1"):
        writer.add_stop(n, n)
    writer.add_journey("OpA", "7", "outbound", "J7a", {"mon"}, A_TO_M, 1)
    writer.add_journey("OpB", "8", "outbound", "J7b", {"mon"}, M_TO_B, 2)
    writer.commit()


def test_one_change_plan_found(writer, store):
    _seed(writer)
    plans = store.plan_one_change({"A1"}, {"B1"}, "mon", "10:00:00")
    assert len(plans) == 1, plans
    p = plans[0]
    assert p["type"] == "change" and p["total_changes"] == 1
    leg1, leg2 = p["legs"]
    assert (leg1["operator"], leg1["board"], leg1["alight"]) == ("OpA", "A1", "M1")
    assert leg1["depart"] == "09:00:00" and leg1["arrive"] == "09:10:00"
    assert (leg2["operator"], leg2["board"], leg2["alight"]) == ("OpB", "M1", "B1")
    assert leg2["depart"] == "09:15:00" and leg2["arrive"] == "09:25:00"


def test_one_change_respects_target(writer, store):
    _seed(writer)
    # Change itinerary arrives 09:25 — excluded by a 09:24 target.
    assert store.plan_one_change({"A1"}, {"B1"}, "mon", "09:24:00") == []


def test_one_change_respects_day(writer, store):
    _seed(writer)
    assert store.plan_one_change({"A1"}, {"B1"}, "tue", "10:00:00") == []


def test_one_change_no_self_transfer(writer, store):
    # Single journey through A->M->B must NOT yield a change plan onto itself.
    writer.add_journey("OpA", "7c", "outbound", "J7c", {"mon"}, A_TO_M, 1)
    writer.commit()
    assert store.plan_one_change({"A1"}, {"B1"}, "mon", "10:00:00") == []


def test_one_change_direct_first_occurrence_not_reboarded(writer, store):
    # j2 boards at the FIRST occurrence of M1; a later M1 in j2 is ignored.
    writer.add_journey("OpA", "7", "outbound", "J7a", {"mon"}, A_TO_M, 1)
    writer.add_journey("OpB", "8", "outbound", "J7d", {"mon"}, [
        {"naptan": "M1", "arrival": "08:00:00", "departure": "08:05:00"},   # first M1: too early
        {"naptan": "X9", "arrival": "08:30:00", "departure": None},
        {"naptan": "M1", "arrival": "09:15:00", "departure": "09:16:00"},   # later M1: would work, must be ignored
        {"naptan": "B1", "arrival": "09:25:00", "departure": None}], 2)
    writer.commit()
    assert store.plan_one_change({"A1"}, {"B1"}, "mon", "10:00:00") == []
