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
import yaml
import json
import zipfile
import io
import xml.etree.ElementTree as ET
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlencode
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, time, timezone
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
    arrival: Optional[time] = None
    departure: Optional[time] = None


@dataclass(slots=True)
class Journey:
    operator: str
    route_num: str
    direction: str
    journey_code: str
    stops: list[JourneyStop] = field(default_factory=list)
    days: set[str] = field(default_factory=set)  # mon, tue, wed, thu, fri, sat, sun
    dataset_id: int = 0  # BODS dataset this journey came from (enables surgical purge)


class TimetableIndex:
    """In-memory index of all parsed timetable data."""

    def __init__(self):
        self.stops: dict[str, Stop] = {}
        self.routes: dict[str, Route] = {}
        self.stop_to_routes: dict[str, set[str]] = {}
        self.journeys: list[Journey] = []
        self.loaded_datasets: set[int] = set()
        # Per-dataset provenance for incremental (diff) refreshes:
        #   ds_id -> {"modified": "2026-09-01T06:00:00Z", "operator": "..."}
        self.dataset_meta: dict[int, dict] = {}
        # Timestamp of the last completed catalogue sweep (ISO, UTC). Delta loads
        # use it as the "since" watermark.
        self.last_refresh: Optional[str] = None

    def add_stop(self, stop: Stop):
        if stop.naptan not in self.stops:
            self.stops[stop.naptan] = stop
        else:
            if not self.stops[stop.naptan].name:
                self.stops[stop.naptan].name = stop.name

    def add_route(self, route: Route):
        key = f"{route.operator}|{route.route_num}"
        if key not in self.routes:
            self.routes[key] = route
        else:
            self.routes[key].directions |= route.directions
            if len(route.stops) > len(self.routes[key].stops):
                self.routes[key].stops = route.stops
        for naptan in route.stops:
            if naptan not in self.stop_to_routes:
                self.stop_to_routes[naptan] = set()
            self.stop_to_routes[naptan].add(key)

    def add_journey(self, journey: Journey):
        self.journeys.append(journey)

    def save_cache(self):
        cache = {
            "stops": {k: asdict(v) for k, v in self.stops.items()},
            "routes": {
                k: {"operator": v.operator, "route_num": v.route_num,
                    "directions": list(v.directions), "stops": v.stops}
                for k, v in self.routes.items()
            },
            "stop_to_routes": {k: list(v) for k, v in self.stop_to_routes.items()},
            "journeys": [
                {
                    "operator": j.operator,
                    "route_num": j.route_num,
                    "direction": j.direction,
                    "journey_code": j.journey_code,
                    "dataset_id": j.dataset_id,
                    "stops": [
                        {"naptan": s.naptan,
                         "arrival": s.arrival.isoformat() if s.arrival else None,
                         "departure": s.departure.isoformat() if s.departure else None}
                        for s in j.stops
                    ],
                    "days": list(j.days),
                }
                for j in self.journeys
            ],
            "loaded_datasets": list(self.loaded_datasets),
            "dataset_meta": self.dataset_meta,
            "last_refresh": self.last_refresh,
        }
        with open(CACHE_DIR / "timetable_cache.json", "w", encoding="utf-8") as f:
            json.dump(cache, f)

    def load_cache(self) -> bool:
        cache_file = CACHE_DIR / "timetable_cache.json"
        if not cache_file.exists():
            return False
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cache = json.load(f)
            self.stops = {k: Stop(**v) for k, v in cache.get("stops", {}).items()}
            self.routes = {
                k: Route(operator=v["operator"], route_num=v["route_num"],
                         directions=set(v.get("directions", [])), stops=v.get("stops", []))
                for k, v in cache.get("routes", {}).items()
            }
            self.stop_to_routes = {k: set(v) for k, v in cache.get("stop_to_routes", {}).items()}
            self.loaded_datasets = set(cache.get("loaded_datasets", []))
            self.dataset_meta = {int(k): v for k, v in cache.get("dataset_meta", {}).items()}
            self.last_refresh = cache.get("last_refresh")
            self.journeys = []
            for j in cache.get("journeys", []):
                stops = []
                for s in j.get("stops", []):
                    arr = time.fromisoformat(s["arrival"]) if s.get("arrival") else None
                    dep = time.fromisoformat(s["departure"]) if s.get("departure") else None
                    stops.append(JourneyStop(naptan=s["naptan"], arrival=arr, departure=dep))
                self.journeys.append(Journey(
                    operator=j["operator"], route_num=j["route_num"],
                    direction=j["direction"], journey_code=j["journey_code"],
                    stops=stops, days=set(j.get("days", [])),
                    dataset_id=int(j.get("dataset_id", 0)),
                ))
            return True
        except Exception as e:
            print(f"Cache load failed: {e}", file=sys.stderr)
            return False

    def clear(self):
        self.stops.clear()
        self.routes.clear()
        self.stop_to_routes.clear()
        self.journeys.clear()
        self.loaded_datasets.clear()
        self.dataset_meta.clear()
        self.last_refresh = None

    def discard_dataset(self, ds_id: int):
        """Remove all index entries contributed by a single dataset.

        Journeys are tagged with their source dataset, so they purge exactly.
        stop_to_routes is rebuilt from surviving journeys. Route *data* entries
        are left in place: they are merged state across datasets and
        self-heal when a replacement dataset is re-downloaded (add_route keeps
        the longest stop sequence). A route left behind by a withdrawn
        dataset is undiscoverable (no stop_to_routes ref) and is fully
        removed by the next force_refresh / reconcile rebuild.
        """
        self.loaded_datasets.discard(ds_id)
        self.dataset_meta.pop(ds_id, None)
        self.journeys = [j for j in self.journeys if j.dataset_id != ds_id]
        remaining_route_keys: set[str] = {f"{j.operator}|{j.route_num}" for j in self.journeys}
        self.stop_to_routes = {
            k: routes & remaining_route_keys
            for k, routes in self.stop_to_routes.items()
            if routes & remaining_route_keys
        }


