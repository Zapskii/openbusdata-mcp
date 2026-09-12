#!/usr/bin/env python3
"""
OpenBusData MCP Server

Exposes OpenBusData Services APIs as MCP tools based on local OpenAPI YAML specs
and rich timetable data parsing for route/stop discovery, timetable search,
and multi-leg journey planning.

API key is read from the OPENBUS_API_KEY environment variable.
"""

import os
import sys
import tempfile
import yaml
import json
import zipfile
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
import math
import re
import time
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import quote, urlencode, urljoin
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from .store import TimetableStore, TimetableWriter, _parse_time
from .siri import parse_siri_vm, parse_siri_sx, parse_cancellations
from .fares import parse_fare_prices
from collections import defaultdict

import httpx
from mcp.server.fastmcp import FastMCP

# Suppress noisy httpx logs (they would break stdio MCP transport)
import logging
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_URL = os.environ.get("OPENBUS_BASE_URL", "https://data.bus-data.dft.gov.uk")
API_KEY = os.environ.get("OPENBUS_API_KEY", "")
# Resolve specs from the bundled openapi-schema directory inside the package
SPECS_DIR = Path(__file__).parent / "openapi-schema"
CACHE_DIR = Path.home() / ".cache" / "openbusdata"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Shared pooled HTTP client: one TCP/TLS connection pool for the process
# lifetime instead of a fresh client (new handshake) per tool call. Test seam:
# set_http_client() injects a client (or None to reset to lazy creation).
_http_client: Optional[httpx.AsyncClient] = None


def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    return _http_client


def set_http_client(client: Optional[httpx.AsyncClient]) -> None:
    global _http_client
    _http_client = client


class TTLCache:
    """Tiny process-local TTL cache: monotonic clock, bounded entry count."""

    def __init__(self, ttl_seconds: float):
        self.ttl = ttl_seconds
        self._store: dict = {}

    def get(self, key):
        hit = self._store.get(key)
        if hit is None:
            return None
        ts, val = hit
        if time.monotonic() - ts > self.ttl:
            del self._store[key]
            return None
        return val

    def put(self, key, val):
        if len(self._store) >= 256:
            self._store.clear()  # simple bounded reset; keys are few
        self._store[key] = (time.monotonic(), val)


_LIVE_CACHE = TTLCache(20.0)  # bus positions are re-polled within seconds

_SIRI_VM_FILTER_KEYS = ("operatorRef", "lineRef", "originRef",
                        "destinationRef", "vehicleRef", "boundingBox")

_API_KEY_PARAM_RE = re.compile(r"(api_key=)[^&\s'\"]+")


def _redact(text: str) -> str:
    """Strip the API key from text bound for tool output or a log line.

    httpx builds HTTPStatusError messages from the request URL, and every BODS
    URL carries ?api_key=..., so an unredacted failure message publishes the
    credential into tool output and the session transcript.

    Ruling R24: this strips the api_key parameter by PATTERN rather than by
    enumerating the key's encodings. The package builds that parameter three
    ways -- the fares tool interpolates API_KEY raw, SIRI-SX uses
    quote(API_KEY) (safe='/'), and SIRI-VM uses urlencode(..., quote_via=quote)
    (which passes safe='') -- so a key containing '/' appears as %2F on one
    path and '/' on another, and no fixed list of literals covers all three.
    A pattern also keeps working when the next URL builder is added, which is
    what makes this a closed class rather than a list that goes stale.

    The literal replacement is kept as well, for a key that reaches output
    outside a query string (a raw traceback, say).
    """
    if not API_KEY:
        return text
    out = text.replace(API_KEY, "***")
    return _API_KEY_PARAM_RE.sub(r"\1***", out)


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
        print(_redact(f"[siri-sx] {kind} fetch/parse failed: "
                      f"{type(e).__name__}: {e}"),
              file=sys.stderr, flush=True)
        return None
    _SX_CACHE.put(kind, parsed)
    return parsed


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


mcp = FastMCP("openbusdata")


# ---------------------------------------------------------------------------
# SIRI-SX tools (disruptions & cancellations)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Data structures for timetable parsing
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Stop:
    naptan: str
    name: str
    lat: Optional[float] = None
    lon: Optional[float] = None


@dataclass(slots=True)
class Route:
    operator: str
    route_num: str
    directions: set = field(default_factory=set)
    stops: list = field(default_factory=list)


@dataclass(slots=True)
class JourneyStop:
    naptan: str
    # Normalized 'HH:MM:SS' strings, hours NOT clamped at 24 (service-day
    # times past midnight publish as 24:00+); string ordering is the clock.
    arrival: Optional[str] = None
    departure: Optional[str] = None


@dataclass(slots=True)
class Journey:
    operator: str
    route_num: str
    direction: str
    journey_code: str
    stops: list[JourneyStop] = field(default_factory=list)
    days: set[str] = field(default_factory=set)  # mon, tue, wed, thu, fri, sat, sun
    dataset_id: int = 0  # BODS dataset this journey came from (enables surgical purge)


