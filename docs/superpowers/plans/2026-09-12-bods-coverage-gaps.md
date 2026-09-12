# BODS Coverage Gaps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the six BODS feature gaps — server-side AVL filtering, SIRI-SX disruptions/cancellations parsing tools, a departures board, live ETA estimation, disruption-aware journey planning, and NeTEx fares price extraction.

**Architecture:** One new SIRI parsing module (`src/openbusdata_mcp/siri.py`) and one fares module (`src/openbusdata_mcp/fares.py`), both stdlib-only and namespace-tolerant; two new `TimetableStore` read methods riding existing indexes; five new MCP tools plus four optional filter params on `get_live_buses_on_route`. A shared raw datafeed fetch (`_fetch_siri_vm`) with the existing 20s TTL cache serves both live consumers. Feed failures degrade to readable error strings or silently-unannotated output — never crash a tool.

**Tech Stack:** Python ≥3.10, `defusedxml` (safe XML parsing of remote data — new dependency), stdlib `zipfile`/`io`/`math`, httpx (shared pooled client), FastMCP (`mcp>=1.12,<2`), pytest.

**Spec:** `docs/superpowers/specs/2026-09-12-bods-coverage-gaps-design.md`

## Global Constraints

- **One new dependency: `defusedxml`.** All XML parsing of remote data goes through `defusedxml.ElementTree.fromstring` (billion-laughs / entity-expansion safe); stdlib `zipfile`, `io`, `math` for the rest. Add `defusedxml` to `pyproject.toml`'s `dependencies` in Task 1 and commit the regenerated `uv.lock`.
- **stdio hygiene:** never `print(..., file=sys.stdout)` — MCP runs over stdio; diagnostics go to `sys.stderr` with `flush=True`.
- **Backward-compatible output:** `get_live_buses_on_route` keeps its existing dict shape (lat/lon rendered `"N/A"` when absent); `plan_journey` never drops plans — disruptions are annotations only.
- **Feed failures degrade:** every live-feed consumer returns a readable error string, or (for `plan_journey`) proceeds silently unannotated.
- **Read-only store:** no schema changes, no migrations, no new indexes — all new store queries ride existing indexes (`jst_n(naptan, dep)`, `jst_j(journey_id)`, `j_oproute`, `days_mask`).
- **Live-feed fetches:** raw SIRI-VM bytes cached on the full filter tuple in `_LIVE_CACHE` (20s TTL; empty feeds never cached — existing ruling). SIRI-SX feeds cached 60s in `_SX_CACHE`.
- **Tests:** run with `~/.local/bin/uv run --extra dev pytest -q` from the repo root (the shared `.venv` has no pytest). Suite is 68 tests today; every task leaves it green.
- **Do not edit** test counts or content inside older `docs/superpowers/plans/*` or `specs/*` files — those are point-in-time records.
- Commits end with `Co-Authored-By: Claude Code <noreply@anthropic.com>`.

---

### Task 1: `siri.py` — extract SIRI-VM parsing + server-side AVL filters

**Files:**
- Create: `src/openbusdata_mcp/siri.py`
- Modify: `src/openbusdata_mcp/server.py` (replace the inline parser in `get_live_buses_on_route`, add `_siri_vm_url` + `_fetch_siri_vm`)
- Test: `test_siri.py` (create), `test_http.py` (extend)

**Interfaces:**
- Consumes: existing `_LIVE_CACHE` (TTLCache, 20s), `get_http_client()`, `BASE_URL`, `API_KEY` in server.py.
- Produces: `siri.parse_siri_vm(content: bytes) -> list[dict]` (dict shape below); `server._siri_vm_url(filters: dict) -> str`; `server._fetch_siri_vm(filters: dict) -> bytes` (async, raises on HTTP error, caches raw bytes on the filter tuple, never caches an empty feed). `get_live_buses_on_route` gains `origin_ref`, `destination_ref`, `vehicle_ref`, `bounding_box` optional params (all `Optional[str] = None`). Task 2 extends `siri.py`; Task 4 reuses `_fetch_siri_vm` and `parse_siri_vm`.

- [ ] **Step 1: Write the failing tests**

Create `test_siri.py`:

```python
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
```

Append to `test_http.py`:

```python
def test_live_buses_passes_server_side_filters():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(200, content=b'<Siri xmlns="http://www.siri.org.uk/siri"/>')

    server.set_http_client(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    server._LIVE_CACHE = server.TTLCache(20.0)
    try:
        out = asyncio.run(server.get_live_buses_on_route(
            "OPX", "12", origin_ref="490A", bounding_box="-0.2,51.4,0.0,51.6"))
        assert "originRef=490A" in captured["url"]
        assert "boundingBox=-0.2%2C51.4%2C0.0%2C51.6" in captured["url"]
        assert "operatorRef=OPX" in captured["url"]
        assert "lineRef=12" in captured["url"]
        assert "No live buses found." in out
    finally:
        server.set_http_client(None)
        server._LIVE_CACHE = server.TTLCache(20.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `~/.local/bin/uv run --extra dev pytest -q test_siri.py test_http.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'openbusdata_mcp.siri'` and the new filter assertions fail (the current tool builds its URL inline with only operatorRef/lineRef).

- [ ] **Step 3: Create `src/openbusdata_mcp/siri.py`**

```python
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
```

- [ ] **Step 4: Add the `defusedxml` dependency, then modify `server.py` — shared fetch + filtered tool**

First the dependency: in `pyproject.toml`, add `"defusedxml",` to the `dependencies` array (keeping the existing `mcp>=1.12,<2`, `httpx`, `pyyaml` entries). The next `uv run` regenerates `uv.lock`; commit both.

Add the import next to the existing `.store` import (line ~26):

```python
from .siri import parse_siri_vm
```

Add below `_LIVE_CACHE = TTLCache(20.0)` (line ~87):

```python
_SIRI_VM_FILTER_KEYS = ("operatorRef", "lineRef", "originRef",
                        "destinationRef", "vehicleRef", "boundingBox")


def _siri_vm_url(filters: dict) -> str:
    params = {k: v for k, v in filters.items() if v}
    params["api_key"] = API_KEY
    return f"{BASE_URL}/api/v1/datafeed/?{urlencode(params, quote_via=quote)}"


async def _fetch_siri_vm(filters: dict) -> bytes:
    """Fetch raw SIRI-VM bytes for the given datafeed filters, cached on the
    full filter tuple with the 20s TTL. Empty feeds (no VehicleActivity) are
    never cached so the next poll re-requests. Raises on HTTP errors."""
    key = tuple(filters.get(k) for k in _SIRI_VM_FILTER_KEYS)
    cached = _LIVE_CACHE.get(key)
    if cached is not None:
        return cached
    resp = await get_http_client().get(_siri_vm_url(filters))
    resp.raise_for_status()
    if parse_siri_vm(resp.content):
        _LIVE_CACHE.put(key, resp.content)
    return resp.content
