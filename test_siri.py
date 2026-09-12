# test_siri.py
import openbusdata_mcp.siri as siri

VM = b'''<?xml version="1.0"?>
<Siri xmlns="http://www.siri.org.uk/siri">
 <VehicleActivity>
  <MonitoredVehicleJourney>
   <VehicleRef>V1</VehicleRef>
   <DirectionRef>inbound</DirectionRef>
   <OriginName>Alpha Street</OriginName>
   <DestinationName>Bravo Road</DestinationName>
   <VehicleLocation><Latitude>51.5</Latitude><Longitude>-0.1</Longitude></VehicleLocation>
   <Bearing>90</Bearing>
  </MonitoredVehicleJourney>
 </VehicleActivity>
 <VehicleActivity>
  <MonitoredVehicleJourney>
   <VehicleRef>V2</VehicleRef>
  </MonitoredVehicleJourney>
 </VehicleActivity>
</Siri>'''


def test_parse_siri_vm_full_fields():
    buses = siri.parse_siri_vm(VM)
    assert buses[0] == {"vehicle_id": "V1", "direction": "inbound",
                        "origin": "Alpha Street", "destination": "Bravo Road",
                        "location": {"lat": 51.5, "lon": -0.1}, "bearing": "90"}


def test_parse_siri_vm_missing_location_is_none():
    buses = siri.parse_siri_vm(VM)
    assert buses[1]["vehicle_id"] == "V2"
    assert buses[1]["location"] == {"lat": None, "lon": None}


def test_parse_siri_vm_empty_feed():
    assert siri.parse_siri_vm(b'<Siri xmlns="http://www.siri.org.uk/siri"/>') == []


# V1 is well-formed; V2's latitude is not a number and its longitude is
# whitespace only. One malformed coordinate must cost that one vehicle its
# coordinate (rendered "N/A" downstream), never the whole feed.
VM_BAD_COORD = b'''<?xml version="1.0"?>
<Siri xmlns="http://www.siri.org.uk/siri">
 <VehicleActivity>
  <MonitoredVehicleJourney>
   <VehicleRef>V1</VehicleRef>
   <VehicleLocation><Latitude>51.5</Latitude><Longitude>-0.1</Longitude></VehicleLocation>
  </MonitoredVehicleJourney>
 </VehicleActivity>
 <VehicleActivity>
  <MonitoredVehicleJourney>
   <VehicleRef>V2</VehicleRef>
   <VehicleLocation><Latitude>not-a-number</Latitude><Longitude> </Longitude></VehicleLocation>
  </MonitoredVehicleJourney>
 </VehicleActivity>
</Siri>'''


def test_parse_siri_vm_unparseable_coordinate_degrades():
    buses = siri.parse_siri_vm(VM_BAD_COORD)
    assert len(buses) == 2, "one bad coordinate must not fail the whole feed"
    assert buses[0]["location"] == {"lat": 51.5, "lon": -0.1}
    assert buses[1]["vehicle_id"] == "V2"
    assert buses[1]["location"] == {"lat": None, "lon": None}


SX = b'''<?xml version="1.0"?>
<Siri xmlns="http://www.siri.org.uk/siri">
 <ServiceDelivery>
  <InfoMessageDelivery>
   <InfoMessage>
    <RecordedAtTime>2026-09-12T10:00:00Z</RecordedAtTime>
    <InfoChannelRef>disruptions</InfoChannelRef>
    <ValidUntilTime>2026-09-12T18:00:00Z</ValidUntilTime>
    <Content>
      <OperatorRef>OPX</OperatorRef>
      <LineRef>12</LineRef>
      <LineRef>15</LineRef>
      <StopPointRef>010A</StopPointRef>
      <Severity>severe</Severity>
      <Summary>Road closure on Bravo Road</Summary>
    </Content>
   </InfoMessage>
   <InfoMessage>
    <RecordedAtTime>2026-09-12T10:05:00Z</RecordedAtTime>
    <ValidUntilTime>2026-09-12T18:00:00Z</ValidUntilTime>
    <Content><LineRef>99</LineRef><Summary>Diversion</Summary></Content>
   </InfoMessage>
  </InfoMessageDelivery>
 </ServiceDelivery>
</Siri>'''


def test_parse_siri_sx_records():
    msgs = siri.parse_siri_sx(SX)
    assert len(msgs) == 2
    assert msgs[0] == {
        "recorded_at": "2026-09-12T10:00:00Z",
        "valid_until": "2026-09-12T18:00:00Z",
        "channel": "disruptions", "severity": "severe",
        "operators": ["OPX"], "lines": ["12", "15"], "stops": ["010A"],
        "summary": "Road closure on Bravo Road", "description": None}
    assert msgs[1]["lines"] == ["99"]
    assert msgs[1]["summary"] == "Diversion"


