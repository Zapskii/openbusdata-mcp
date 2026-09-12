# test_live_tools.py — tool-level tests for the new live/coverage tools.
import asyncio
import json
from datetime import datetime

import httpx

import openbusdata_mcp.server as server

# Two messages: the first carries operator OPX, lines 12/15, stop 010A.
SIRI_SX = b'''<?xml version="1.0"?>
<Siri xmlns="http://www.siri.org.uk/siri">
 <ServiceDelivery>
  <InfoMessageDelivery>
   <InfoMessage>
    <RecordedAtTime>2026-09-12T10:00:00Z</RecordedAtTime>
    <ValidUntilTime>2026-09-12T18:00:00Z</ValidUntilTime>
    <Content>
      <OperatorRef>OPX</OperatorRef>
      <LineRef>12</LineRef>
      <LineRef>15</LineRef>
      <StopPointRef>010A</StopPointRef>
      <Summary>Road closure on Bravo Road</Summary>
    </Content>
   </InfoMessage>
   <InfoMessage>
    <RecordedAtTime>2026-09-12T10:05:00Z</RecordedAtTime>
    <Content><LineRef>99</LineRef><Summary>Diversion</Summary></Content>
   </InfoMessage>
  </InfoMessageDelivery>
 </ServiceDelivery>
</Siri>'''


def _install(handler):
    server.set_http_client(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    server._SX_CACHE = server.TTLCache(60.0)


def _reset():
    server.set_http_client(None)
    server._SX_CACHE = server.TTLCache(60.0)


def test_get_disruptions_returns_structured_and_filters():
    _install(lambda request: httpx.Response(200, content=SIRI_SX))
    try:
        msgs = json.loads(asyncio.run(server.get_disruptions()))
        assert len(msgs) == 2
        by_line = json.loads(asyncio.run(server.get_disruptions(line="12")))
        assert len(by_line) == 1 and by_line[0]["lines"] == ["12", "15"]
        by_op = json.loads(asyncio.run(server.get_disruptions(operator="OPX")))
        assert len(by_op) == 1 and by_op[0]["summary"] == "Road closure on Bravo Road"
        by_stop = json.loads(asyncio.run(server.get_disruptions(stop="010A")))
        assert len(by_stop) == 1
        assert asyncio.run(server.get_disruptions(line="nope")) == "No matching disruption messages."
    finally:
        _reset()


def test_get_disruptions_degrades_on_http_error():
    _install(lambda request: httpx.Response(500))
    try:
        out = asyncio.run(server.get_disruptions())
        assert "unavailable" in out
    finally:
        _reset()


def test_get_disruptions_pins_request_path_and_unparseable_body():
    # Two untested halves at once: the actual request path behind
    # _SX_PATHS["disruptions"] (a wrong entry would otherwise pass the suite),
    # and the unparseable-body half of _fetch_sx's contract -- only the
    # 500/unreachable half was covered, and a 200 with junk must degrade
    # exactly like a failure rather than raise through the tool.
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, content=b"<not-xml")

    _install(handler)
    try:
        out = asyncio.run(server.get_disruptions())
    finally:
        _reset()
    # Inline raise, not `assert ..., out`: `out` is a tool return from a tool
    # that just made an HTTP request, so on a feed that echoes the request URL
    # the value would carry ?api_key=..., and pytest renders a failing assert's
    # operands. The message therefore names the expectation without
    # interpolating the value. (Rulings R34 and R37.)
    if out != "Disruptions feed unavailable (fetch or parse failed).":
        raise AssertionError("an unparseable 200 body must degrade, not raise through")
    # Inline raise, not assert: the operand is a request URL carrying
    # ?api_key=..., and pytest renders assert operands on failure. (Ruling R34.)
    if len(seen) != 1:
        raise AssertionError(f"expected exactly one feed request, got {len(seen)}")
    path = seen[0].split("?")[0]
    assert path == f"{server.BASE_URL}/api/v1/siri-sx/", path
    assert "cancellations" not in path, path


