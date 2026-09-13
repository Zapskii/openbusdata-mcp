# test_planner.py
from defusedxml import ElementTree as SafeET

from openbusdata_mcp.server import _parse_days
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


def test_one_change_disjoint_mids_is_empty(writer, store):
    # A-side and B-side networks share NO transfer stop: the mid-candidate
    # intersection is empty, so the plan is empty and cheap to prove.
    writer.add_journey("OpA", "7", "outbound", "J7e", {"mon"},
                       A_TO_M + [{"naptan": "Z9", "arrival": "09:40:00",
                                  "departure": None}], 1)
    writer.add_journey("OpB", "8", "outbound", "J7f", {"mon"},
                       [{"naptan": "W1", "arrival": None, "departure": "09:15:00"},
                        {"naptan": "B1", "arrival": "09:25:00", "departure": None}], 2)
    writer.commit()
    assert store.plan_one_change({"A1"}, {"B1"}, "mon", "10:00:00") == []


# --- _parse_days: TransXChange encodes regular days as presence-only
# EMPTY elements (<Monday/>); text is None there, not "true". Empty element
# = day applies. Explicit text ("true"/"1") must keep working.
def test_parse_days_presence_only_element_applies():
    op = SafeET.fromstring(
        "<OperatingProfile><RegularDayType><DaysOfWeek>"
        "<Saturday/></DaysOfWeek></RegularDayType></OperatingProfile>")
    assert _parse_days(op) == {"sat"}


def test_parse_days_explicit_text_still_applies():
    op = SafeET.fromstring(
        "<OperatingProfile><RegularDayType><DaysOfWeek>"
        "<Monday>true</Monday><Friday>1</Friday>"
        "</DaysOfWeek></RegularDayType></OperatingProfile>")
    assert _parse_days(op) == {"mon", "fri"}


def test_parse_days_none_profile_is_empty_service_level_decides():
    # Contract change (BUG_REPORT round 4): a VJ with no OperatingProfile no
    # longer means "runs every day". BODS declares days on the <Service>
    # element; parse_transxchange falls back per-VJ -> Service -> all-days.
    # At this level, None simply means "nothing declared here".
    assert _parse_days(None) == set()