# Scalar fields nested one level below the InfoMessage and padded with
# pretty-print whitespace — the shape a real (unprobed) feed may publish.
NESTED_PADDED_SX = b'''<?xml version="1.0"?>
<Siri xmlns="http://www.siri.org.uk/siri">
 <ServiceDelivery>
  <InfoMessageDelivery>
   <InfoMessage>
    <Content>
      <RecordedAtTime>
        2026-09-12T10:00:00Z
      </RecordedAtTime>
      <ValidUntilTime> 2026-09-12T18:00:00Z </ValidUntilTime>
      <InfoChannelRef> disruptions </InfoChannelRef>
      <OperatorRef> OPX </OperatorRef>
      <LineRef> 12 </LineRef>
      <Severity> severe </Severity>
      <Summary>Road closure on Bravo Road</Summary>
      <Description>
        Road closure on Bravo Road
      </Description>
    </Content>
   </InfoMessage>
  </InfoMessageDelivery>
 </ServiceDelivery>
</Siri>'''


def test_parse_siri_sx_descendant_search_strips_scalars():
    msg = siri.parse_siri_sx(NESTED_PADDED_SX)[0]
    assert msg["recorded_at"] == "2026-09-12T10:00:00Z"
    assert msg["valid_until"] == "2026-09-12T18:00:00Z"
    assert msg["channel"] == "disruptions"
    assert msg["severity"] == "severe"
    assert msg["operators"] == ["OPX"]
    assert msg["lines"] == ["12"]
    assert msg["summary"] == "Road closure on Bravo Road"
    assert msg["description"] == "Road closure on Bravo Road"


NESTED_SX = b'''<?xml version="1.0"?>
<Siri xmlns="http://www.siri.org.uk/siri">
 <ServiceDelivery>
  <InfoMessageDelivery>
   <InfoMessage>
    <InfoChannelRef>disruptions</InfoChannelRef>
    <Content><Siri><ServiceDelivery><InfoMessageDelivery>
      <InfoMessage><RecordedAtTime>2026-09-12T11:00:00Z</RecordedAtTime>
        <LineRef>77</LineRef><Summary>Diversion</Summary></InfoMessage>
    </InfoMessageDelivery></ServiceDelivery></Siri></Content>
   </InfoMessage>
  </InfoMessageDelivery>
 </ServiceDelivery>
</Siri>'''


def test_parse_siri_sx_skips_container_messages():
    msgs = siri.parse_siri_sx(NESTED_SX)
    assert len(msgs) == 1, "the wrapping InfoMessage must not be reported twice"
    assert msgs[0]["lines"] == ["77"]


CXL = b'''<?xml version="1.0"?>
<Siri xmlns="http://www.siri.org.uk/siri">
 <EstimatedVehicleJourneyCancellations>
  <RecordedAtTime>2026-09-12T10:00:00Z</RecordedAtTime>
  <OperatorRef>OPX</OperatorRef>
  <LineRef>12</LineRef>
  <OriginRef>010A</OriginRef>
  <DestinationRef>010C</DestinationRef>
  <FramedVehicleJourneyRef><VehicleJourneyRef>J-42</VehicleJourneyRef></FramedVehicleJourneyRef>
  <CancellationReason>breakdown</CancellationReason>
 </EstimatedVehicleJourneyCancellations>
</Siri>'''


def test_parse_cancellations():
    assert siri.parse_cancellations(CXL) == [{
        "recorded_at": "2026-09-12T10:00:00Z",
        "vehicle_journey_ref": "J-42", "operator": "OPX", "line": "12",
        "origin": "010A", "destination": "010C", "reason": "breakdown"}]


# Same fields one level below the matching element and padded — the shape a
# real (unprobed) feed may publish.
NESTED_PADDED_CXL = b'''<?xml version="1.0"?>
<Siri xmlns="http://www.siri.org.uk/siri">
 <EstimatedVehicleJourneyCancellations>
  <EstimatedVehicleJourney>
   <RecordedAtTime> 2026-09-12T10:00:00Z </RecordedAtTime>
   <OperatorRef> OPX </OperatorRef>
   <LineRef> 12 </LineRef>
   <OriginRef> 010A </OriginRef>
   <DestinationRef> 010C </DestinationRef>
   <FramedVehicleJourneyRef><VehicleJourneyRef> J-42 </VehicleJourneyRef></FramedVehicleJourneyRef>
   <CancellationReason> breakdown </CancellationReason>
  </EstimatedVehicleJourney>
 </EstimatedVehicleJourneyCancellations>
</Siri>'''


def test_parse_cancellations_descendant_search_strips_scalars():
    assert siri.parse_cancellations(NESTED_PADDED_CXL) == [{
        "recorded_at": "2026-09-12T10:00:00Z",
        "vehicle_journey_ref": "J-42", "operator": "OPX", "line": "12",
        "origin": "010A", "destination": "010C", "reason": "breakdown"}]