```

Replace the whole `get_live_buses_on_route` tool (currently at `server.py:953-998`) with:

```python
@mcp.tool()
async def get_live_buses_on_route(operator_ref: str, line_ref: str,
                                  origin_ref: Optional[str] = None,
                                  destination_ref: Optional[str] = None,
                                  vehicle_ref: Optional[str] = None,
                                  bounding_box: Optional[str] = None) -> str:
    """
    Get real-time bus locations for a specific operator and route.
    Filters are applied server-side by the BODS datafeed, shrinking payloads.

    Parameters:
      operator_ref: Operator NOC code (e.g. ARBB, SCCM, CBBH).
      line_ref: Route number (e.g. 12, MK1, 100).
      origin_ref: Optional origin stop ref filter.
      destination_ref: Optional destination stop ref filter.
      vehicle_ref: Optional single-vehicle filter.
      bounding_box: Optional filter "minLon,minLat,maxLon,maxLat" (WGS84).
    """
    filters = {"operatorRef": operator_ref, "lineRef": line_ref,
               "originRef": origin_ref, "destinationRef": destination_ref,
               "vehicleRef": vehicle_ref, "boundingBox": bounding_box}
    try:
        content = await _fetch_siri_vm(filters)
        buses = []
        for v in parse_siri_vm(content):
            lat, lon = v["location"]["lat"], v["location"]["lon"]
            buses.append({**v, "location": {"lat": "N/A" if lat is None else lat,
                                            "lon": "N/A" if lon is None else lon}})
        if buses:
            return json.dumps(buses, indent=2, ensure_ascii=False)
        return "No live buses found."
    except Exception as e:
        return f"Error: {type(e).__name__}: {str(e)}"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `~/.local/bin/uv run --extra dev pytest -q`
Expected: all pass, including the pre-existing `test_live_buses_ttl_cache_skips_second_request` (its counting assertions are unaffected — the cache now stores raw bytes under a longer key, but the request-count behavior is identical).

- [ ] **Step 6: Commit**

```bash
git add src/openbusdata_mcp/siri.py src/openbusdata_mcp/server.py test_siri.py test_http.py pyproject.toml uv.lock
git commit -m "feat: shared SIRI-VM parser and server-side AVL filters

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: SIRI-SX disruptions & cancellations parsers + tools

**Files:**
- Modify: `src/openbusdata_mcp/siri.py` (append parsers)
- Modify: `src/openbusdata_mcp/server.py` (add `_SX_CACHE`, `_fetch_sx`, two tools, extend the siri import)
- Test: `test_siri.py` (extend), `test_live_tools.py` (create)

**Interfaces:**
- Consumes: `siri._text`/`siri._refs` from Task 1; server's `TTLCache`, `get_http_client()`, `BASE_URL`, `API_KEY`.
- Produces: `siri.parse_siri_sx(content: bytes) -> list[dict]` (keys: recorded_at, valid_until, channel, severity, operators, lines, stops, summary); `siri.parse_cancellations(content: bytes) -> list[dict]` (keys: recorded_at, vehicle_journey_ref, operator, line, origin, destination, reason); `server._fetch_sx(kind: str) -> Optional[list[dict]]` (async; kind is `"disruptions"` or `"cancellations"`; `None` on failure — Task 5 consumes this). Tools `get_disruptions(operator, line, stop)` and `get_cancellations(operator, line)`.

- [ ] **Step 1: Probe the live feeds and record the structure**

Run (key from the environment; the value is never printed — only the document shape is inspected):

```bash
curl -s "https://data.bus-data.dft.gov.uk/api/v1/siri-sx/?api_key=$OPENBUS_API_KEY" | head -c 4000
curl -s "https://data.bus-data.dft.gov.uk/api/v1/siri-sx/cancellations/?api_key=$OPENBUS_API_KEY" | head -c 4000
```

Paste the real element paths into the module docstring section of `siri.py` (replace the placeholder note added in Task 1). If the real structure carries the message content under different element names than the parser below assumes, adjust the tag constants in the parser — the tests use synthetic fixtures, so only the constants and the docstring change.

If `OPENBUS_API_KEY` is not set in the environment (or the network is unreachable), record "live probe pending" in the `siri.py` docstring instead and proceed — the probe is documentation, not a gate; the synthetic-fixture tests are the correctness check.

- [ ] **Step 2: Write the failing tests**

Append to `test_siri.py`:

```python
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
        "operators": [], "lines": ["12", "15"], "stops": ["010A"],
        "summary": "Road closure on Bravo Road"}
    assert msgs[1]["lines"] == ["99"]
    assert msgs[1]["summary"] == "Diversion"


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
```

Create `test_live_tools.py`:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `~/.local/bin/uv run --extra dev pytest -q test_siri.py test_live_tools.py`
Expected: FAIL — `parse_siri_sx` / `parse_cancellations` not defined; `get_disruptions` / `get_cancellations` attributes missing on server.

- [ ] **Step 4: Append the SIRI-SX parsers to `siri.py`**

```python
# --- SIRI-SX (disruptions & cancellations) --------------------------------
# (appended to siri.py — SafeET is already imported there)

def parse_siri_sx(content: bytes) -> list:
    """Flatten a SIRI-SX document into one dict per InfoMessage:
    recorded_at, valid_until, channel, severity, operators, lines, stops,
    summary. All refs use descendant search; a container InfoMessage that
    wraps a nested one is skipped so its content is not reported twice."""
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
            "recorded_at": _text(info, "RecordedAtTime"),
            "valid_until": _text(info, "ValidUntilTime"),
            "channel": _text(info, "InfoChannelRef"),
            "severity": _text(info, "Severity"),
            "operators": _refs(info, "OperatorRef"),
            "lines": _refs(info, "LineRef"),
            "stops": _refs(info, "StopPointRef"),
            "summary": summary,
        })
    return messages


CANCELLATION_TAGS = {"VehicleJourneyCancellation",
                     "EstimatedVehicleJourneyCancellations"}


