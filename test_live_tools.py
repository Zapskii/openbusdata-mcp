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


def test_get_cancellations_filters():
    def handler(request):
        assert "/siri-sx/cancellations/" in str(request.url)
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
