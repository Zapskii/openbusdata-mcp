"""SIRI (SIRI-VM / SIRI-SX) parsing helpers shared by the live-data tools.

All lookups are namespace-qualified against http://www.siri.org.uk/siri and
use descendant search where noted so the parsers tolerate BODS's nesting.

Recorded live structures (update the probes when the live steps run):
  /api/v1/datafeed/ (SIRI-VM): Siri > VehicleActivity >
    MonitoredVehicleJourney > VehicleRef, DirectionRef, OriginName,
    DestinationName, VehicleLocation > Latitude/Longitude, Bearing.
  /api/v1/siri-sx/ (SIRI-SX disruptions): live probe pending — no
    OPENBUS_API_KEY was available in the environment when this was written,
    so the element paths below are the synthetic-fixture assumptions, not a
    recorded live structure. Refresh this note the first time the probe runs.
  /api/v1/siri-sx/cancellations/ (SIRI-SX cancellations): live probe
    pending (same reason as above).

Element paths are kept at the top of each parser so a live-structure change
is a one-place fix. All parsing goes through defusedxml (safe against
entity-expansion / billion-laughs in remote documents).
"""

from typing import Optional

from defusedxml import ElementTree as SafeET

SIRI_NS = "http://www.siri.org.uk/siri"


def _text(el, tag: str, default=None):
    """Direct-child text lookup, namespace-qualified. Surrounding whitespace
    is stripped, so pretty-printed feeds compare equal to their values."""
    found = el.find(f"{{{SIRI_NS}}}{tag}")
    if found is not None and found.text is not None:
        return found.text.strip()
    return default


def _refs(el, tag: str) -> list:
    """All descendant texts of a ref tag, deduplicated and sorted."""
    return sorted({n.text.strip() for n in el.iter(f"{{{SIRI_NS}}}{tag}")
                   if n.text and n.text.strip()})


def _desc_text(el, tag: str, default=None):
    """Descendant-search text lookup (first match), namespace-qualified and
    whitespace-stripped.

    BODS nests fields at varying depth (e.g. <Severity> inside <Content>),
    so the SIRI-SX parsers read every scalar this way; a descendant search
    also finds direct children, so direct-child elements still resolve."""
    found = el.find(f".//{{{SIRI_NS}}}{tag}")
    if found is not None and found.text is not None:
        return found.text.strip()
    return default


def _coord(el) -> Optional[float]:
    """A coordinate element's numeric value, or None when it is absent,
    empty, whitespace-only or not a number.

    One vehicle with a malformed <Latitude> must not fail the whole feed: the
    tool renders a None coordinate as "N/A" and every other vehicle still
    arrives. (The pre-release tool passed the raw text through and rendered
    a missing coordinate the same way.)"""
    if el is None or not el.text:
        return None
    try:
        return float(el.text)
    except ValueError:
        return None


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
            lat = _coord(loc.find(f"{{{SIRI_NS}}}Latitude"))
            lon = _coord(loc.find(f"{{{SIRI_NS}}}Longitude"))
        buses.append({
            "vehicle_id": _text(mvj, "VehicleRef", "N/A"),
            "direction": _text(mvj, "DirectionRef", "N/A"),
            "origin": _text(mvj, "OriginName", "N/A"),
            "destination": _text(mvj, "DestinationName", "N/A"),
            "location": {"lat": lat, "lon": lon},
            "bearing": _text(mvj, "Bearing", "N/A"),
        })
    return buses


# --- SIRI-SX (disruptions & cancellations) --------------------------------

def parse_siri_sx(content: bytes) -> list:
    """Flatten a SIRI-SX document into one dict per InfoMessage:
    recorded_at, valid_until, channel, severity, operators, lines, stops,
    summary, description. Every scalar uses descendant search (stripped) so
    the parser tolerates BODS's nesting; a container InfoMessage that wraps
    a nested one is skipped so its content is not reported twice."""
    root = SafeET.fromstring(content)
    messages = []
    for info in root.iter(f"{{{SIRI_NS}}}InfoMessage"):
        if info.find(f".//{{{SIRI_NS}}}InfoMessage") is not None:
            continue  # container message; the nested leaf messages follow
        summary = None
        for tag in ("Summary", "Description"):
            found = info.find(f".//{{{SIRI_NS}}}{tag}")
            if found is not None and found.text:
                summary = " ".join(found.text.split())
                break
        messages.append({
            "recorded_at": _desc_text(info, "RecordedAtTime"),
            "valid_until": _desc_text(info, "ValidUntilTime"),
            "channel": _desc_text(info, "InfoChannelRef"),
            "severity": _desc_text(info, "Severity"),
            "operators": _refs(info, "OperatorRef"),
            "lines": _refs(info, "LineRef"),
            "stops": _refs(info, "StopPointRef"),
            "summary": summary,
            "description": _desc_text(info, "Description"),
        })
    return messages


CANCELLATION_TAGS = {"VehicleJourneyCancellation",
                     "EstimatedVehicleJourneyCancellations"}


def parse_cancellations(content: bytes) -> list:
    """Flatten the /siri-sx/cancellations document into one dict per
    cancelled-vehicle entry. Every scalar uses descendant search (stripped)
    so the parser tolerates BODS's nesting. If the live probe (Step 1) shows
    the feed carries InfoMessage-shaped records instead, replace this
    traversal with parse_siri_sx and map the fields — the tool layer is
    unaffected."""
    root = SafeET.fromstring(content)
    out = []
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] not in CANCELLATION_TAGS:
            continue
        fjv = el.find(f".//{{{SIRI_NS}}}FramedVehicleJourneyRef")
        vjr = None
        if fjv is not None:
            vjr = (_desc_text(fjv, "VehicleJourneyRef")
                   or _desc_text(fjv, "DatedVehicleJourneyRef"))
        out.append({
            "recorded_at": _desc_text(el, "RecordedAtTime"),
            "vehicle_journey_ref": vjr or _desc_text(el, "VehicleJourneyRef"),
            "operator": _desc_text(el, "OperatorRef"),
            "line": _desc_text(el, "LineRef"),
            "origin": _desc_text(el, "OriginRef") or _desc_text(el, "OriginName"),
            "destination": (_desc_text(el, "DestinationRef")
                            or _desc_text(el, "DestinationName")),
            "reason": _desc_text(el, "CancellationReason") or _desc_text(el, "Reason"),
        })
    return out