def parse_cancellations(content: bytes) -> list:
    """Flatten the /siri-sx/cancellations document into one dict per
    cancelled-vehicle entry. If the live probe (Step 1) shows the feed
    carries InfoMessage-shaped records instead, replace this traversal with
    parse_siri_sx and map the fields — the tool layer is unaffected."""
    root = SafeET.fromstring(content)
    out = []
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] not in CANCELLATION_TAGS:
            continue
        fjv = el.find(f".//{{{SIRI_NS}}}FramedVehicleJourneyRef")
        vjr = None
        if fjv is not None:
            vjr = _text(fjv, "VehicleJourneyRef") or _text(fjv, "DatedVehicleJourneyRef")
        out.append({
            "recorded_at": _text(el, "RecordedAtTime"),
            "vehicle_journey_ref": vjr or _text(el, "VehicleJourneyRef"),
            "operator": _text(el, "OperatorRef"),
            "line": _text(el, "LineRef"),
            "origin": _text(el, "OriginRef") or _text(el, "OriginName"),
            "destination": _text(el, "DestinationRef") or _text(el, "DestinationName"),
            "reason": _text(el, "CancellationReason") or _text(el, "Reason"),
        })
    return out
```

- [ ] **Step 5: Add `_SX_CACHE`, `_fetch_sx`, and the two tools to `server.py`**

Extend the siri import:

```python
from .siri import parse_siri_vm, parse_siri_sx, parse_cancellations
```

Add below `_fetch_siri_vm`:

```python
_SX_CACHE = TTLCache(60.0)  # SIRI-SX feeds refresh on their own cadence

_SX_PATHS = {"disruptions": "/api/v1/siri-sx/",
             "cancellations": "/api/v1/siri-sx/cancellations/"}