def test_get_cancellations_filters():
    def handler(request):
        # Inline raise, not assert: the operand is the request URL, key included.
        if "/siri-sx/cancellations/" not in str(request.url):
            raise AssertionError("cancellations tool must hit the cancellations path")
        return httpx.Response(200, content=b'''<?xml version="1.0"?>
<Siri xmlns="http://www.siri.org.uk/siri">
 <EstimatedVehicleJourneyCancellations>
  <OperatorRef>OPX</OperatorRef><LineRef>12</LineRef>
  <FramedVehicleJourneyRef><VehicleJourneyRef>J-42</VehicleJourneyRef></FramedVehicleJourneyRef>
  <CancellationReason>breakdown</CancellationReason>
 </EstimatedVehicleJourneyCancellations>
</Siri>''')

    _install(handler)
    try:
        out = json.loads(asyncio.run(server.get_cancellations()))
        assert out[0]["vehicle_journey_ref"] == "J-42"
        assert json.loads(asyncio.run(server.get_cancellations(operator="OPX")))[0]["line"] == "12"
        assert asyncio.run(server.get_cancellations(operator="NOPE")) == "No matching cancellation entries."
    finally:
        _reset()


def test_get_departures_board_tool(seeded_route):
    out = json.loads(asyncio.run(server.get_departures_board(
        "Alpha Street", day="mon", from_time="08:00")))
    assert [d["depart"] for d in out] == ["09:00:00", "09:30:00"]
    assert out[0]["destination"] == "Charlie Road"


def test_get_departures_board_errors(seeded_route):
    out = asyncio.run(server.get_departures_board("Alpha Street", from_time="25:99"))
    assert "Invalid time format" in out
    out = asyncio.run(server.get_departures_board("Nowhere Street"))
    assert "Could not resolve stop" in out


def test_get_departures_board_blank_stop_name_is_unknown(writer, store):
    # A `stops` row can exist with an empty name (add_stop inserts the name as
    # given), so COALESCE(s.name, 'Unknown') alone renders such a destination
    # as "" — the fallback must cover a present-but-empty name too (P18).
    writer.add_stop("010A", "Alpha Street")
    writer.add_stop("010B", "")
    writer.upsert_route("OPX", "12", {"outbound"}, ["010A", "010B"], 1)
    writer.add_journey("OPX", "12", "outbound", "J1", {"mon"}, [
        {"naptan": "010A", "arrival": None, "departure": "09:00:00"},
        {"naptan": "010B", "arrival": "09:10:00", "departure": None}], 1)
    writer.commit()
    out = json.loads(asyncio.run(server.get_departures_board(
        "Alpha Street", day="mon", from_time="08:00")))
    assert out, "the seeded departure must be on the board"
    assert out[0]["destination"] == "Unknown", out


VM_ONE = b'''<?xml version="1.0"?>
<Siri xmlns="http://www.siri.org.uk/siri">
 <VehicleActivity>
  <MonitoredVehicleJourney>
   <VehicleRef>V1</VehicleRef>
   <DirectionRef>outbound</DirectionRef>
   <VehicleLocation><Latitude>51.511</Latitude><Longitude>-0.111</Longitude></VehicleLocation>
  </MonitoredVehicleJourney>
 </VehicleActivity>
</Siri>'''  # nearest route stop is 010B (51.51, -0.11), ~0.13 km away


class FakeDateTime(datetime):
    """Pins server 'now' so ETA math is deterministic."""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 12, 9, 5, 0)


def test_estimate_live_eta_scheduled_basis(seeded_route, monkeypatch):
    # now = 09:05; the bus is near 010B but J1 hasn't reached 010B yet
    # (scheduled 09:10) -> fallback: the next service, scheduled arrival 09:20.
    server.set_http_client(httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=VM_ONE))))
    server._LIVE_CACHE = server.TTLCache(20.0)
    monkeypatch.setattr(server, "datetime", FakeDateTime)
    try:
        out = json.loads(asyncio.run(server.estimate_live_eta(
            "Charlie Road", "OPX", "12", day="mon")))
        assert out["target_stop"] == "Charlie Road"
        v = out["vehicles"][0]
        assert v["vehicle_id"] == "V1"
        assert v["vehicle_at"] == "Bravo Road"
        assert v["distance_km"] < 1.0
        assert v["eta"]["minutes"] == 15  # 09:20 scheduled - 09:05 now
        assert v["eta"]["basis"] == "scheduled"
        assert v["eta"]["journey_code"] == "J1"
    finally:
        server.set_http_client(None)
        server._LIVE_CACHE = server.TTLCache(20.0)


