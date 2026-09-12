# test_live_tools.py — tool-level tests for the new live/coverage tools.
import asyncio
import json

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