# Global index
index = TimetableIndex()


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


def _parse_time(text: str) -> Optional[time]:
    """Parse HH:MM or HH:MM:SS."""
    if not text:
        return None
    parts = text.strip().split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        s = int(parts[2]) if len(parts) > 2 else 0
        return time(hour=h % 24, minute=m, second=s)
    except (ValueError, IndexError):
        return None


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

    ns = ""
    if 'xmlns="' in content:
        ns = content.split('xmlns="')[1].split('"')[0]

    q = lambda tag: _get_ns(tag, ns)

    stops: list[Stop] = []
    routes: list[Route] = []
    journeys: list[Journey] = []

    # --- Extract StopPoints ---
    stop_map: dict[str, str] = {}
    for asp in root.iter(q("AnnotatedStopPointRef")):
        ref_elem = asp.find(q("StopPointRef"))
        name_elem = asp.find(q("CommonName"))
        if ref_elem is not None:
            naptan = ref_elem.text
            name = name_elem.text if name_elem is not None else "Unknown"
            stop_map[naptan] = name
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

        # Build stop schedule by accumulating run times
        journey_stops: list[JourneyStop] = []
        current_time = datetime.combine(datetime.today(), dep_time)

        for sid in jp_data["section_ids"]:
            if sid not in jps_links:
                continue
            for i, (from_ref, to_ref, runtime) in enumerate(jps_links[sid]):
                if i == 0 and not journey_stops:
                    # First stop
                    journey_stops.append(JourneyStop(naptan=from_ref, departure=current_time.time()))
                # Travel to next stop
                current_time += runtime
                journey_stops.append(JourneyStop(naptan=to_ref, arrival=current_time.time()))

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
async def load_dataset(ds_id: int) -> dict:
    """Download and parse a single timetable dataset. Returns metadata.

    No-op when the dataset is already loaded (resume semantics); updating a
    changed dataset goes through load_timetable_delta, which calls
    _load_dataset(force_reload=True) directly.
    """
    if ds_id in index.loaded_datasets:
        return {}
    return await _load_dataset(ds_id)


