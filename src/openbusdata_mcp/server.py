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
import xml.etree.ElementTree as ET
import re
import time
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import quote, urlencode, urljoin
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from .store import TimetableStore, TimetableWriter, _parse_time
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

mcp = FastMCP("openbusdata")


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
        root = ET.fromstring(content)
    except ET.ParseError:
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
                print(f"[loader] dataset {ds_id} failed: {type(e).__name__}: {e}",
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
                    print(f"[loader] dataset {ds_id} failed: {type(e).__name__}: {e}",
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
                            return (f"HTTP Error {e.response.status_code}: "
                                    f"{e.response.text[:500]}")
                        except Exception as e:
                            return f"Error: {type(e).__name__}: {str(e)}"
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


@mcp.tool()
async def plan_journey(stop_a: str, stop_b: str, arrive_by: str, day: Optional[str] = None, max_changes: int = 1) -> str:
    """
    Plan a journey from stop_a to stop_b arriving by a given time.
    Supports direct routes and single changes.

    Parameters:
      stop_a: Starting stop (NaPTAN code or name).
      stop_b: Destination stop (NaPTAN code or name).
      arrive_by: Target arrival time (HH:MM, 24h format).
      day: Optional day filter: mon, tue, wed, thu, fri, sat, sun. Defaults to today.
      max_changes: Maximum number of bus changes (0 = direct only, 1 = one change). Default 1.
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
            deduped.append(plan)

    deduped.sort(key=lambda p: p["legs"][-1]["arrive"] or "")
    return json.dumps(deduped[:15], indent=2, ensure_ascii=False) if deduped else f"No journey found from '{stop_a}' to '{stop_b}' by {arrive_by} on {day}."


@mcp.tool()
async def get_live_buses_on_route(operator_ref: str, line_ref: str) -> str:
    """
    Get real-time bus locations for a specific operator and route.

    Parameters:
      operator_ref: Operator NOC code (e.g. ARBB, SCCM, CBBH).
      line_ref: Route number (e.g. 12, MK1, 100).
    """
    query = urlencode({"operatorRef": operator_ref, "lineRef": line_ref, "api_key": API_KEY}, quote_via=quote)
    full_url = f"{BASE_URL}/api/v1/datafeed/?{query}"
    cached = _LIVE_CACHE.get((operator_ref, line_ref))
    if cached is not None:
        return json.dumps(cached, indent=2, ensure_ascii=False)
    try:
        client = get_http_client()
        resp = await client.get(full_url)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        ns = "http://www.siri.org.uk/siri"
        def get_text(tag):
            el = mvj.find(f"{{{ns}}}{tag}")
            return el.text if el is not None else "N/A"
        buses = []
        for activity in root.iter(f"{{{ns}}}VehicleActivity"):
            mvj = activity.find(f"{{{ns}}}MonitoredVehicleJourney")
            if mvj is None:
                continue
            loc = mvj.find(f"{{{ns}}}VehicleLocation")
            lat = lon = "N/A"
            if loc is not None:
                lat_el = loc.find(f"{{{ns}}}Latitude")
                lon_el = loc.find(f"{{{ns}}}Longitude")
                lat = lat_el.text if lat_el is not None else "N/A"
                lon = lon_el.text if lon_el is not None else "N/A"
            buses.append({
                "vehicle_id": get_text("VehicleRef"), "direction": get_text("DirectionRef"),
                "origin": get_text("OriginName"), "destination": get_text("DestinationName"),
                "location": {"lat": lat, "lon": lon}, "bearing": get_text("Bearing"),
            })
        if buses:
            _LIVE_CACHE.put((operator_ref, line_ref), buses)
            return json.dumps(buses, indent=2, ensure_ascii=False)
        return f"No live buses found."
    except Exception as e:
        return f"Error: {type(e).__name__}: {str(e)}"


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