async def _fetch_sx(kind: str) -> Optional[list]:
    """Cached SIRI-SX fetch. Returns None when the feed is unreachable or
    unparseable — callers degrade (never crash) on None."""
    cached = _SX_CACHE.get(kind)
    if cached is not None:
        return cached
    url = f"{BASE_URL}{_SX_PATHS[kind]}?api_key={quote(API_KEY)}"
    try:
        resp = await get_http_client().get(url)
        resp.raise_for_status()
        parsed = (parse_siri_sx if kind == "disruptions"
                  else parse_cancellations)(resp.content)
    except Exception as e:
        print(f"[siri-sx] {kind} fetch/parse failed: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)
        return None
    _SX_CACHE.put(kind, parsed)
    return parsed


@mcp.tool()
async def get_disruptions(operator: Optional[str] = None, line: Optional[str] = None,
                          stop: Optional[str] = None) -> str:
    """
    Get active SIRI-SX disruption messages as structured JSON, optionally
    filtered (client-side — the endpoint itself accepts no query parameters).

    Parameters:
      operator: Optional operator NOC code to filter by (substring of the
                message's operator refs).
      line: Optional line/route number to filter by.
      stop: Optional NaPTAN stop ref to filter by.
    """
    messages = await _fetch_sx("disruptions")
    if messages is None:
        return "Disruptions feed unavailable (fetch or parse failed)."

    def keep(m):
        if operator and operator not in m["operators"]:
            return False
        if line and line not in m["lines"]:
            return False
        if stop and stop not in m["stops"]:
            return False
        return True

    filtered = [m for m in messages if keep(m)]
    if not filtered:
        return "No matching disruption messages."
    return json.dumps(filtered, indent=2, ensure_ascii=False)


@mcp.tool()
async def get_cancellations(operator: Optional[str] = None,
                            line: Optional[str] = None) -> str:
    """
    Get published operator cancellations (SIRI-SX /cancellations) as
    structured JSON. Filtering is client-side and exact-match.

    Parameters:
      operator: Optional operator NOC code (exact match).
      line: Optional line/route number (exact match).
    """
    entries = await _fetch_sx("cancellations")
    if entries is None:
        return "Cancellations feed unavailable (fetch or parse failed)."
    filtered = [e for e in entries
                if (not operator or e["operator"] == operator)
                and (not line or e["line"] == line)]
    if not filtered:
        return "No matching cancellation entries."
    return json.dumps(filtered, indent=2, ensure_ascii=False)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `~/.local/bin/uv run --extra dev pytest -q`
Expected: all pass (75+ tests).

- [ ] **Step 7: Commit**

```bash
git add src/openbusdata_mcp/siri.py src/openbusdata_mcp/server.py test_siri.py test_live_tools.py
git commit -m "feat: SIRI-SX disruptions and cancellations tools

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 3: Departures board (store method + tool)

**Files:**
- Modify: `src/openbusdata_mcp/store.py` (add `TimetableStore.next_departures` — place it right after `_plan_one_change_cached`, before `dataset_meta` at store.py:398)
- Modify: `conftest.py` (repo root — add the shared `seeded_route` fixture)
- Modify: `src/openbusdata_mcp/server.py` (add the `get_departures_board` tool after `get_live_buses_on_route`)
- Test: `test_relational.py` (extend), `test_live_tools.py` (extend)

**Interfaces:**
- Consumes: `DAY_BITS`, the `journey_stop_times` table (index `jst_n(naptan, dep)`, `jst_j(journey_id)`), `journeys.days_mask`, `stops.name`.
- Produces: `TimetableStore.next_departures(naptans: set, day: str, from_time: str, limit: int = 20) -> list[dict]` (keys: operator, route, direction, journey_code, depart, destination); tool `get_departures_board(stop, day=None, from_time=None, limit=20)`; conftest fixture `seeded_route` (route `OPX|12` outbound over `010A Alpha Street` → `010B Bravo Road` → `010C Charlie Road`, two Monday journeys J1 09:00→09:20 and J2 09:30→09:50 — reused by Tasks 3, 4, 5).

- [ ] **Step 1: Add the shared seeding fixture to `conftest.py`**

Append at the end of the repo-root `conftest.py` (after the existing fixtures):

```python
@pytest.fixture()
def seeded_route(writer):
    """Route OPX|12 outbound over three stops, two Monday journeys.

    J1: 010A dep 09:00, 010B arr 09:10 / dep 09:11, 010C arr 09:20.
    J2: 010A dep 09:30, 010B arr 09:40,            010C arr 09:50.
    """
    writer.add_stop("010A", "Alpha Street", 51.5, -0.1)
    writer.add_stop("010B", "Bravo Road", 51.51, -0.11)
    writer.add_stop("010C", "Charlie Road", 51.52, -0.12)
    writer.upsert_route("OPX", "12", {"outbound"}, ["010A", "010B", "010C"], 1)
    writer.add_journey("OPX", "12", "outbound", "J1", {"mon"}, [
        {"naptan": "010A", "arrival": None, "departure": "09:00:00"},
        {"naptan": "010B", "arrival": "09:10:00", "departure": "09:11:00"},
        {"naptan": "010C", "arrival": "09:20:00", "departure": None}], 1)
    writer.add_journey("OPX", "12", "outbound", "J2", {"mon"}, [
        {"naptan": "010A", "arrival": None, "departure": "09:30:00"},
        {"naptan": "010B", "arrival": "09:40:00", "departure": None},
        {"naptan": "010C", "arrival": "09:50:00", "departure": None}], 1)
    writer.commit()
```

- [ ] **Step 2: Write the failing tests**

Append to `test_relational.py`:

```python
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
```

Append to `test_live_tools.py`:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `~/.local/bin/uv run --extra dev pytest -q test_relational.py test_live_tools.py`
Expected: FAIL — `next_departures` attribute missing; `get_departures_board` missing.

- [ ] **Step 4: Implement `TimetableStore.next_departures`**

In `store.py`, after `_plan_one_change_cached`:

```python
def next_departures(self, naptans: set, day: str, from_time: str,
                    limit: int = 20) -> list:
    """Next scheduled departures from any of the given stops on `day`,
    at/after from_time ('HH:MM:SS'), ordered by time. Destination is the
    name of each journey's final stop. Rides jst_n (naptan, dep) for the
    candidate scan and jst_j (journey_id) for the correlated MAX(seq)."""
    if not naptans:
        return []
    marks = ",".join("?" * len(naptans))
    rows = self.conn.execute(f"""
        SELECT j.op, j.route, j.direction, j.code,
               MIN(jst.dep) AS dep, s.name
        FROM journey_stop_times jst
        JOIN journeys j ON j.id = jst.journey_id AND j.days_mask & ? != 0
        JOIN journey_stop_times lastj ON lastj.journey_id = jst.journey_id
             AND lastj.seq = (SELECT MAX(seq) FROM journey_stop_times
                              WHERE journey_id = jst.journey_id)
        JOIN stops s ON s.naptan = lastj.naptan
        WHERE jst.naptan IN ({marks}) AND jst.dep IS NOT NULL AND jst.dep >= ?
        GROUP BY jst.journey_id
        ORDER BY dep
        LIMIT ?
    """, [DAY_BITS.get(day, 0)] + list(naptans) + [from_time, limit]).fetchall()
    return [{"operator": r[0], "route": r[1], "direction": r[2],
             "journey_code": r[3], "depart": r[4], "destination": r[5]}
            for r in rows]
```

- [ ] **Step 5: Implement the `get_departures_board` tool**

In `server.py`, after `get_live_buses_on_route`:

```python
@mcp.tool()
async def get_departures_board(stop: str, day: Optional[str] = None,
                               from_time: Optional[str] = None,
                               limit: int = 20) -> str:
    """
    Get the next scheduled departures at a stop (a departures board).

    Parameters:
      stop: Stop (NaPTAN code or name).
      day: Optional day filter: mon, tue, wed, thu, fri, sat, sun. Defaults to today.
      from_time: Optional start time HH:MM (24h). Defaults to now.
      limit: Max departures to return (1-50, default 20).
    """
    if not store.exists():
        return "No timetable data loaded. Please call load_timetable_index() first."
    naptans = store.resolve_stop(stop)
    if not naptans:
        return f'Could not resolve stop: "{stop}". Try search_stops().'
    if day is None:
        day = datetime.now().strftime("%a").lower()
    day = day.lower()[:3]
    if from_time is None:
        from_s = datetime.now().strftime("%H:%M:%S")
    else:
        from_s = _parse_time(from_time)
        if from_s is None:
            return f'Invalid time format: "{from_time}". Use HH:MM (24h).'
    limit = max(1, min(int(limit), 50))
    board = store.next_departures(naptans, day, from_s, limit)
    if not board:
        return f"No departures found at '{stop}' on {day} after {from_s}."
    return json.dumps(board, indent=2, ensure_ascii=False)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `~/.local/bin/uv run --extra dev pytest -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/openbusdata_mcp/store.py src/openbusdata_mcp/server.py conftest.py test_relational.py test_live_tools.py
git commit -m "feat: departures board tool with indexed next-departures query

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: Route coords + journey profiles + live ETA tool

**Files:**
- Modify: `src/openbusdata_mcp/store.py` (add `TimetableStore.route_stop_coords` and `TimetableStore.journeys_on_route` right after `next_departures`)
- Modify: `src/openbusdata_mcp/server.py` (add `import math` at top, `_haversine_km`, `_hms_minutes`, and the `estimate_live_eta` tool after `get_departures_board`)
- Test: `test_relational.py` (extend), `test_live_tools.py` (extend)

**Interfaces:**
- Consumes: Task 1's `_fetch_siri_vm` + `parse_siri_vm`; Task 3's `seeded_route`; `routes.stops` JSON (ordered naptan list, key `f"{op}|{route}"`); `journey_stop_times` (dep = COALESCE(departure, arrival), arr = raw arrival).
- Produces: `TimetableStore.route_stop_coords(operator: str, route: str) -> Optional[list[dict]]` (None if route unknown; dicts carry `seq`, `naptan`, `name`, `lat`, `lon`); `TimetableStore.journeys_on_route(operator: str, route: str, day: str, limit: int = 300) -> list[dict]` (keys: journey_id, direction, code, stops=[{naptan, dep, arr}]); `server._haversine_km(lat1, lon1, lat2, lon2) -> Optional[float]`; `server._hms_minutes(hms: str) -> int` (hours not clamped — handles `24:xx`); tool `estimate_live_eta(stop, operator_ref, line_ref, day=None)`.

- [ ] **Step 1: Write the failing tests**

Append to `test_relational.py`:

```python
def test_route_stop_coords_ordered_with_names(seeded_route):
    coords = seeded_route.route_stop_coords("OPX", "12")
    assert [c["naptan"] for c in coords] == ["010A", "010B", "010C"]
    assert coords[0] == {"seq": 0, "naptan": "010A", "name": "Alpha Street",
                         "lat": 51.5, "lon": -0.1}


def test_route_stop_coords_unknown_route_is_none(seeded_route):
    assert seeded_route.route_stop_coords("OPX", "999") is None


def test_journeys_on_route_full_profiles(seeded_route):
    profiles = seeded_route.journeys_on_route("OPX", "12", "mon")
    assert len(profiles) == 2
    j1 = next(p for p in profiles if p["code"] == "J1")
    assert j1["direction"] == "outbound"
    assert j1["stops"] == [
        {"naptan": "010A", "dep": "09:00:00", "arr": None},
        {"naptan": "010B", "dep": "09:11:00", "arr": "09:10:00"},
        {"naptan": "010C", "dep": "09:20:00", "arr": "09:20:00"}]
```

Append to `test_live_tools.py`:

```python
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
    monkeypatch.setattr(server, "datetime", FakeLater)
    try:
        out = json.loads(asyncio.run(server.estimate_live_eta(
            "Charlie Road", "OPX", "12", day="mon")))
        v = out["vehicles"][0]
        assert v["eta"]["minutes"] == 10
        assert v["eta"]["basis"] == "schedule-offset"
    finally:
        server.set_http_client(None)


def test_estimate_live_eta_unmatched_vehicle(seeded_route, monkeypatch):
    far_vm = VM_ONE.replace(b"<Latitude>51.511</Latitude>",
                            b"<Latitude>40.0</Latitude>").replace(
        b"<Longitude>-0.111</Longitude>", b"<Longitude>-1.0</Longitude>")
    server.set_http_client(httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=far_vm))))
    monkeypatch.setattr(server, "datetime", FakeDateTime)
    try:
        out = asyncio.run(server.estimate_live_eta("Charlie Road", "OPX", "12", day="mon"))
        assert "No live buses matched" in out
    finally:
        server.set_http_client(None)