async def _load_dataset(ds_id: int, force_reload: bool = False) -> dict:
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        meta_resp = await client.get(f"{BASE_URL}/api/v1/dataset/{ds_id}/?api_key={API_KEY}")
        if meta_resp.status_code != 200:
            return {}
        meta = meta_resp.json()

        operator = meta.get("operatorName", "Unknown")
        download_url = meta.get("url")
        if not download_url:
            return meta

        zip_resp = await client.get(f"{download_url}?api_key={API_KEY}")
        if zip_resp.status_code != 200 or len(zip_resp.content) < 100:
            return meta

        try:
            z = zipfile.ZipFile(io.BytesIO(zip_resp.content))
        except zipfile.BadZipFile:
            return meta

        if force_reload:
            index.discard_dataset(ds_id)

        xml_files = [n for n in z.namelist() if n.endswith(".xml")]
        for fname in xml_files:
            try:
                content = z.read(fname).decode("utf-8", errors="ignore")
            except Exception:
                continue
            stops, routes, journeys = parse_transxchange(content, operator)
            for stop in stops:
                index.add_stop(stop)
            for route in routes:
                index.add_route(route)
            for journey in journeys:
                journey.dataset_id = ds_id
                index.add_journey(journey)

        index.loaded_datasets.add(ds_id)
        index.dataset_meta[ds_id] = {
            "modified": meta.get("modified"),
            "operator": operator,
        }
        return meta


