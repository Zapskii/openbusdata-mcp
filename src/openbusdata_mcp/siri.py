"""SIRI (SIRI-VM / SIRI-SX) parsing helpers shared by the live-data tools.

All lookups are namespace-qualified against http://www.siri.org.uk/siri and
use descendant search where noted so the parsers tolerate BODS's nesting.

Recorded live structures (update the probes when the live steps run):
  /api/v1/datafeed/ (SIRI-VM): Siri > VehicleActivity >
    MonitoredVehicleJourney > VehicleRef, DirectionRef, OriginName,
    DestinationName, VehicleLocation > Latitude/Longitude, Bearing.

Element paths are kept at the top of each parser so a live-structure change
is a one-place fix. All parsing goes through defusedxml (safe against
entity-expansion / billion-laughs in remote documents).
"""

from defusedxml import ElementTree as SafeET

SIRI_NS = "http://www.siri.org.uk/siri"


def _text(el, tag: str, default=None):
    """Direct-child text lookup, namespace-qualified."""
    found = el.find(f"{{{SIRI_NS}}}{tag}")
    if found is not None and found.text is not None:
        return found.text
    return default


def _refs(el, tag: str) -> list:
    """All descendant texts of a ref tag, deduplicated and sorted."""
    return sorted({n.text.strip() for n in el.iter(f"{{{SIRI_NS}}}{tag}")
                   if n.text and n.text.strip()})


def parse_siri_vm(content: bytes) -> list:
    """Parse a SIRI-VM datafeed document into vehicle dicts.

    Shape: {"vehicle_id", "direction", "origin", "destination",
            "location": {"lat": float|None, "lon": float|None}, "bearing"}.
    lat/lon are floats or None (callers render None as "N/A") so coordinate
    consumers (estimate_live_eta) don't re-parse strings.
    """
    root = SafeET.fromstring(content)
    buses = []
    for activity in root.iter(f"{{{SIRI_NS}}}VehicleActivity"):
        mvj = activity.find(f"{{{SIRI_NS}}}MonitoredVehicleJourney")
        if mvj is None:
            continue
        loc = mvj.find(f"{{{SIRI_NS}}}VehicleLocation")
        lat = lon = None
        if loc is not None:
            lat_el = loc.find(f"{{{SIRI_NS}}}Latitude")
            lon_el = loc.find(f"{{{SIRI_NS}}}Longitude")
            if lat_el is not None and lat_el.text:
                lat = float(lat_el.text)
            if lon_el is not None and lon_el.text:
                lon = float(lon_el.text)
        buses.append({
            "vehicle_id": _text(mvj, "VehicleRef", "N/A"),
            "direction": _text(mvj, "DirectionRef", "N/A"),
            "origin": _text(mvj, "OriginName", "N/A"),
            "destination": _text(mvj, "DestinationName", "N/A"),
            "location": {"lat": lat, "lon": lon},
            "bearing": _text(mvj, "Bearing", "N/A"),
        })
    return buses
