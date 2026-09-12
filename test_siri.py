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