def test_estimate_live_eta_schedule_offset_basis(seeded_route, monkeypatch):
    # now = 09:12: J1 has passed its 09:10 arrival at 010B -> the vehicle is
    # running 2 min late at 010B, so ETA = t_target - t_k remaining travel =
    # 09:20 - 09:10 = 10 minutes from now (arrival ~09:22), basis
    # schedule-offset.
    class FakeLater(FakeDateTime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 12, 9, 12, 0)

    server.set_http_client(httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=VM_ONE))))
    server._LIVE_CACHE = server.TTLCache(20.0)
    monkeypatch.setattr(server, "datetime", FakeLater)
    try:
        out = json.loads(asyncio.run(server.estimate_live_eta(
            "Charlie Road", "OPX", "12", day="mon")))
        v = out["vehicles"][0]
        assert v["eta"]["minutes"] == 10
        assert v["eta"]["basis"] == "schedule-offset"
    finally:
        server.set_http_client(None)
        server._LIVE_CACHE = server.TTLCache(20.0)


def test_estimate_live_eta_unmatched_vehicle(seeded_route, monkeypatch):
    far_vm = VM_ONE.replace(b"<Latitude>51.511</Latitude>",
                            b"<Latitude>40.0</Latitude>").replace(
        b"<Longitude>-0.111</Longitude>", b"<Longitude>-1.0</Longitude>")
    server.set_http_client(httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=far_vm))))
    server._LIVE_CACHE = server.TTLCache(20.0)
    monkeypatch.setattr(server, "datetime", FakeDateTime)
    try:
        out = asyncio.run(server.estimate_live_eta("Charlie Road", "OPX", "12", day="mon"))
        assert "No live buses matched" in out
    finally:
        server.set_http_client(None)
        server._LIVE_CACHE = server.TTLCache(20.0)


def test_estimate_live_eta_route_not_indexed(seeded_route):
    out = asyncio.run(server.estimate_live_eta("Charlie Road", "OPX", "77", day="mon"))
    assert "not in the timetable index" in out


EMPTY_VM = b'<Siri xmlns="http://www.siri.org.uk/siri"/>'


def test_live_buses_empty_feed_is_not_cached():
    # The guard at server.py:140 is deliberate: an empty AVL feed is usually
    # transient, so it must not be cached (unlike an authoritative empty SX
    # response). Deleting the guard leaves every other test green, so pin it.
    counts = {"n": 0}

    def handler(request):
        counts["n"] += 1
        return httpx.Response(200, content=EMPTY_VM)

    server.set_http_client(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    server._LIVE_CACHE = server.TTLCache(20.0)
    try:
        assert asyncio.run(server.get_live_buses_on_route("OPX", "12")) == "No live buses found."
        asyncio.run(server.get_live_buses_on_route("OPX", "12"))
        assert counts["n"] == 2, "an empty AVL feed must not be cached"
    finally:
        server.set_http_client(None)
        server._LIVE_CACHE = server.TTLCache(20.0)


def test_live_buses_missing_coordinate_renders_na():
    # V9 has a VehicleLocation with no Longitude; V10 has no VehicleLocation
    # element at all. Both must render the missing coordinate as "N/A" rather
    # than null (the shape get_live_buses_on_route has always published).
    partial_vm = b'''<?xml version="1.0"?>
<Siri xmlns="http://www.siri.org.uk/siri">
 <VehicleActivity>
  <MonitoredVehicleJourney>
   <VehicleRef>V9</VehicleRef>
   <VehicleLocation><Latitude>51.5</Latitude></VehicleLocation>
  </MonitoredVehicleJourney>
 </VehicleActivity>
 <VehicleActivity>
  <MonitoredVehicleJourney><VehicleRef>V10</VehicleRef></MonitoredVehicleJourney>
 </VehicleActivity>
</Siri>'''
    server.set_http_client(httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=partial_vm))))
    server._LIVE_CACHE = server.TTLCache(20.0)
    try:
        out = json.loads(asyncio.run(server.get_live_buses_on_route("OPX", "12")))
    finally:
        server.set_http_client(None)
        server._LIVE_CACHE = server.TTLCache(20.0)
    assert [(b["vehicle_id"], b["location"]) for b in out] == [
        ("V9", {"lat": 51.5, "lon": "N/A"}),
        ("V10", {"lat": "N/A", "lon": "N/A"})], out