async def load_all_timetable_data(force_refresh: bool = False) -> str:
    """Load all accessible timetable datasets and build indexes.

    Resumes from a partial/complete cache: already-loaded datasets are
    skipped, datasets withdrawn from the catalogue are purged, and progress
    is checkpointed to disk every 100 datasets so an interrupted load
    continues where it left off instead of restarting from zero.
    """
    if not force_refresh:
        index.load_cache()

    if force_refresh:
        index.clear()

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        all_ids: list[int] = []
        offset = 0
        limit = 100
        while True:
            resp = await client.get(f"{BASE_URL}/api/v1/dataset/?limit={limit}&offset={offset}&api_key={API_KEY}")
            if resp.status_code != 200:
                break
            data = resp.json()
            results = data.get("results", [])
            if not results:
                break
            for r in results:
                all_ids.append(r["id"])
            if len(results) < limit:
                break
            offset += limit

    # Reconcile: purge datasets that disappeared from the catalogue.
    if not force_refresh and all_ids:
        known = set(all_ids)
        for ds_id in [i for i in index.loaded_datasets if i not in known]:
            index.discard_dataset(ds_id)

    loaded = 0
    skipped = 0
    errors = 0
    for ds_id in all_ids:
        if ds_id in index.loaded_datasets:
            skipped += 1
            continue
        try:
            await load_dataset(ds_id)
            loaded += 1
        except Exception:
            errors += 1
        if loaded % 100 == 0:
            index.save_cache()  # checkpoint: partial progress survives a restart

    index.last_refresh = datetime.now(timezone.utc).isoformat()
    index.save_cache()
    return (f"Loaded {loaded} datasets ({errors} errors, {skipped} already cached). "
            f"Total: {len(index.stops)} stops, {len(index.routes)} routes, {len(index.journeys)} journeys.")


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
      - purges datasets withdrawn from the catalogue (reconcile=true);
      - checkpoints to disk as it goes.

    Falls back to a full load when no watermark exists (first ever run).
    Typical weekly delta touches ~15-20% of the catalogue, so minutes
    instead of hours.
    """
    if not index.loaded_datasets:
        index.load_cache()
    if not index.loaded_datasets:
        # Nothing cached yet: a delta has nothing to diff against.
        return "No cached index yet - running full load first.\n" + await load_all_timetable_data()

    reference = (
        _parse_bods_ts(since)
        or _parse_bods_ts(index.last_refresh)
    )
    if reference is None:
        return ("No valid reference timestamp (pass since=YYYY-MM-DDTHH:MM:SS or complete a full "
                "load first to set the watermark).\n" + await load_all_timetable_data())

    sweep_start = datetime.now(timezone.utc)

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        catalog: dict[int, dict] = {}
        offset = 0
        limit = 100
        while True:
            resp = await client.get(
                f"{BASE_URL}/api/v1/dataset/?limit={limit}&offset={offset}&api_key={API_KEY}")
            if resp.status_code != 200:
                break
            results = resp.json().get("results", [])
            if not results:
                break
            for r in results:
                catalog[r["id"]] = r
            if len(results) < limit:
                break
            offset += limit

    if not catalog:
        return "Catalogue sweep failed (non-200 or empty) - watermark left untouched."

    updated = 0
    purged = 0
    errors = 0
    if reconcile:
        for ds_id in [i for i in index.loaded_datasets if i not in catalog]:
            index.discard_dataset(ds_id)
            purged += 1

    for ds_id, entry in catalog.items():
        modified = _parse_bods_ts(entry.get("modified"))
        # Unparseable/missing timestamps are treated as changed (conservative).
        if modified is None or modified > reference:
            try:
                await _load_dataset(ds_id, force_reload=True)
                updated += 1
            except Exception:
                errors += 1
            if updated % 100 == 0:
                index.save_cache()

    if errors == 0:
        index.last_refresh = sweep_start.isoformat()
    index.save_cache()
    return (f"Delta update: {updated} refreshed, {purged} purged, {errors} errors "
            f"(reference: {reference.isoformat()}). "
            f"Total: {len(index.stops)} stops, {len(index.routes)} routes, "
            f"{len(index.journeys)} journeys."
            + ("" if errors == 0 else " Watermark NOT advanced - re-run to retry the failures."))


# ---------------------------------------------------------------------------
# Existing OpenAPI-based tools (from specs)
# ---------------------------------------------------------------------------
def load_specs() -> dict[str, Any]:
    specs = {}
    for yml_file in sorted(SPECS_DIR.glob("*.yml")):
        with open(yml_file, "r", encoding="utf-8") as f:
            specs[yml_file.stem] = yaml.safe_load(f)
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
                                url_path = url_path.replace(f"{{{p['name']}}}", str(kwargs[p["name"]]))
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
                            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                                resp = await client.get(full_url)
                                resp.raise_for_status()
                                try:
                                    return json.dumps(resp.json(), indent=2, ensure_ascii=False)
                                except Exception:
                                    return resp.text
                        except httpx.HTTPStatusError as e:
                            return f"HTTP Error {e.response.status_code}: {e.response.text}"
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
    if not index.stops:
        if not index.load_cache():
            return "No timetable data loaded. Please call load_timetable_index() first."
    query_lower = query.lower()
    matches = []
    for naptan, stop in index.stops.items():
        if query_lower in stop.name.lower():
            matches.append({"naptan": naptan, "name": stop.name})
    matches.sort(key=lambda x: x["name"])
    if not matches:
        return f'No stops found matching "{query}".'
    return json.dumps(matches[:50], indent=2, ensure_ascii=False)


@mcp.tool()
async def find_routes_between_stops(stop_a: str, stop_b: str) -> str:
    """
    Find all bus routes that serve BOTH of the given stops.
    Stops can be specified by NaPTAN code or by name.

    Parameters:
      stop_a: First stop (NaPTAN code or name substring).
      stop_b: Second stop (NaPTAN code or name substring).
    """
    if not index.stops:
        if not index.load_cache():
            return "No timetable data loaded. Please call load_timetable_index() first."
    naptans_a = _resolve_stop(stop_a)
    naptans_b = _resolve_stop(stop_b)
    if not naptans_a:
        return f'Could not resolve stop_a: "{stop_a}". Try search_stops().'
    if not naptans_b:
        return f'Could not resolve stop_b: "{stop_b}". Try search_stops().'

    results = []
    seen = set()
    for na in naptans_a:
        for nb in naptans_b:
            routes_a = index.stop_to_routes.get(na, set())
            routes_b = index.stop_to_routes.get(nb, set())
            for route_key in routes_a & routes_b:
                if route_key in seen:
                    continue
                seen.add(route_key)
                route = index.routes[route_key]
                try:
                    idx_a = [i for i, s in enumerate(route.stops) if s in naptans_a][0]
                    idx_b = [i for i, s in enumerate(route.stops) if s in naptans_b][0]
                    direction = "A->B" if idx_a < idx_b else "B->A"
                except IndexError:
                    direction = "unknown"
                results.append({
                    "operator": route.operator, "route": route.route_num,
                    "directions": sorted(route.directions), "stop_order": direction,
                })
    results.sort(key=lambda x: (x["operator"], x["route"]))
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
    if not index.routes:
        if not index.load_cache():
            return "No timetable data loaded. Please call load_timetable_index() first."
    matches = []
    for key, r in index.routes.items():
        if route.lower() in r.route_num.lower() and operator.lower() in r.operator.lower():
            if direction and direction.lower() not in [d.lower() for d in r.directions]:
                continue
            stop_names = [{"naptan": n, "name": index.stops.get(n, Stop(n, "Unknown")).name} for n in r.stops]
            matches.append({"operator": r.operator, "route": r.route_num,
                            "directions": sorted(r.directions), "stops": stop_names})
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
    if not index.journeys:
        if not index.load_cache():
            return "No timetable data loaded. Please call load_timetable_index() first."

    naptans_a = _resolve_stop(stop_a)
    naptans_b = _resolve_stop(stop_b)
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

    results = []
    for journey in index.journeys:
        if day not in journey.days:
            continue

        # Find stop indices
        idx_a = None
        idx_b = None
        for i, js in enumerate(journey.stops):
            if js.naptan in naptans_a:
                idx_a = i
            if js.naptan in naptans_b:
                idx_b = i

        if idx_a is None or idx_b is None or idx_a >= idx_b:
            continue

        arrival_at_b = journey.stops[idx_b].arrival
        departure_from_a = journey.stops[idx_a].departure

        if arrival_at_b is None:
            continue

        if arrival_at_b <= target_time:
            results.append({
                "operator": journey.operator,
                "route": journey.route_num,
                "direction": journey.direction,
                "journey_code": journey.journey_code,
                "board_at": index.stops.get(journey.stops[idx_a].naptan, Stop("", "Unknown")).name,
                "depart": departure_from_a.isoformat() if departure_from_a else None,
                "alight_at": index.stops.get(journey.stops[idx_b].naptan, Stop("", "Unknown")).name,
                "arrive": arrival_at_b.isoformat(),
            })

    results.sort(key=lambda x: x["arrive"] or "")
    return json.dumps(results[:20], indent=2, ensure_ascii=False) if results else f"No buses found arriving at '{stop_b}' by {arrive_by} on {day}."


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
    if not index.journeys:
        if not index.load_cache():
            return "No timetable data loaded. Please call load_timetable_index() first."

    naptans_a = _resolve_stop(stop_a)
    naptans_b = _resolve_stop(stop_b)
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

    plans = []

    # --- Direct journeys ---
    for journey in index.journeys:
        if day not in journey.days:
            continue
        idx_a = next((i for i, s in enumerate(journey.stops) if s.naptan in naptans_a), None)
        idx_b = next((i for i, s in enumerate(journey.stops) if s.naptan in naptans_b), None)
        if idx_a is None or idx_b is None or idx_a >= idx_b:
            continue
        arrival_at_b = journey.stops[idx_b].arrival
        if arrival_at_b and arrival_at_b <= target_time:
            plans.append({
                "type": "direct",
                "legs": [{
                    "operator": journey.operator,
                    "route": journey.route_num,
                    "board": index.stops.get(journey.stops[idx_a].naptan, Stop("", "Unknown")).name,
                    "depart": journey.stops[idx_a].departure.isoformat() if journey.stops[idx_a].departure else None,
                    "alight": index.stops.get(journey.stops[idx_b].naptan, Stop("", "Unknown")).name,
                    "arrive": arrival_at_b.isoformat(),
                }],
                "total_changes": 0,
            })

    if max_changes >= 1:
        # --- Single change ---
        # Group journeys by the stops they serve for fast lookup
        for j1 in index.journeys:
            if day not in j1.days:
                continue
            idx_a1 = next((i for i, s in enumerate(j1.stops) if s.naptan in naptans_a), None)
            if idx_a1 is None:
                continue
            arr_a1 = j1.stops[idx_a1].arrival or j1.stops[idx_a1].departure
            if arr_a1 is None:
                continue

            # Every stop after A on j1 is a potential change point
            for mid_idx in range(idx_a1 + 1, len(j1.stops)):
                mid_naptan = j1.stops[mid_idx].naptan
                mid_arrival = j1.stops[mid_idx].arrival
                if mid_arrival is None:
                    continue

                # Find j2 that departs from mid_naptan and goes to B
                for j2 in index.journeys:
                    if day not in j2.days:
                        continue
                    if j2.operator == j1.operator and j2.route_num == j1.route_num and j2.direction == j1.direction:
                        continue  # Same journey

                    idx_mid2 = next((i for i, s in enumerate(j2.stops) if s.naptan == mid_naptan), None)
                    idx_b2 = next((i for i, s in enumerate(j2.stops) if s.naptan in naptans_b), None)
                    if idx_mid2 is None or idx_b2 is None or idx_mid2 >= idx_b2:
                        continue

                    dep_mid2 = j2.stops[idx_mid2].departure or j2.stops[idx_mid2].arrival
                    arr_b2 = j2.stops[idx_b2].arrival
                    if dep_mid2 is None or arr_b2 is None:
                        continue

                    # Connection time: must depart mid after arriving there
                    if dep_mid2 < mid_arrival:
                        continue

                    # Must arrive at B by target
                    if arr_b2 > target_time:
                        continue

                    plans.append({
                        "type": "change",
                        "legs": [
                            {
                                "operator": j1.operator,
                                "route": j1.route_num,
                                "board": index.stops.get(j1.stops[idx_a1].naptan, Stop("", "Unknown")).name,
                                "depart": j1.stops[idx_a1].departure.isoformat() if j1.stops[idx_a1].departure else None,
                                "alight": index.stops.get(mid_naptan, Stop("", "Unknown")).name,
                                "arrive": mid_arrival.isoformat(),
                            },
                            {
                                "operator": j2.operator,
                                "route": j2.route_num,
                                "board": index.stops.get(mid_naptan, Stop("", "Unknown")).name,
                                "depart": dep_mid2.isoformat(),
                                "alight": index.stops.get(j2.stops[idx_b2].naptan, Stop("", "Unknown")).name,
                                "arrive": arr_b2.isoformat(),
                            },
                        ],
                        "total_changes": 1,
                    })

    # Deduplicate by journey codes
    seen = set()
    deduped = []
    for plan in plans:
        key = tuple(leg.get("route", "") + "@" + (leg.get("depart") or "") for leg in plan["legs"])
        if key not in seen:
            seen.add(key)
            deduped.append(plan)

    deduped.sort(key=lambda p: p["legs"][-1]["arrive"])
    return json.dumps(deduped[:15], indent=2, ensure_ascii=False) if deduped else f"No journey found from '{stop_a}' to '{stop_b}' by {arrive_by} on {day}."


@mcp.tool()
async def get_live_buses_on_route(operator_ref: str, line_ref: str) -> str:
    """
    Get real-time bus locations for a specific operator and route.

    Parameters:
      operator_ref: Operator NOC code (e.g. ARBB, SCCM, CBBH).
      line_ref: Route number (e.g. 12, MK1, 100).
    """
    full_url = f"{BASE_URL}/api/v1/datafeed/?operatorRef={operator_ref}&lineRef={line_ref}&api_key={API_KEY}"
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(full_url)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            ns = "http://www.siri.org.uk/siri"
            buses = []
            for activity in root.iter(f"{{{ns}}}VehicleActivity"):
                mvj = activity.find(f"{{{ns}}}MonitoredVehicleJourney")
                if mvj is None:
                    continue
                def get_text(tag):
                    el = mvj.find(f"{{{ns}}}{tag}")
                    return el.text if el is not None else "N/A"
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
            return json.dumps(buses, indent=2, ensure_ascii=False) if buses else f"No live buses found."
    except Exception as e:
        return f"Error: {type(e).__name__}: {str(e)}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _resolve_stop(stop_query: str) -> set[str]:
    """Resolve a stop query to a set of NaPTAN codes."""
    if stop_query.isdigit() or (len(stop_query) >= 8 and stop_query[:2].isdigit()):
        return {stop_query}
    query_lower = stop_query.lower()
    matches = set()
    for naptan, stop in index.stops.items():
        if query_lower in stop.name.lower():
            matches.add(naptan)
    return matches


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
specs = load_specs()
if specs:
    register_tools_from_specs(specs)

if index.load_cache():
    print(f"[OpenBusData MCP] Loaded timetable cache: {len(index.stops)} stops, {len(index.routes)} routes, {len(index.journeys)} journeys from {len(index.loaded_datasets)} datasets.", file=sys.stderr)
else:
    print("[OpenBusData MCP] No timetable cache found. Call load_timetable_index() to download and parse all timetable data.", file=sys.stderr)


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