def test_estimate_live_eta_route_not_indexed(seeded_route):
    out = asyncio.run(server.estimate_live_eta("Charlie Road", "OPX", "77", day="mon"))
    assert "not in the timetable index" in out
```

Add `from datetime import datetime` to the imports at the top of `test_live_tools.py` (needed by `FakeDateTime`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `~/.local/bin/uv run --extra dev pytest -q test_relational.py test_live_tools.py`
Expected: FAIL — `route_stop_coords` / `journeys_on_route` / `estimate_live_eta` missing.

- [ ] **Step 3: Implement the store methods**

In `store.py`, after `next_departures`:

```python
def route_stop_coords(self, operator: str, route: str) -> Optional[list]:
    """The route's ordered stop list with names and coordinates (the
    routes.stops JSON joined to stops). None when the route is unknown.
    The list is direction-merged (upsert_route keeps the longest sequence)."""
    row = self.conn.execute(
        "SELECT stops FROM routes WHERE key=?", (f"{operator}|{route}",)).fetchone()
    if row is None:
        return None
    naptans = json.loads(row[0])
    if not naptans:
        return []
    marks = ",".join("?" * len(naptans))
    info = {r[0]: (r[1], r[2], r[3]) for r in self.conn.execute(
        f"SELECT naptan, name, lat, lon FROM stops WHERE naptan IN ({marks})",
        list(naptans)).fetchall()}
    return [{"seq": i, "naptan": n,
             "name": info.get(n, ("Unknown", None, None))[0],
             "lat": info.get(n, ("Unknown", None, None))[1],
             "lon": info.get(n, ("Unknown", None, None))[2]}
            for i, n in enumerate(naptans)]

def journeys_on_route(self, operator: str, route: str, day: str,
                      limit: int = 300) -> list:
    """Today's journeys for a route with their full stop-time profiles
    (dep = COALESCE(departure, arrival), arr = raw arrival)."""
    rows = self.conn.execute(
        "SELECT id, direction, code FROM journeys "
        "WHERE op=? AND route=? AND days_mask & ? != 0 LIMIT ?",
        (operator, route, DAY_BITS.get(day, 0), limit)).fetchall()
    profiles = []
    for jid, direction, code in rows:
        stops = self.conn.execute(
            "SELECT naptan, dep, arr FROM journey_stop_times "
            "WHERE journey_id=? ORDER BY seq", (jid,)).fetchall()
        profiles.append({"journey_id": jid, "direction": direction, "code": code,
                         "stops": [{"naptan": n, "dep": d, "arr": a}
                                   for n, d, a in stops]})
    return profiles
```

- [ ] **Step 4: Implement the server helpers and tool**

In `server.py`, add `import math` to the stdlib import block at the top.

Add below `_fetch_sx` (or anywhere in the hand-written tools section):

```python
def _haversine_km(lat1, lon1, lat2, lon2) -> Optional[float]:
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return None
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2)
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(a)))


def _hms_minutes(s: str) -> int:
    """'HH:MM:SS' -> minutes since service-day start. Hours are NOT clamped
    at 24 (past-midnight times publish as 24:xx+), matching the store."""
    h, m, _sec = s.split(":")
    return int(h) * 60 + int(m)