def test_plan_journey_annotates_disrupted_legs(seeded_route, monkeypatch):
    async def fake_fetch(kind):
        return [{"recorded_at": "2026-09-12T10:00:00Z", "valid_until": None,
                 "channel": "disruptions", "severity": "severe",
                 "operators": [], "lines": ["12"], "stops": [],
                 "summary": "Road closure on Bravo Road"}]

    monkeypatch.setattr(server, "_fetch_sx", fake_fetch)
    plans = json.loads(asyncio.run(server.plan_journey(
        "Alpha Street", "Charlie Road", arrive_by="10:00", day="mon")))
    assert plans, "a plan must exist for the seeded route"
    assert all(p["disruption_alerts"] == ["route 12 has an active disruption"]
               for p in plans)


def test_plan_journey_clean_legs_untouched(seeded_route, monkeypatch):
    async def fake_fetch(kind):
        return [{"recorded_at": "2026-09-12T10:00:00Z", "valid_until": None,
                 "channel": "disruptions", "severity": "severe",
                 "operators": [], "lines": ["999"], "stops": [],
                 "summary": "Somewhere else"}]

    monkeypatch.setattr(server, "_fetch_sx", fake_fetch)
    plans = json.loads(asyncio.run(server.plan_journey(
        "Alpha Street", "Charlie Road", arrive_by="10:00", day="mon")))
    assert plans and all("disruption_alerts" not in p for p in plans)


def test_plan_journey_survives_feed_failure(seeded_route, monkeypatch):
    async def broken(kind):
        raise RuntimeError("boom")

    monkeypatch.setattr(server, "_fetch_sx", broken)
    plans = json.loads(asyncio.run(server.plan_journey(
        "Alpha Street", "Charlie Road", arrive_by="10:00", day="mon")))
    assert plans and all("disruption_alerts" not in p for p in plans)


def test_plan_journey_check_disruptions_false_skips_fetch(seeded_route, monkeypatch):
    called = []

    async def spy(kind):
        called.append(kind)
        return []

    monkeypatch.setattr(server, "_fetch_sx", spy)
    asyncio.run(server.plan_journey("Alpha Street", "Charlie Road",
                                    arrive_by="10:00", day="mon",
                                    check_disruptions=False))
    assert called == []


def test_plan_journey_annotation_does_not_reduce_capped_output(
        seeded_route, writer, monkeypatch):
    # 16 extra Monday journeys on route 12 push the planner's plan count past
    # plan_journey's 15-plan output cap (seeded_route already contributes J1
    # and J2), so the cap is genuinely exercised rather than merely present.
    # The cap predates the annotation feature (R28): the pre-release tool
    # returned deduped[:15] too, so annotation must not reduce the set below
    # what a check_disruptions=False call returns — it adds alerts, nothing
    # else.
    for i in range(16):
        dep = 10 * 60 + i * 30  # 10:00 onwards, every 30 minutes
        writer.add_journey("OPX", "12", "outbound", f"X{i}", {"mon"}, [
            {"naptan": "010A", "arrival": None,
             "departure": f"{dep // 60:02d}:{dep % 60:02d}:00"},
            {"naptan": "010B", "arrival": f"{(dep + 10) // 60:02d}:{(dep + 10) % 60:02d}:00",
             "departure": f"{(dep + 11) // 60:02d}:{(dep + 11) % 60:02d}:00"},
            {"naptan": "010C", "arrival": f"{(dep + 20) // 60:02d}:{(dep + 20) % 60:02d}:00",
             "departure": None}], 1)
    writer.commit()

    calls = []

    async def fake_fetch(kind):
        calls.append(kind)
        return [{"recorded_at": "2026-09-12T10:00:00Z", "valid_until": None,
                 "channel": "disruptions", "severity": "severe",
                 "operators": [], "lines": ["12"], "stops": [],
                 "summary": "Road closure on Bravo Road"}]

    monkeypatch.setattr(server, "_fetch_sx", fake_fetch)

    # check_disruptions=False makes no fetch, so this call measures the capped
    # output the annotated call must match exactly.
    off = json.loads(asyncio.run(server.plan_journey(
        "Alpha Street", "Charlie Road", arrive_by="23:00", day="mon",
        check_disruptions=False)))
    assert calls == [], "the opt-out path must not fetch the feed"
    assert len(off) == 15, f"the pre-existing 15-plan output cap must hold: {len(off)}"

    on = json.loads(asyncio.run(server.plan_journey(
        "Alpha Street", "Charlie Road", arrive_by="23:00", day="mon")))
    assert calls == ["disruptions"]
    assert len(on) == len(off) == 15, \
        f"annotation changed the returned plan count: {len(on)} of {len(off)}"
    assert [{k: v for k, v in p.items() if k != "disruption_alerts"} for p in on] == off, \
        "the annotated call must return exactly the opt-out call's plans"
    assert all(p["disruption_alerts"] == ["route 12 has an active disruption"]
               for p in on)