# All queryable state lives in the SQLite store; the in-memory index that
# predated it (and its discard_dataset merge semantics) is preserved in
# TimetableWriter.discard_dataset.
store = TimetableStore()
writer = TimetableWriter()


# ---------------------------------------------------------------------------
# TransXChange XML parsing helpers
# ---------------------------------------------------------------------------
def _get_ns(tag: str, ns: str) -> str:
    return f"{{{ns}}}{tag}" if ns else tag


def _parse_duration(text: str) -> timedelta:
    """Parse ISO 8601 duration like PT5M or PT1H30M."""
    if not text:
        return timedelta(0)
    total = timedelta(0)
    text = text.strip()
    if text.startswith("PT"):
        text = text[2:]
    # Hours
    h_match = re.search(r'(\d+)H', text)
    if h_match:
        total += timedelta(hours=int(h_match.group(1)))
    # Minutes
    m_match = re.search(r'(\d+)M', text)
    if m_match:
        total += timedelta(minutes=int(m_match.group(1)))
    # Seconds
    s_match = re.search(r'(\d+)S', text)
    if s_match:
        total += timedelta(seconds=int(s_match.group(1)))
    return total


def _fmt_seconds(total: int) -> str:
    """Render seconds since the start of the service day as 'HH:MM:SS'.

    Hours are NOT wrapped at 24: a journey departing 24:05 must stay
    '25:10:30' after 1h10m of runtime, or arrival comparisons break.
    """
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def _parse_days(op_profile) -> set[str]:
    """Extract operating days from OperatingProfile or SpecialDaysOperation."""
    days = set()
    day_map = {
        "Monday": "mon", "Tuesday": "tue", "Wednesday": "wed",
        "Thursday": "thu", "Friday": "fri", "Saturday": "sat", "Sunday": "sun",
    }
    if op_profile is None:
        return set(day_map.values())  # Assume every day if not specified

    for regular in op_profile.iter():
        tag = regular.tag.split("}")[-1] if "}" in regular.tag else regular.tag
        if tag in day_map and regular.text and regular.text.lower() in ("true", "1"):
            days.add(day_map[tag])

    if not days:
        return set(day_map.values())
    return days


def parse_transxchange(content: str, operator_name: str) -> tuple[list[Stop], list[Route], list[Journey]]:
    """Parse a single TransXChange XML string. Returns (stops, routes, journeys)."""
    try:
        root = SafeET.fromstring(content)
    except (SafeET.ParseError, DefusedXmlException):
        return [], [], []

    ns = root.tag.split("}")[0].strip("{") if "}" in root.tag else ""

    q = lambda tag: _get_ns(tag, ns)

    stops: list[Stop] = []
    routes: list[Route] = []
    journeys: list[Journey] = []

    # --- Extract StopPoints ---
    for asp in root.iter(q("AnnotatedStopPointRef")):
        ref_elem = asp.find(q("StopPointRef"))
        name_elem = asp.find(q("CommonName"))
        if ref_elem is not None:
            naptan = ref_elem.text
            name = name_elem.text if name_elem is not None else "Unknown"
            stops.append(Stop(naptan=naptan, name=name))

    # --- Extract JourneyPatternSections with timing ---
    # jps_id -> list of (from_stop, to_stop, runtime)
    jps_links: dict[str, list[tuple[str, str, timedelta]]] = {}
    for jps in root.iter(q("JourneyPatternSection")):
        jps_id = jps.get("id")
        if not jps_id:
            continue
        links = []
        for link in jps.iter(q("JourneyPatternTimingLink")):
            from_stop = link.find(q("From"))
            to_stop = link.find(q("To"))
            runtime_elem = link.find(q("RunTime"))
            from_ref = from_stop.find(q("StopPointRef")).text if from_stop is not None else None
            to_ref = to_stop.find(q("StopPointRef")).text if to_stop is not None else None
            runtime = _parse_duration(runtime_elem.text if runtime_elem is not None else "")
            if from_ref and to_ref:
                links.append((from_ref, to_ref, runtime))
        jps_links[jps_id] = links

    # --- Extract JourneyPatterns ---
    jp_map: dict[str, dict] = {}  # jp_id -> {direction, route_ref, section_ids}
    for jp in root.iter(q("JourneyPattern")):
        jp_id = jp.get("id")
        direction_elem = jp.find(q("Direction"))
        direction = direction_elem.text if direction_elem is not None else "unknown"
        section_ids = [ref.text for ref in jp.findall(q("JourneyPatternSectionRefs")) if ref.text]
        jp_map[jp_id] = {"direction": direction, "section_ids": section_ids}

    # --- Extract Routes ---
    route_num = None
    for pln in root.iter(q("PublishedLineName")):
        if pln.text:
            route_num = pln.text
            break
    if not route_num:
        for lr in root.iter(q("LineRef")):
            if lr.text:
                route_num = lr.text.split(":")[-1]
                break
    if not route_num:
        route_num = "Unknown"

    # Build route stop sequence from first journey pattern
    all_stop_seqs = []
    all_directions = set()
    for jp_id, jp_data in jp_map.items():
        seq = []
        for sid in jp_data["section_ids"]:
            if sid in jps_links:
                for from_ref, to_ref, _ in jps_links[sid]:
                    if not seq or seq[-1] != from_ref:
                        seq.append(from_ref)
                    seq.append(to_ref)
        if seq:
            all_stop_seqs.append(seq)
            all_directions.add(jp_data["direction"])

    if all_stop_seqs:
        longest = max(all_stop_seqs, key=len)
        routes.append(Route(
            operator=operator_name, route_num=route_num,
            directions=all_directions, stops=longest,
        ))

    # --- Extract VehicleJourneys with times ---
    for vj in root.iter(q("VehicleJourney")):
        jpref = vj.find(q("JourneyPatternRef"))
        if jpref is None or jpref.text not in jp_map:
            continue

        jp_data = jp_map[jpref.text]
        dep_time_elem = vj.find(q("DepartureTime"))
        dep_time = _parse_time(dep_time_elem.text if dep_time_elem is not None else None)
        if dep_time is None:
            continue

        vj_code_elem = vj.find(q("VehicleJourneyCode"))
        journey_code = vj_code_elem.text if vj_code_elem is not None else "unknown"

        # Operating profile (days)
        op_profile = vj.find(q("OperatingProfile"))
        days = _parse_days(op_profile)

        # Build stop schedule by accumulating run times as seconds since the
        # start of the service day (no 24-hour wraparound — see _fmt_seconds)
        h, m, s = (int(x) for x in dep_time.split(":"))
        current_seconds = h * 3600 + m * 60 + s
        journey_stops: list[JourneyStop] = []

        for sid in jp_data["section_ids"]:
            if sid not in jps_links:
                continue
            for i, (from_ref, to_ref, runtime) in enumerate(jps_links[sid]):
                if i == 0 and not journey_stops:
                    # First stop
                    journey_stops.append(JourneyStop(
                        naptan=from_ref, departure=_fmt_seconds(current_seconds)))
                # Travel to next stop
                current_seconds += int(runtime.total_seconds())
                journey_stops.append(JourneyStop(
                    naptan=to_ref, arrival=_fmt_seconds(current_seconds)))

        if len(journey_stops) >= 2:
            journeys.append(Journey(
                operator=operator_name, route_num=route_num,
                direction=jp_data["direction"], journey_code=journey_code,
                stops=journey_stops, days=days,
            ))

    return stops, routes, journeys


# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------
async def load_dataset(ds_id: int, client: Optional[httpx.AsyncClient] = None) -> Optional[dict]:
    """Download and parse a single timetable dataset. Returns metadata.

    No-op when the dataset is already loaded (resume semantics); updating a
    changed dataset goes through load_timetable_delta, which calls
    _load_dataset(force_reload=True) directly.
    """
    if ds_id in writer.loaded_ids():
        return {}
    return await _load_dataset(ds_id, client=client)


def _xml_contents(z: zipfile.ZipFile) -> Iterator[str]:
    """Yield each .xml member's decoded text, one at a time."""
    for name in z.namelist():
        if name.endswith(".xml"):
            yield z.read(name).decode("utf-8", errors="ignore")


async def _load_dataset(ds_id: int, force_reload: bool = False,
                        client: Optional[httpx.AsyncClient] = None) -> Optional[dict]:
    """Returns meta on success, None on failure (caller counts errors).

    Reuses `client` if given; otherwise owns and closes a private one.
    The zip is streamed to a seekable temp file (never held in RAM whole).
    """
    owns = client is None
    if owns:
        client = httpx.AsyncClient(timeout=120.0, follow_redirects=True)
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".zip")
    os.close(tmp_fd)  # we only need the path; open it normally below
    z = None
    try:
        meta_resp = await client.get(f"{BASE_URL}/api/v1/dataset/{ds_id}/?api_key={API_KEY}")
        if meta_resp.status_code != 200:
            return None
        meta = meta_resp.json()

        operator = meta.get("operatorName", "Unknown")
        download_url = meta.get("url")
        if not download_url:
            return meta  # not a timetable dataset; nothing to load

        size = 0
        async with client.stream("GET", f"{download_url}?api_key={API_KEY}") as resp:
            if resp.status_code != 200:
                return None
            with open(tmp_path, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
                    size += len(chunk)
        if size < 100:
            return None

        try:
            z = zipfile.ZipFile(tmp_path)
            contents = _xml_contents(z)
        except zipfile.BadZipFile:
            # Some BODS datasets publish a bare TransXChange XML document
            # instead of a zip container.
            with open(tmp_path, "rb") as f:
                head = f.read(200)
            if head.lstrip().startswith(b"<?xml") or b"<TransXChange" in head:
                with open(tmp_path, "rb") as f:
                    contents = iter([f.read().decode("utf-8", errors="ignore")])
            else:
                return None

        already = ds_id in writer.loaded_ids()
        if already and not force_reload:
            return meta

        # ensure_schema commits, so it runs BEFORE the purge to keep purge +
        # rewrite inside the single per-dataset transaction closed below.
        writer.ensure_schema()
        try:
            if force_reload:
                writer.discard_dataset(ds_id)

            for content in contents:
                stops, routes, journeys = parse_transxchange(content, operator)
                for stop in stops:
                    writer.add_stop(stop.naptan, stop.name, stop.lat, stop.lon)
                for route in routes:
                    # Legacy (untagged) journeys for this op+route are superseded
                    # by this tagged write.
                    writer.discard_untagged_for(route.operator, route.route_num)
                    writer.upsert_route(route.operator, route.route_num,
                                        route.directions, route.stops, ds_id)
                for journey in journeys:
                    writer.add_journey(
                        journey.operator, journey.route_num, journey.direction,
                        journey.journey_code, journey.days,
                        [asdict(s) for s in journey.stops], ds_id)

            writer.mark_dataset_loaded(ds_id, meta.get("modified"), operator)
            writer.commit()  # one transaction per dataset: implicit checkpoint
        except Exception:
            # A failure mid-rewrite must not leave the purge uncommitted:
            # the next ensure_schema() would commit it, persisting a partial
            # purge. Roll the whole purge + rewrite back and re-raise.
            writer.conn.rollback()
            raise
        return meta
    finally:
        if z is not None:
            z.close()
        os.unlink(tmp_path)
        if owns:
            await client.aclose()


async def _sweep_catalogue(client: httpx.AsyncClient,
                           modified_since: Optional[str] = None
                           ) -> tuple[dict[int, dict], bool]:
    """Sweep the BODS catalogue, returning ({id: entry}, complete).

    complete is True only when the sweep finished normally (empty or short
    page); False when it broke on a non-200 page (partial view).

    modified_since: when given, the API filters server-side on modifiedDate,
    so the sweep pages only changed datasets instead of the whole catalogue.
    A filtered sweep CANNOT detect catalogue withdrawals — callers that need
    reconcile must sweep unfiltered.
    """
    catalog: dict[int, dict] = {}
    offset = 0
    limit = 100
    while True:
        url = f"{BASE_URL}/api/v1/dataset/?limit={limit}&offset={offset}"
        if modified_since:
            url += "&modifiedDate=" + quote(modified_since, safe="")
        url += f"&api_key={API_KEY}"
        resp = await client.get(url)
        if resp.status_code != 200:
            return catalog, False
        results = resp.json().get("results", [])
        if not results:
            return catalog, True
        for r in results:
            catalog[r["id"]] = r
        if len(results) < limit:
            return catalog, True
        offset += limit


async def load_all_timetable_data(force_refresh: bool = False) -> str:
    """Load all accessible timetable datasets into the SQLite index.

    Memory-flat (one XML file in RAM at a time) and resumable: each dataset
    commits as its own transaction, so an interrupted load continues where
    it left off. Datasets withdrawn from the catalogue are purged.
    """
    writer.ensure_schema()

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        catalog, sweep_complete = await _sweep_catalogue(client)
        all_ids = list(catalog)

    # Reconcile: purge datasets that disappeared from the catalogue. Only when
    # the sweep completed — a partial sweep (broke on a non-200 page) has only
    # seen part of the catalogue, so it must not purge datasets that may still
    # be live (self-healing re-download would fix it, but it leaves the index
    # temporarily incomplete during a force_refresh rebuild).
    if all_ids and sweep_complete:
        known = set(all_ids)
        for ds_id in [i for i in writer.loaded_ids() if i not in known]:
            writer.discard_dataset(ds_id)
        writer.commit()

    loaded = 0
    skipped = 0
    errors = 0
    failed_ids: list[int] = []
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        for ds_id in all_ids:
            if not force_refresh and ds_id in writer.loaded_ids():
                skipped += 1
                continue
            try:
                # force_refresh bypasses the resume guard in load_dataset: purge +
                # rewrite through _load_dataset(force_reload=True) directly.
                if force_refresh:
                    result = await _load_dataset(ds_id, force_reload=True, client=client)
                else:
                    result = await load_dataset(ds_id, client=client)
                if result is None:
                    errors += 1
                    failed_ids.append(ds_id)
                else:
                    loaded += 1
            except Exception as e:
                errors += 1
                failed_ids.append(ds_id)
                print(_redact(f"[loader] dataset {ds_id} failed: "
                              f"{type(e).__name__}: {e}"),
                      file=sys.stderr, flush=True)
            # No explicit checkpoints needed: every dataset commit IS a checkpoint.

    writer.set_last_refresh(datetime.now(timezone.utc).isoformat())
    writer.commit()
    j, s, r, d = writer.counts()
    writer.optimize_fts()
    failed_note = _format_failed(failed_ids)
    return (f"Loaded {loaded} datasets ({errors} errors, {skipped} already cached). "
            f"Total: {s} stops, {r} routes, {j} journeys from {d} datasets."
            + failed_note)


def _format_failed(failed_ids: list[int]) -> str:
    if not failed_ids:
        return ""
    shown = ", ".join(str(i) for i in failed_ids[:20])
    more = f" (+{len(failed_ids) - 20} more)" if len(failed_ids) > 20 else ""
    return f" Failed dataset IDs: [{shown}{more}] (details on stderr)."


def _parse_bods_ts(value: Optional[str]) -> Optional[datetime]:
    """Parse a BODS modified timestamp (ISO 8601 with offset). None on failure."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


async def _load_timetable_delta(since: str = "", reconcile: bool = True) -> str:
    """Incrementally refresh the timetable index using the catalogue's
    modifiedDate filter (a diff update instead of a full re-download).

    One catalogue sweep, then:
      - re-downloads only datasets whose `modified` timestamp is newer than
        the reference time (default: the last successful refresh watermark),
        replacing their previous entries in place;
      - purges datasets withdrawn from the catalogue (reconcile=true).

    Falls back to a full load when no watermark exists (first ever run).
    Typical weekly delta touches ~15-20% of the catalogue, so minutes
    instead of hours.
    """
    writer.ensure_schema()
    if not writer.loaded_ids():
        # Nothing cached yet: a delta has nothing to diff against.
        return "No cached index yet - running full load first.\n" + await load_all_timetable_data()

    reference = (
        _parse_bods_ts(since)
        or _parse_bods_ts(writer.last_refresh())
    )
    if reference is None:
        return ("No valid reference timestamp (pass since=YYYY-MM-DDTHH:MM:SS or complete a full "
                "load first to set the watermark).\n" + await load_all_timetable_data())

    sweep_start = datetime.now(timezone.utc)

    # Full sweep when the full-sweep watermark is stale or absent (withdrawal
    # purges need the whole catalogue, so reconcile runs on full sweeps only);
    # otherwise a server-side filtered sweep touches only changed datasets.
    full = writer.full_sweep_due()
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        catalog, sweep_complete = await _sweep_catalogue(
            client, modified_since=None if full else reference.isoformat())

    # An empty catalogue is only a failure when the sweep broke before seeing
    # anything; a completed sweep that returns no results is authoritative
    # (filtered: nothing changed; full: everything withdrawn -> purge below).
    if not catalog and not sweep_complete:
        return "Catalogue sweep failed (non-200 or empty) - watermark left untouched."

    updated = 0
    purged = 0
    errors = 0
    failed_ids: list[int] = []
    if reconcile and sweep_complete and full:
        for ds_id in [i for i in writer.loaded_ids() if i not in catalog]:
            writer.discard_dataset(ds_id)
            purged += 1
        if purged:
            writer.commit()

    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        for ds_id, entry in catalog.items():
            modified = _parse_bods_ts(entry.get("modified"))
            # Unparseable/missing timestamps are treated as changed (conservative).
            if modified is None or modified > reference:
                try:
                    result = await _load_dataset(ds_id, force_reload=True, client=client)
                    if result is None:
                        errors += 1
                        failed_ids.append(ds_id)
                    else:
                        updated += 1
                except Exception as e:
                    errors += 1
                    failed_ids.append(ds_id)
                    print(_redact(f"[loader] dataset {ds_id} failed: "
                                  f"{type(e).__name__}: {e}"),
                          file=sys.stderr, flush=True)

    # Full-sweep watermark: written only AFTER the purge/load phase has been
    # applied — a crash mid-apply leaves the watermark stale so the next run
    # repeats the (paid-for) full sweep instead of trusting a half-applied one.
    if full and sweep_complete:
        writer.set_last_full_sweep(datetime.now(timezone.utc).isoformat())
        writer.commit()

    # Watermark policy: advance when the run is essentially clean. A single
    # permanently-broken dataset must not freeze the watermark forever (that
    # would re-download the whole catalogue every run); failures stay listed
    # above and are retried next sweep since their `modified` stays > reference.
    error_budget = max(2, int(updated * 0.01))
    if errors <= error_budget:
        writer.set_last_refresh(sweep_start.isoformat())
        writer.commit()
    j, s, r, d = writer.counts()
    failed_note = _format_failed(failed_ids)
    watermark_note = ""
    if errors > error_budget:
        watermark_note = (f" Watermark NOT advanced (errors {errors} > budget {error_budget}) "
                          "- re-run to retry the failures.")
    return (f"Delta update: {updated} refreshed, {purged} purged, {errors} errors "
            f"(reference: {reference.isoformat()}). "
            f"Total: {s} stops, {r} routes, {j} journeys from {d} datasets."
            + watermark_note + failed_note)


# ---------------------------------------------------------------------------
# Existing OpenAPI-based tools (from specs)
# ---------------------------------------------------------------------------
def load_specs() -> dict[str, Any]:
    specs = {}
    for yml_file in sorted(SPECS_DIR.glob("*.yml")):
        with open(yml_file, "r", encoding="utf-8") as f:
            spec = yaml.safe_load(f)
        # Some BODS specs lack a servers block while their paths are already
        # absolute (/api/v1/...). The tool URL builder prepends base_path, so
        # defaulting to "/api/v1/" would double-prefix ("/api/v1/api/v1/…").
        # Anchor every spec explicitly to the site root.
        if "servers" not in spec:
            spec["servers"] = [{"url": "/"}]
        specs[yml_file.stem] = spec
    return specs


def build_param_schema(param: dict) -> dict:
    schema = param.get("schema", {})
    result = {
        "type": schema.get("type", "string"),
        "description": param.get("description", "").strip().replace("\n", " "),
    }
    if "enum" in schema:
        result["enum"] = schema["enum"]
    if "example" in schema:
        result["example"] = schema["example"]
    if param.get("required"):
        result["required"] = True
    return result


def register_tools_from_specs(specs: dict[str, Any]):
    for spec_name, spec in specs.items():
        servers = spec.get("servers", [{}])
        base_path = servers[0].get("url", "/api/v1/") if servers else "/api/v1/"
        paths = spec.get("paths", {})
        for path_template, methods in paths.items():
            for method, operation in methods.items():
                if method.lower() != "get":
                    continue
                tag = "general"
                if operation.get("tags"):
                    tag = operation["tags"][0].replace(" ", "_").replace("-", "_")
                op_id = operation.get("operationId")
                summary = operation.get("summary", "")
                description = operation.get("description", summary or "No description")
                if op_id:
                    tool_name = op_id
                else:
                    clean_path = path_template.strip("/").replace("/", "_").replace("{", "by_").replace("}", "")
                    tool_name = f"{tag}_{clean_path}"
                tool_name = tool_name.replace("-", "_").replace(".", "_")
                parameters = operation.get("parameters", [])
                param_defs = {}
                required_params = []
                for p in parameters:
                    pname = p["name"]
                    param_defs[pname] = build_param_schema(p)
                    if p.get("required"):
                        required_params.append(pname)

                def make_tool(path_tpl=path_template, bp=base_path, params_def=parameters):
                    async def tool_func(**kwargs) -> str:
                        url_path = bp.rstrip("/") + path_tpl
                        for p in params_def:
                            if p["in"] == "path" and p["name"] in kwargs:
                                url_path = url_path.replace(
                                    f"{{{p['name']}}}",
                                    quote(str(kwargs[p["name"]]), safe=""))
                        full_url = urljoin(BASE_URL + "/", url_path.lstrip("/"))
                        query = {}
                        for p in params_def:
                            pname = p["name"]
                            if p["in"] == "query" and pname in kwargs and kwargs[pname] is not None:
                                val = kwargs[pname]
                                schema = p.get("schema", {})
                                if schema.get("type") == "array":
                                    query[pname] = ",".join(str(v) for v in val) if isinstance(val, list) else str(val)
                                elif schema.get("type") == "boolean":
                                    query[pname] = "true" if val else "false"
                                else:
                                    query[pname] = str(val)
                        if query:
                            full_url += "?" + urlencode(query)
                        if API_KEY:
                            sep = "&" if "?" in full_url else "?"
                            full_url += f"{sep}api_key={API_KEY}"
                        try:
                            client = get_http_client()
                            resp = await client.get(full_url)
                            resp.raise_for_status()
                            try:
                                return json.dumps(resp.json(), indent=2, ensure_ascii=False)
                            except Exception:
                                return resp.text
                        except httpx.HTTPStatusError as e:
                            return _redact(
                                f"HTTP Error {e.response.status_code}: "
                                f"{e.response.text[:500]}")
                        except Exception as e:
                            return _redact(
                                f"Error: {type(e).__name__}: {str(e)}")
                    return tool_func

                tool_func = make_tool()
                tool_func.__name__ = tool_name
                tool_func.__doc__ = f"{description}\n\nParameters:\n"
                for pname, pdef in param_defs.items():
                    req_flag = " (required)" if pname in required_params else ""
                    tool_func.__doc__ += f"  {pname}{req_flag}: {pdef.get('description', '')}\n"
                mcp.tool(name=tool_name)(tool_func)


# ---------------------------------------------------------------------------
# NEW Rich timetable tools
# ---------------------------------------------------------------------------
@mcp.tool()
async def load_timetable_index(force_refresh: bool = False) -> str:
    """
    Download and index all accessible timetable datasets.
    Call this first if stop/route search tools return no results.

    Parameters:
      force_refresh: If true, re-download all data instead of using cache.
    """
    return await load_all_timetable_data(force_refresh=force_refresh)


@mcp.tool()
async def load_timetable_delta(since: str = "", reconcile: bool = True) -> str:
    """
    Incrementally refresh the timetable index using the catalogue's
    modifiedDate filter: re-download only datasets changed since the last
    refresh (default watermark; pass an ISO timestamp to override) and
    purge datasets withdrawn from the catalogue. Minutes instead of hours
    versus a full load. No-op fallback to a full load if no cache exists.
    Catalogue sweeps are server-side filtered by modifiedDate when the index
    is fresh; a full (unfiltered) sweep - needed to detect withdrawn
    datasets - runs at most every 7 days. reconcile=True does not force a
    full sweep, but withdrawal purges happen only on full sweeps, so with
    a fresh watermark withdrawals linger up to 7 days (self-heals on the
    next full sweep).

    Parameters:
      since: Optional ISO timestamp (YYYY-MM-DDTHH:MM:SS). Empty = use the
             watermark left by the last full load / delta.
      reconcile: If true (default), also purge datasets that vanished from
                 the catalogue.
    """
    return await _load_timetable_delta(since=since, reconcile=reconcile)


@mcp.tool()
async def search_stops(query: str) -> str:
    """
    Search for bus stops by name across all loaded timetable data.
    Returns matching stops with their NaPTAN codes.

    Parameters:
      query: Substring to search for in stop names (case-insensitive).
    """
    if not store.exists():
        return "No timetable data loaded. Please call load_timetable_index() first."
    matches = store.search_stops(query)
    if not matches:
        return f'No stops found matching "{query}".'
    return json.dumps(matches, indent=2, ensure_ascii=False)


@mcp.tool()
async def find_routes_between_stops(stop_a: str, stop_b: str) -> str:
    """
    Find all bus routes that serve BOTH of the given stops.
    Stops can be specified by NaPTAN code or by name.

    Parameters:
      stop_a: First stop (NaPTAN code or name substring).
      stop_b: Second stop (NaPTAN code or name substring).
    """
    if not store.exists():
        return "No timetable data loaded. Please call load_timetable_index() first."
    naptans_a = store.resolve_stop(stop_a)
    naptans_b = store.resolve_stop(stop_b)
    if not naptans_a:
        return f'Could not resolve stop_a: "{stop_a}". Try search_stops().'
    if not naptans_b:
        return f'Could not resolve stop_b: "{stop_b}". Try search_stops().'

    results = store.find_routes_between(naptans_a, naptans_b)
    return json.dumps(results, indent=2, ensure_ascii=False) if results else f"No single route serves both '{stop_a}' and '{stop_b}'."


@mcp.tool()
async def get_route_stops(operator: str, route: str, direction: Optional[str] = None) -> str:
    """
    Get the full ordered list of stops for a specific bus route.

    Parameters:
      operator: Operator name (exact or partial match).
      route: Route number/identifier.
      direction: Optional filter: 'inbound', 'outbound', or leave blank for all.
    """
    if not store.exists():
        return "No timetable data loaded. Please call load_timetable_index() first."
    matches = store.get_route_stops(operator, route, direction)
    return json.dumps(matches, indent=2, ensure_ascii=False) if matches else f"No route found."


@mcp.tool()
async def find_buses_by_arrival_time(stop_a: str, stop_b: str, arrive_by: str, day: Optional[str] = None) -> str:
    """
    Find scheduled buses that board at stop_a and arrive at stop_b by the given time.

    Parameters:
      stop_a: Boarding stop (NaPTAN code or name).
      stop_b: Alighting stop (NaPTAN code or name).
      arrive_by: Target arrival time (HH:MM, 24h format).
      day: Optional day filter: mon, tue, wed, thu, fri, sat, sun. Defaults to today.
    """
    if not store.exists():
        return "No timetable data loaded. Please call load_timetable_index() first."

    naptans_a = store.resolve_stop(stop_a)
    naptans_b = store.resolve_stop(stop_b)
    if not naptans_a:
        return f'Could not resolve stop_a: "{stop_a}". Try search_stops().'
    if not naptans_b:
        return f'Could not resolve stop_b: "{stop_b}". Try search_stops().'

    target_time = _parse_time(arrive_by)
    if target_time is None:
        return f'Invalid time format: "{arrive_by}". Use HH:MM (24h).'

    if day is None:
        day = datetime.now().strftime("%a").lower()
    day = day.lower()[:3]

    results = store.find_buses_by_arrival_time(naptans_a, naptans_b, arrive_by, day)
    return json.dumps(results, indent=2, ensure_ascii=False) if results else f"No buses found arriving at '{stop_b}' by {arrive_by} on {day}."


async def _annotate_disruptions(plans: list) -> list:
    """Attach disruption_alerts to plans whose legs' route or operator refs
    appear in the live SIRI-SX feed. Annotation only — plans are never
    dropped — and a feed failure degrades to silent, unannotated output."""
    try:
        messages = await _fetch_sx("disruptions")
    except Exception as e:
        print(_redact(f"[siri-sx] disruption annotation skipped: "
                      f"{type(e).__name__}: {e}"),
              file=sys.stderr, flush=True)
        return plans
    if not messages:
        return plans
    line_refs, op_refs = set(), set()
    for m in messages:
        line_refs.update(m.get("lines") or ())
        op_refs.update(m.get("operators") or ())
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


@mcp.tool()
async def plan_journey(stop_a: str, stop_b: str, arrive_by: str,
                       day: Optional[str] = None, max_changes: int = 1,
                       check_disruptions: bool = True) -> str:
    """
    Plan a journey from stop_a to stop_b arriving by a given time.
    Supports direct routes and single changes.

    Parameters:
      stop_a: Starting stop (NaPTAN code or name).
      stop_b: Destination stop (NaPTAN code or name).
      arrive_by: Target arrival time (HH:MM, 24h format).
      day: Optional day filter: mon, tue, wed, thu, fri, sat, sun. Defaults to today.
      max_changes: Maximum number of bus changes (0 = direct only, 1 = one change). Default 1.
      check_disruptions: If true (default), plans whose route or operator
                appears in the live SIRI-SX disruptions feed are annotated
                with a "disruption_alerts" list. Annotation never drops or
                reorders a plan and never changes the returned count; a feed
                failure degrades silently. At most the 15 earliest-arriving
                plans are returned, with or without annotation.
    """
    if not store.exists():
        return "No timetable data loaded. Please call load_timetable_index() first."

    naptans_a = store.resolve_stop(stop_a)
    naptans_b = store.resolve_stop(stop_b)
    if not naptans_a:
        return f'Could not resolve stop_a: "{stop_a}". Try search_stops().'
    if not naptans_b:
        return f'Could not resolve stop_b: "{stop_b}". Try search_stops().'

    target_time = _parse_time(arrive_by)
    if target_time is None:
        return f'Invalid time format: "{arrive_by}". Use HH:MM (24h).'

    if day is None:
        day = datetime.now().strftime("%a").lower()
    day = day.lower()[:3]
    target_s = target_time  # normalized 'HH:MM:SS' string from _parse_time

    plans = store.plan_direct(naptans_a, naptans_b, day, target_s)
    if max_changes >= 1:
        plans.extend(store.plan_one_change(naptans_a, naptans_b, day, target_s))

    # Deduplicate by journey codes (operator + route + depart per leg)
    seen = set()
    deduped = []
    for plan in plans:
        key = tuple(leg.get("operator", "") + "@" + leg.get("route", "") + "@" + (leg.get("depart") or "")
                    for leg in plan["legs"])
        if key not in seen:
            seen.add(key)
            # Copy: plan_direct/plan_one_change hand back the planner's own
            # lru-cached dicts, and _annotate_disruptions mutates what it is
            # given — annotating the originals would leak alerts into later
            # check_disruptions=False calls on the same cache key.
            deduped.append(dict(plan))

    deduped.sort(key=lambda p: p["legs"][-1]["arrive"] or "")
    if check_disruptions:
        await _annotate_disruptions(deduped[:15])
    # R28: the 15-plan output cap predates the annotation feature (both the
    # release base and the commit before it ended with deduped[:15]), so it is
    # the back-compatible output the Global Constraint protects. Annotation
    # must not reduce the returned set below this; it adds alerts, nothing else.
    return json.dumps(deduped[:15], indent=2, ensure_ascii=False) if deduped else f"No journey found from '{stop_a}' to '{stop_b}' by {arrive_by} on {day}."


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
        return _redact(f"Error: {type(e).__name__}: {str(e)}")


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
        return _redact(f"Error fetching live data: {type(e).__name__}: {str(e)}")

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


@mcp.tool()
async def get_fare_prices(dataset_id: str, origin_zone: Optional[str] = None,
                          destination_zone: Optional[str] = None) -> str:
    """
    Download a BODS fares dataset (NeTEx) and extract its published fare
    prices as structured JSON, optionally filtered by tariff zone. Find
    dataset ids with the fares catalogue passthrough tool
    (Data_set_api_v1_fares_dataset).

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
        return _redact(f"Error: {type(e).__name__}: {str(e)}")

    def keep(p):
        if origin_zone and origin_zone not in p["start_zones"] + p["zones"]:
            return False
        if destination_zone and destination_zone not in p["end_zones"] + p["zones"]:
            return False
        return True

    filtered = [p for p in prices if keep(p)]
    if not filtered:
        if not prices:
            return (f"No fare prices could be extracted from dataset "
                    f"{dataset_id}: the downloaded document contained no "
                    f"recognised NeTEx price elements.")
        return f"No fare prices match the given zones ({len(prices)} prices extracted)."
    return json.dumps(filtered, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
specs = load_specs()
if specs:
    register_tools_from_specs(specs)

writer.ensure_schema()
_j, _s, _r, _d = writer.counts()
if _d:
    print(f"[OpenBusData MCP] SQLite index ready: {_s} stops, {_r} routes, {_j} journeys from {_d} datasets.", file=sys.stderr)
else:
    print("[OpenBusData MCP] No timetable index yet. Call load_timetable_index() to download and parse all timetable data.", file=sys.stderr)


def main():
    """Entry point for the openbusdata-mcp console script."""
    if not API_KEY:
        print("WARNING: OPENBUS_API_KEY not set.", file=sys.stderr)
    if not specs:
        print(f"WARNING: No .yml spec files found in {SPECS_DIR}. OpenAPI-based tools will be unavailable.", file=sys.stderr)
    else:
        print(f"Loaded {len(specs)} OpenAPI specs: {', '.join(specs.keys())}", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