```

Add the tool after `get_departures_board`:

```python
@mcp.tool()
async def estimate_live_eta(stop: str, operator_ref: str, line_ref: str,
                            day: Optional[str] = None) -> str:
    """
    Estimate when live buses on a route will reach a stop. Each vehicle is
    matched to its nearest stop on the timetable route; the ETA is the
    schedule offset from that position (or, when the next service hasn't
    reached the vehicle's position yet, its scheduled arrival). Honest
    estimation, not prediction: it assumes vehicles run to schedule from
    their matched position. Route stop lists are direction-merged and
    past-midnight times compare as published.

    Parameters:
      stop: Target stop (NaPTAN code or name).
      operator_ref: Operator NOC code.
      line_ref: Route number.
      day: Optional day filter: mon, tue, wed, thu, fri, sat, sun. Defaults to today.
    """
    if not store.exists():
        return "No timetable data loaded. Please call load_timetable_index() first."
    naptans = store.resolve_stop(stop)
    if not naptans:
        return f'Could not resolve stop: "{stop}". Try search_stops().'
    coords = store.route_stop_coords(operator_ref, line_ref)
    if coords is None:
        return (f"Route {operator_ref}|{line_ref} is not in the timetable index. "
                f"Try get_route_stops().")
    target = next((c for c in coords if c["naptan"] in naptans), None)
    if target is None:
        return f'"{stop}" is not served by route {operator_ref}|{line_ref}.'

    filters = {"operatorRef": operator_ref, "lineRef": line_ref}
    try:
        content = await _fetch_siri_vm(filters)
        vehicles = parse_siri_vm(content)
    except Exception as e:
        return f"Error fetching live data: {type(e).__name__}: {str(e)}"

    if day is None:
        day = datetime.now().strftime("%a").lower()
    day = day.lower()[:3]
    profiles = store.journeys_on_route(operator_ref, line_ref, day)
    now_s = datetime.now().strftime("%H:%M:%S")

    results = []
    skipped = 0
    for v in vehicles:
        vlat, vlon = v["location"]["lat"], v["location"]["lon"]
        if vlat is None or vlon is None:
            continue
        nearest, dist = None, None
        for c in coords:
            d = _haversine_km(vlat, vlon, c["lat"], c["lon"])
            if d is not None and (dist is None or d < dist):
                nearest, dist = c, d
        if nearest is None or dist is None or dist > 2.0:
            skipped += 1
            continue
        cands = ([p for p in profiles
                  if v["direction"] in ("inbound", "outbound")
                  and p["direction"] == v["direction"]] or profiles)
        at_nearest = []
        for p in cands:
            for st in p["stops"]:
                if st["naptan"] == nearest["naptan"]:
                    t = st["arr"] or st["dep"]
                    if t:
                        at_nearest.append((t, p))
                    break  # first occurrence of the nearest stop in the profile
        running = [tp for tp in at_nearest if tp[0] <= now_s]
        if running:
            t_k, profile = max(running, key=lambda tp: tp[0])
            basis = "schedule-offset"
        elif at_nearest:
            t_k, profile = min(at_nearest, key=lambda tp: tp[0])
            basis = "scheduled"
        else:
            continue
        t_target = None
        for i, st in enumerate(profile["stops"]):
            if st["naptan"] == target["naptan"] and i >= nearest["seq"]:
                t_target = st["arr"] or st["dep"]
                break
        if not t_target or t_target < t_k:
            results.append({"vehicle_id": v["vehicle_id"],
                            "vehicle_at": nearest["name"],
                            "distance_km": round(dist, 2), "eta": None,
                            "note": "past the target stop or no onward scheduled time"})
            continue
        if basis == "scheduled":
            minutes = _hms_minutes(t_target) - _hms_minutes(now_s)
        else:
            minutes = _hms_minutes(t_target) - _hms_minutes(t_k)
        minutes = max(0, min(int(minutes), 180))
        results.append({"vehicle_id": v["vehicle_id"],
                        "vehicle_at": nearest["name"],
                        "distance_km": round(dist, 2),
                        "eta": {"minutes": minutes, "basis": basis,
                                "journey_code": profile["code"],
                                "scheduled_time": t_target}})
    results.sort(key=lambda r: (r["eta"] or {}).get("minutes", 10 ** 6))
    if not results:
        note = f" ({skipped} vehicles unmatched to the route)" if skipped else ""
        return (f"No live buses matched to route {operator_ref}|{line_ref} "
                f"near the target stop{note}.")
    return json.dumps({"target_stop": target["name"], "vehicles": results},
                      indent=2, ensure_ascii=False)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `~/.local/bin/uv run --extra dev pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/openbusdata_mcp/store.py src/openbusdata_mcp/server.py test_relational.py test_live_tools.py
git commit -m "feat: live ETA tool matching AVL positions to schedule profiles

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 5: Disruption-aware journey planning

**Files:**
- Modify: `src/openbusdata_mcp/server.py` (add `_annotate_disruptions` above `plan_journey`, extend `plan_journey` signature and docstring)
- Test: `test_live_tools.py` (extend)

**Interfaces:**
- Consumes: Task 2's `_fetch_sx("disruptions")` (async, `None` on failure); the plan dicts produced by `plan_direct`/`plan_one_change` (legs carry `operator`, `route`).
- Produces: `server._annotate_disruptions(plans: list[dict]) -> list[dict]` (async; adds a `disruption_alerts` list of strings to matching plans); `plan_journey(stop_a, stop_b, arrive_by, day=None, max_changes=1, check_disruptions=True)`.

- [ ] **Step 1: Write the failing tests**

Append to `test_live_tools.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `~/.local/bin/uv run --extra dev pytest -q test_live_tools.py`
Expected: FAIL — `plan_journey` has no `check_disruptions` parameter (TypeError) and no annotations appear.

- [ ] **Step 3: Implement the annotation helper and tool parameter**

In `server.py`, add directly above `plan_journey`:

```python
async def _annotate_disruptions(plans: list) -> list:
    """Attach disruption_alerts to plans whose legs' route or operator refs
    appear in the live SIRI-SX feed. Annotation only — plans are never
    dropped — and a feed failure degrades to silent, unannotated output."""
    try:
        messages = await _fetch_sx("disruptions")
    except Exception as e:
        print(f"[siri-sx] disruption annotation skipped: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)
        return plans
    if not messages:
        return plans
    line_refs, op_refs = set(), set()
    for m in messages:
        line_refs.update(m["lines"])
        op_refs.update(m["operators"])
    for p in plans:
        alerts = []
        for leg in p["legs"]:
            if leg.get("route") in line_refs:
                alerts.append(f"route {leg.get('route')} has an active disruption")
            elif leg.get("operator") in op_refs:
                alerts.append(f"operator {leg.get('operator')} has an active disruption notice")
        if alerts:
            p["disruption_alerts"] = sorted(set(alerts))
    return plans
```

Change `plan_journey`'s signature and docstring:

```python
async def plan_journey(stop_a: str, stop_b: str, arrive_by: str,
                       day: Optional[str] = None, max_changes: int = 1,
                       check_disruptions: bool = True) -> str:
```

Add to the docstring, after the `max_changes` parameter line:

```
      check_disruptions: If true (default), plans whose route or operator
                appears in the live SIRI-SX disruptions feed are annotated
                with a "disruption_alerts" list. Plans are never dropped;
                a feed failure degrades silently.
```

And change the final return block (after the sort, before serialization):

```python
    deduped.sort(key=lambda p: p["legs"][-1]["arrive"] or "")
    if check_disruptions:
        deduped = await _annotate_disruptions(deduped[:15])
    return json.dumps(deduped, indent=2, ensure_ascii=False) if deduped else f"No journey found from '{stop_a}' to '{stop_b}' by {arrive_by} on {day}."
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `~/.local/bin/uv run --extra dev pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/openbusdata_mcp/server.py test_live_tools.py
git commit -m "feat: annotate journey plans with live SIRI-SX disruptions

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 6: Fares NeTEx price extraction + `get_fare_prices` tool

**Files:**
- Create: `src/openbusdata_mcp/fares.py`
- Modify: `src/openbusdata_mcp/server.py` (fares import + tool, placed after `estimate_live_eta`)
- Test: `test_fares.py` (create), `test_live_tools.py` (extend)

**Interfaces:**
- Consumes: `BASE_URL`, `API_KEY`, `get_http_client()` in server.py. The catalogue metadata pattern mirrors the timetable loader (`GET /api/v1/dataset/{id}/` → `meta["url"]` → `GET {url}?api_key=`), here with `/api/v1/fares/dataset/{id}/`.
- Produces: `fares.extract_xml_bytes(content: bytes) -> bytes` (unzips PK-magic payloads; first `.xml` member); `fares.parse_fare_prices(content: bytes) -> list[dict]` (keys: amount, currency, start_zones, end_zones, zones, product); tool `get_fare_prices(dataset_id, origin_zone=None, destination_zone=None)`.