def test_plan_journey_opt_out_has_no_alerts_without_any_fetch(seeded_route):
    # Control for the cache-contamination test below: with no SX fetch ever
    # made, the opt-out path produces no alerts at all — so any alert it does
    # return after an annotated call must have reached it through the plan
    # cache rather than through the parser or the fixture.
    off = json.loads(asyncio.run(server.plan_journey(
        "Alpha Street", "Charlie Road", "10:00", day="mon",
        check_disruptions=False)))
    assert off and all("disruption_alerts" not in p for p in off)


def test_plan_journey_opt_out_uncontaminated_by_cache(seeded_route):
    # plan_direct/plan_one_change hand out the lru_cache's own plan dicts, so
    # annotating them in place would make a later check_disruptions=False call
    # (same stops/day/time -> same cache key) return alerts it never asked for.
    _install(lambda request: httpx.Response(200, content=SIRI_SX))
    try:
        # Negative control, same store and same cache key: this call runs
        # before anything has been annotated.
        control = json.loads(asyncio.run(server.plan_journey(
            "Alpha Street", "Charlie Road", "10:00", day="mon",
            check_disruptions=False)))
        assert control and all("disruption_alerts" not in p for p in control)

        on = json.loads(asyncio.run(server.plan_journey(
            "Alpha Street", "Charlie Road", "10:00", day="mon")))
        assert on and all(p["disruption_alerts"] == ["route 12 has an active disruption"]
                          for p in on)

        off = json.loads(asyncio.run(server.plan_journey(
            "Alpha Street", "Charlie Road", "10:00", day="mon",
            check_disruptions=False)))
    finally:
        _reset()
    contaminated = [p for p in off if "disruption_alerts" in p]
    assert not contaminated, (
        f"CACHE CONTAMINATION: {len(contaminated)} of {len(off)} plans returned "
        "alerts to a caller that asked NOT to check disruptions")


def test_plan_journey_survives_message_missing_refs(seeded_route, monkeypatch):
    async def feed_without_refs(kind):
        return [{"recorded_at": "2026-09-12T10:00:00Z", "summary": "no refs"}]

    monkeypatch.setattr(server, "_fetch_sx", feed_without_refs)
    plans = json.loads(asyncio.run(server.plan_journey(
        "Alpha Street", "Charlie Road", arrive_by="10:00", day="mon")))
    assert plans and all("disruption_alerts" not in p for p in plans)


def test_plan_journey_survives_message_null_refs(seeded_route, monkeypatch):
    async def feed_with_null_refs(kind):
        return [{"recorded_at": "2026-09-12T10:00:00Z", "lines": None,
                 "operators": None, "summary": "null refs"}]

    monkeypatch.setattr(server, "_fetch_sx", feed_with_null_refs)
    plans = json.loads(asyncio.run(server.plan_journey(
        "Alpha Street", "Charlie Road", arrive_by="10:00", day="mon")))
    assert plans and all("disruption_alerts" not in p for p in plans)


# Profile-shaped fares document (BODS NeTEx Fares Profile v0.4, section 6.3.3
# and section 7.2): the price lives in a PriceGroup that the
# DistanceMatrixElement references by PriceGroupRef, so the zone refs are
# reachable only by resolving that reference.
FARES_NETEX = b'''<?xml version="1.0"?>
<PublicationDelivery xmlns="http://www.netex.org.uk/netex">
 <dataObjects><CompositeFrame><FaresFrame>
  <FareFrame version="1.0" id="FF-PRICE">
   <priceGroups>
    <PriceGroup version="1.0" id="price_band_1.20">
     <members>
      <GeographicalIntervalPrice version="1.0" id="price_band_1.20@adult">
       <Amount>1.20</Amount>
      </GeographicalIntervalPrice>
     </members>
    </PriceGroup>
   </priceGroups>
  </FareFrame>
  <FareFrame version="1.0" id="FF-PRODUCT">
   <fareStructureElements>
    <FareStructureElement version="1.0" id="Tariff@single">
     <distanceMatrixElements>
      <DistanceMatrixElement version="1.0" id="Z1+Z2">
       <priceGroups>
        <PriceGroupRef version="1.0" ref="price_band_1.20"/>
       </priceGroups>
       <StartTariffZoneRef version="1.0" ref="Z1"/>
       <EndTariffZoneRef version="1.0" ref="Z2"/>
      </DistanceMatrixElement>
     </distanceMatrixElements>
    </FareStructureElement>
   </fareStructureElements>
  </FareFrame>
 </FaresFrame></CompositeFrame></dataObjects>
</PublicationDelivery>'''

# A well-formed NeTEx document carrying no price element at all.
FARES_NETEX_EMPTY = b'''<?xml version="1.0"?>
<PublicationDelivery xmlns="http://www.netex.org.uk/netex">
 <dataObjects><CompositeFrame><FaresFrame>
  <FareFrame version="1.0" id="FF-PRICE">
   <priceGroups/>
  </FareFrame>
 </FaresFrame></CompositeFrame></dataObjects>
</PublicationDelivery>'''


def test_get_fare_prices_tool():
    def handler(request):
        url = str(request.url)
        if "/api/v1/fares/dataset/DS1/" in url:
            return httpx.Response(200, json={"url": "https://example.test/dl"})
        if url.startswith("https://example.test/dl"):
            return httpx.Response(200, content=FARES_NETEX)
        # Do not interpolate the URL: it carries ?api_key=... and pytest
        # prints the message, which would leak the credential.
        raise AssertionError("unexpected URL fetched")

    _install(handler)
    try:
        prices = json.loads(asyncio.run(server.get_fare_prices("DS1")))
        assert prices[0]["amount"] == "1.20"
        assert prices[0]["start_zones"] == ["Z1"]
        filtered = json.loads(asyncio.run(
            server.get_fare_prices("DS1", origin_zone="Z1")))
        assert filtered[0]["amount"] == "1.20"
        assert asyncio.run(server.get_fare_prices("DS1", origin_zone="Z9")) == \
            "No fare prices match the given zones (1 prices extracted)."
    finally:
        _reset()


def test_get_fare_prices_no_download_url():
    _install(lambda request: httpx.Response(200, json={}))
    try:
        out = asyncio.run(server.get_fare_prices("DS2"))
        assert "no download URL" in out
    finally:
        _reset()


def test_get_fare_prices_distinguishes_no_extraction_from_no_match():
    """Nothing extracted must not be reported as a zone-filter miss: with no
    filter passed, naming a zone filter is a lie about what happened."""
    def handler(request):
        url = str(request.url)
        if "/api/v1/fares/dataset/DS3/" in url:
            return httpx.Response(200, json={"url": "https://example.test/empty"})
        if url.startswith("https://example.test/empty"):
            return httpx.Response(200, content=FARES_NETEX_EMPTY)
        raise AssertionError("unexpected URL fetched")

    _install(handler)
    try:
        out = asyncio.run(server.get_fare_prices("DS3"))
        assert "match the given zones" not in out, (
            "named a zone filter the caller never passed")
        assert "no fare prices could be extracted" in out.lower()
        # ...and the same is true when a filter *was* passed: the filter is
        # still not the reason nothing came back.
        filtered = asyncio.run(server.get_fare_prices("DS3", origin_zone="Z1"))
        assert "match the given zones" not in filtered
    finally:
        _reset()