- [ ] **Step 1: Probe a live fares dataset and record the structure**

```bash
curl -s "https://data.bus-data.dft.gov.uk/api/v1/fares/dataset/?limit=2&api_key=$OPENBUS_API_KEY" | head -c 2000
# then take a dataset id from the result:
curl -s "https://data.bus-data.dft.gov.uk/api/v1/fares/dataset/<ID>/?api_key=$OPENBUS_API_KEY" | python3 -c "import json,sys; print(json.load(sys.stdin).get('url'))"
curl -sL "<download-url>?api_key=$OPENBUS_API_KEY" | head -c 4000
```

Confirm the metadata carries `url` and record the real NeTEx element names in `fares.py`'s docstring. If the real price/zone element names differ from the constants below, adjust the constants — the tests use synthetic fixtures. If `OPENBUS_API_KEY` is not set (or the network is unreachable), record "live probe pending" in the `fares.py` docstring and proceed — the probe is documentation, not a gate.

- [ ] **Step 2: Write the failing tests**

Create `test_fares.py`:

```python
# test_fares.py
import io
import zipfile

from openbusdata_mcp.fares import extract_xml_bytes, parse_fare_prices

NETEX = b'''<?xml version="1.0"?>
<PublicationDelivery xmlns="http://www.netex.org.uk/netex">
 <dataObjects>
  <CompositeFrame>
   <FaresFrame>
    <FareTable id="FT1">
     <Name>Single fares</Name>
     <preAssignedFareProducts>
      <PreassignedFareProduct id="P1">
       <Name>Zone 1-2 single</Name>
       <AccessRightsInProduct>
        <AccessRightInProduct>
         <PreassignedFare>
          <StartTariffZoneRef ref="Z1"/>
          <EndTariffZoneRef ref="Z2"/>
          <FarePrice currency="GBP"><Amount>250</Amount></FarePrice>
         </PreassignedFare>
         <PreassignedFare>
          <TariffZoneRef ref="Z3"/>
          <FarePrice><Amount>180</Amount></FarePrice>
         </PreassignedFare>
        </AccessRightInProduct>
       </AccessRightsInProduct>
      </PreassignedFareProduct>
     </preAssignedFareProducts>
    </FareTable>
   </FaresFrame>
  </CompositeFrame>
 </dataObjects>
</PublicationDelivery>'''


def test_parse_fare_prices_zones_and_product():
    prices = parse_fare_prices(NETEX)
    assert prices[0] == {"amount": "250", "currency": "GBP",
                         "start_zones": ["Z1"], "end_zones": ["Z2"],
                         "zones": [], "product": "Zone 1-2 single"}
    assert prices[1]["zones"] == ["Z3"]
    assert prices[1]["start_zones"] == [] and prices[1]["end_zones"] == []
    assert prices[1]["product"] == "Zone 1-2 single"
    assert prices[1]["currency"] == "GBP", "currency defaults to GBP"


def test_parse_fare_prices_skips_amountless_entries():
    bare = NETEX.replace(b"<Amount>180</Amount>", b"")
    prices = parse_fare_prices(bare)
    assert len(prices) == 1 and prices[0]["amount"] == "250"


def test_extract_xml_bytes_unzips():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("fares.xml", NETEX)
    assert parse_fare_prices(buf.getvalue())[0]["amount"] == "250"


def test_extract_xml_bytes_passthrough():
    assert extract_xml_bytes(NETEX) == NETEX
```

Append to `test_live_tools.py`:

```python
FARES_NETEX = b'''<?xml version="1.0"?>
<PublicationDelivery xmlns="http://www.netex.org.uk/netex">
 <dataObjects><CompositeFrame><FaresFrame><FareTable id="FT1">
  <Name>Single fares</Name>
  <preAssignedFareProducts><PreassignedFareProduct id="P1">
   <Name>Zone 1-2 single</Name>
   <AccessRightsInProduct><AccessRightInProduct><PreassignedFare>
    <StartTariffZoneRef ref="Z1"/><EndTariffZoneRef ref="Z2"/>
    <FarePrice currency="GBP"><Amount>250</Amount></FarePrice>
   </PreassignedFare></AccessRightInProduct></AccessRightsInProduct>
  </PreassignedFareProduct></preAssignedFareProducts>
 </FareTable></FaresFrame></CompositeFrame></dataObjects>
</PublicationDelivery>'''


def test_get_fare_prices_tool():
    def handler(request):
        url = str(request.url)
        if "/api/v1/fares/dataset/DS1/" in url:
            return httpx.Response(200, json={"url": "https://example.test/dl"})
        if url.startswith("https://example.test/dl"):
            return httpx.Response(200, content=FARES_NETEX)
        raise AssertionError(f"unexpected URL fetched: {url}")

    _install(handler)
    try:
        prices = json.loads(asyncio.run(server.get_fare_prices("DS1")))
        assert prices[0]["amount"] == "250"
        assert prices[0]["start_zones"] == ["Z1"]
        filtered = json.loads(asyncio.run(
            server.get_fare_prices("DS1", origin_zone="Z1")))
        assert filtered[0]["amount"] == "250"
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `~/.local/bin/uv run --extra dev pytest -q test_fares.py test_live_tools.py`
Expected: FAIL — `openbusdata_mcp.fares` does not exist; `get_fare_prices` missing.

- [ ] **Step 4: Create `src/openbusdata_mcp/fares.py`**

```python
"""BODS fares (NeTEx) price extraction.

BODS fares datasets are NeTEx XML documents, sometimes delivered as zip
containers (like timetables). Parsing is by local element name — BODS NeTEx
namespace URIs vary — with these constants at the top so a live-structure
change is a one-place fix (record the real names here when the probe runs):

  ...FareTable/Name, PreassignedFareProduct/Name,
  PreassignedFare > StartTariffZoneRef / EndTariffZoneRef / TariffZoneRef
  (ref attributes) > FarePrice(currency) > Amount.

Scope note: this extracts the published price points (amount, currency,
zones, enclosing product name). Mapping tariff zones onto geographic stops
is a separate dataset concern and is deliberately not attempted here.
"""

import io
import zipfile

from defusedxml import ElementTree as SafeET

PRICE_TAGS = {"FarePrice", "farePrice"}
AMOUNT_TAGS = {"Amount", "PriceAmount", "amount"}
ZONE_TAGS = {"StartTariffZoneRef", "EndTariffZoneRef", "TariffZoneRef"}
PRODUCT_TAGS = {"PreassignedFareProduct", "FareProduct", "FareTable"}


def extract_xml_bytes(content: bytes) -> bytes:
    """Unzip PK-magic payloads (first .xml member), else pass through."""
    if content[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            for name in z.namelist():
                if name.lower().endswith(".xml"):
                    return z.read(name)
        raise ValueError("zip archive contains no .xml member")
    return content


def _local(el) -> str:
    return el.tag.rsplit("}", 1)[-1]


def parse_fare_prices(content: bytes) -> list:
    """Extract published price points from a NeTEx fares document.

    For each price element: amount from an Amount/PriceAmount child (or the
    element's own amount attribute — entries without any amount are skipped);
    currency from the currency attribute, defaulting to GBP. Zone refs are
    found by walking up ancestors — the first ancestor level that carries
    any zone ref wins — and are split into start_zones / end_zones where the
    NeTEx tags distinguish them (generic TariffZoneRef lands in "zones").
    The enclosing product/table Name becomes "product".
    """
    root = SafeET.fromstring(extract_xml_bytes(content))
    parents = {c: p for p in root.iter() for c in list(p)}
    out = []
    for el in root.iter():
        if _local(el) not in PRICE_TAGS:
            continue
        amount = el.get("amount")
        if not amount:
            for child in el.iter():
                if _local(child) in AMOUNT_TAGS and child.text and child.text.strip():
                    amount = child.text.strip()
                    break
        if not amount:
            continue
        start_zones, end_zones, zones = [], [], []
        cur = parents.get(el)
        while cur is not None and not (start_zones or end_zones or zones):
            for c in list(cur):
                if _local(c) in ZONE_TAGS:
                    ref = c.get("ref") or c.get("id") or (c.text or "").strip()
                    if _local(c) == "StartTariffZoneRef":
                        start_zones.append(ref)
                    elif _local(c) == "EndTariffZoneRef":
                        end_zones.append(ref)
                    else:
                        zones.append(ref)
            cur = parents.get(cur)
        product = None
        cur = parents.get(el)
        while cur is not None:
            if _local(cur) in PRODUCT_TAGS:
                nm = next((c.text for c in list(cur)
                           if _local(c) in ("Name", "name") and c.text), None)
                product = nm.strip() if nm else _local(cur)
                break
            cur = parents.get(cur)
        out.append({"amount": amount,
                    "currency": el.get("currency") or "GBP",
                    "start_zones": start_zones, "end_zones": end_zones,
                    "zones": zones, "product": product})
    return out
```

- [ ] **Step 5: Add the `get_fare_prices` tool to `server.py`**

Add the import next to the siri import:

```python
from .fares import parse_fare_prices
```

Add the tool after `estimate_live_eta`:

```python
@mcp.tool()
async def get_fare_prices(dataset_id: str, origin_zone: Optional[str] = None,
                          destination_zone: Optional[str] = None) -> str:
    """
    Download a BODS fares dataset (NeTEx) and extract its published fare
    prices as structured JSON, optionally filtered by tariff zone. Find
    dataset ids with the fares catalogue passthrough tool
    (fares_api_v1_fares_dataset).

    Parameters:
      dataset_id: Fares dataset id from the fares catalogue.
      origin_zone: Optional start tariff zone ref to filter by.
      destination_zone: Optional end tariff zone ref to filter by.
    """
    try:
        meta = await get_http_client().get(
            f"{BASE_URL}/api/v1/fares/dataset/{quote(dataset_id)}/?api_key={API_KEY}")
        meta.raise_for_status()
        download_url = meta.json().get("url")
        if not download_url:
            return (f"Dataset {dataset_id} exposes no download URL in its "
                    f"catalogue metadata.")
        dl = await get_http_client().get(f"{download_url}?api_key={API_KEY}")
        dl.raise_for_status()
        prices = parse_fare_prices(dl.content)
    except Exception as e:
        return f"Error: {type(e).__name__}: {str(e)}"

    def keep(p):
        if origin_zone and origin_zone not in p["start_zones"] + p["zones"]:
            return False
        if destination_zone and destination_zone not in p["end_zones"] + p["zones"]:
            return False
        return True

    filtered = [p for p in prices if keep(p)]
    if not filtered:
        note = f" ({len(prices)} prices extracted)" if prices else ""
        return f"No fare prices match the given zones{note}."
    return json.dumps(filtered, indent=2, ensure_ascii=False)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `~/.local/bin/uv run --extra dev pytest -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/openbusdata_mcp/fares.py src/openbusdata_mcp/server.py test_fares.py test_live_tools.py
git commit -m "feat: NeTEx fares price extraction and get_fare_prices tool

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 7: Docs + full-suite verification

**Files:**
- Modify: `docs/RUNBOOK.md` (tool count + smoke-test list)
- Modify: `README.md` (tool list summary)

**Interfaces:**
- Consumes: everything shipped in Tasks 1–6.
- Produces: documentation matching the 22-tool surface; a final green suite run.

- [ ] **Step 1: Update `docs/RUNBOOK.md`**

Line ~172 (`- **Why SQLite:** ... Tool surface unchanged (17 tools).`): replace the trailing sentence with `Tool surface now 22 tools: the 17 passthrough/search/planning tools plus get_disruptions, get_cancellations, get_departures_board, estimate_live_eta and get_fare_prices; get_live_buses_on_route additionally accepts server-side filters (origin_ref, destination_ref, vehicle_ref, bounding_box).`

In section 6 (Verification / smoke tests), after the existing passthrough bullet, add:

```
# New coverage tools — quick checks (replace args with real refs from the index):
#   get_departures_board {stop: "<known stop name>"}           → ordered JSON board
#   get_disruptions {}                                          → JSON list or "No matching..."
#   get_cancellations {}                                        → JSON list or "No matching..."
#   estimate_live_eta {stop, operator_ref, line_ref}            → vehicles + eta or a readable note
#   get_fare_prices {dataset_id}                                → JSON prices or "No fare prices..."
```

- [ ] **Step 2: Update `README.md`**

Replace the bullet at line 10 (`- **Live API tools** — query timetables, fares, disruptions, cancellations and real-time bus locations`) with:

```
- **Live API tools** — query timetables, fares, disruptions, cancellations and real-time bus locations
- **Coverage tools** — departures board, live ETA at a stop, filtered live buses, disruption-aware journey planning, and fares price extraction
```

- [ ] **Step 3: Run the full suite**

Run: `~/.local/bin/uv run --extra dev pytest -q`
Expected: all pass (~95 tests). Record the exact count in the commit body if it differs from 95.

- [ ] **Step 4: Commit**

```bash
git add docs/RUNBOOK.md README.md
git commit -m "docs: document the new coverage tools and updated tool count

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```