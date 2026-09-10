"""SQLite-backed timetable store: disk-backed index for openbusdata-mcp.

Replaces the in-memory timetable index for query tools. Same tool output
shapes, but memory is O(query) instead of O(entire UK timetable).
"""
import json
import sqlite3
from pathlib import Path
from typing import Optional

DB_PATH = Path.home() / ".cache" / "openbusdata" / "index.db"

def _like_escape(text: str) -> str:
    """Escape LIKE wildcards so user input matches literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Cap on how many NaPTANs a fuzzy stop query may resolve to: resolved sets
# feed `IN (...)` clauses built from `?` placeholders, and past SQLite's
# 999-variable limit the query fails outright. Name-substring matches are
# fuzzy anyway — the caller's results degrade gracefully past the cap.
MAX_RESOLVE = 500


def _parse_time(text: str) -> Optional[str]:
    """Parse HH:MM or HH:MM:SS into a normalized 'HH:MM:SS' string.

    Hours are NOT clamped at 24: TransXChange publishes post-midnight
    services as 24:00+, and stop times are compared as strings everywhere
    (Python slices and SQL >=), so '24:05:00' > '23:59:59' must hold.
    """
    if not text:
        return None
    parts = text.strip().split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        s = int(parts[2]) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        return None
    if len(parts) > 3 or h < 0 or m > 59 or s > 59:
        return None
    return f"{h:02d}:{m:02d}:{s:02d}"


class TimetableStore:
    """Read-only query interface over index.db."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            self._conn.execute("PRAGMA cache_size=-16000")  # 16MB page cache
        return self._conn

    def exists(self) -> bool:
        return self.db_path.exists()

    # -- meta ---------------------------------------------------------------
    def loaded_dataset_count(self) -> int:
        if not self.exists():
            return 0
        # Source of truth is the loaded_datasets TABLE (populated by the
        # writer / promoted from the legacy JSON watermark). The meta key
        # only exists on converter-era DBs and is stale after deltas.
        try:
            return self.conn.execute(
                "SELECT COUNT(*) FROM loaded_datasets").fetchone()[0]
        except Exception:
            v = self.conn.execute(
                "SELECT v FROM meta WHERE k='loaded_datasets'").fetchone()
            return len(json.loads(v[0])) if v else 0

    # -- stops --------------------------------------------------------------
    def search_stops(self, query: str, limit: int = 50) -> list[dict]:
        q = f"%{_like_escape(query.lower())}%"
        rows = self.conn.execute(
            "SELECT naptan, name FROM stops WHERE LOWER(name) LIKE ? ESCAPE '\\' "
            "ORDER BY name LIMIT ?", (q, limit)).fetchall()
        return [{"naptan": n, "name": name} for n, name in rows]

    def resolve_stop(self, stop_query: str) -> set[str]:
        """NaPTAN-shaped input = literal, else substring match (capped)."""
        q = stop_query.strip()
        if q.isdigit() or (len(q) >= 4 and q[:2].isdigit() and q.isalnum()):
            return {q}
        return {r[0] for r in self.conn.execute(
            "SELECT naptan FROM stops WHERE LOWER(name) LIKE ? ESCAPE '\\' "
            f"LIMIT {MAX_RESOLVE}",
            (f"%{_like_escape(q.lower())}%",))}

    def stop_name(self, naptan: str) -> str:
        row = self.conn.execute(
            "SELECT name FROM stops WHERE naptan=?", (naptan,)).fetchone()
        return row[0] if row else "Unknown"

    def stop_names_bulk(self, naptans: list[str]) -> dict[str, str]:
        if not naptans:
            return {}
        marks = ",".join("?" * len(naptans))
        return dict(self.conn.execute(
            f"SELECT naptan, name FROM stops WHERE naptan IN ({marks})",
            naptans).fetchall())

    # -- routes -------------------------------------------------------------
    def find_routes_between(self, naptans_a: set, naptans_b: set) -> list[dict]:
        """Routes serving both stop sets, with A->B / B->A ordering."""
        rows = self.conn.execute("""
            SELECT r.key, r.op, r.num, r.directions, r.stops FROM routes r
            WHERE r.key IN (SELECT key FROM stop_to_routes WHERE naptan IN (%s))
              AND r.key IN (SELECT key FROM stop_to_routes WHERE naptan IN (%s))
        """ % (",".join("?" * len(naptans_a)), ",".join("?" * len(naptans_b))),
            list(naptans_a) + list(naptans_b)).fetchall()
        sa, sb = set(naptans_a), set(naptans_b)
        results = []
        for key, op, num, directions, stops in rows:
            stop_list = json.loads(stops)
            try:
                idx_a = next(i for i, s in enumerate(stop_list) if s in sa)
                idx_b = next(i for i, s in enumerate(stop_list) if s in sb)
                direction = "A->B" if idx_a < idx_b else "B->A"
            except StopIteration:
                direction = "unknown"
            results.append({
                "operator": op, "route": num,
                "directions": sorted(json.loads(directions)),
                "stop_order": direction})
        results.sort(key=lambda x: (x["operator"], x["route"]))
        return results

    def get_route_stops(self, operator: str, route: str,
                        direction: Optional[str] = None) -> list[dict]:
        q = (f"%{route.lower()}%", f"%{operator.lower()}%")
        extra, args = "", list(q)
        if direction:
            extra = " AND LOWER(directions) LIKE ?"
            args.append(f'%"{direction.lower()}"%')
        rows = self.conn.execute(
            f"SELECT op, num, directions, stops FROM routes "
            f"WHERE LOWER(num) LIKE ? AND LOWER(op) LIKE ?{extra}", args).fetchall()
        matches = []
        for op, num, directions, stops in rows:
            stop_list = json.loads(stops)
            names = self.stop_names_bulk(stop_list)
            matches.append({
                "operator": op, "route": num,
                "directions": sorted(json.loads(directions)),
                "stops": [{"naptan": n, "name": names.get(n, "Unknown")}
                          for n in stop_list]})
        return matches

    # -- journeys -----------------------------------------------------------
    @staticmethod
    def _journey_row_to_out(row) -> dict:
        jid, ds_id, op, route, direction, code, days, jraw = row
        j = json.loads(jraw)
        return {"id": jid, "ds_id": ds_id, "operator": op, "route": route,
                "direction": direction, "journey_code": code,
                "days": json.loads(days), "stops": j["stops"]}

    def _fetch_journey(self, jid: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT id, ds_id, op, route, direction, code, days, json "
            "FROM journeys WHERE id=?", (jid,)).fetchone()
        return self._journey_row_to_out(row) if row else None

    def _journeys_touching(self, naptans: set) -> list[int]:
        """Journey ids whose stop list contains ANY of naptans."""
        # journeys has no stop index table; use json1 EXISTS over stops json
        marks = ",".join("?" * len(naptans))
        rows = self.conn.execute(
            f"SELECT id FROM journeys WHERE EXISTS ("
            f"  SELECT 1 FROM json_each(journeys.json, '$.stops') "
            f"  WHERE json_extract(value, '$.naptan') IN ({marks}))",
            list(naptans)).fetchall()
        return [r[0] for r in rows]

    def _journeys_departing(self, naptan: str, after: str) -> list[int]:
        rows = self.conn.execute(
            "SELECT id FROM journeys WHERE EXISTS ("
            "  SELECT 1 FROM json_each(journeys.json, '$.stops')"
            "  WHERE json_extract(value,'$.naptan')=?"
            "    AND COALESCE(json_extract(value,'$.departure'),"
            "                 json_extract(value,'$.arrival')) >= ?"
            ") ORDER BY id", (naptan, after)).fetchall()
        return [r[0] for r in rows]

    def find_buses_by_arrival_time(self, naptans_a: set, naptans_b: set,
                                   arrive_by: str, day: str) -> list[dict]:
        target = _parse_time(arrive_by)
        if target is None:
            return []
        target_s = target
        out = []
        # Candidate journeys: touch B stop set at all (smaller set usually)
        for jid in self._journeys_touching(naptans_b):
            j = self._fetch_journey(jid)
            if j is None:
                continue
            if day not in j["days"]:
                continue
            idx_a = idx_b = None
            for i, s in enumerate(j["stops"]):
                if s["naptan"] in naptans_a:
                    idx_a = i
                if s["naptan"] in naptans_b:
                    idx_b = i
            if idx_a is None or idx_b is None or idx_a >= idx_b:
                continue
            arr_b = j["stops"][idx_b].get("arrival")
            if arr_b and arr_b[:8] <= target_s:
                dep_a = j["stops"][idx_a].get("departure")
                out.append({
                    "operator": j["operator"], "route": j["route"],
                    "direction": j["direction"], "journey_code": j["journey_code"],
                    "board_at": self.stop_name(j["stops"][idx_a]["naptan"]),
                    "depart": dep_a, "alight_at": self.stop_name(j["stops"][idx_b]["naptan"]),
                    "arrive": arr_b})
        out.sort(key=lambda x: x["arrive"] or "")
        return out[:20]

    def plan_direct(self, naptans_a: set, naptans_b: set, day: str,
                    target_s: str) -> list[dict]:
        plans = []
        for jid in self._journeys_touching(naptans_b):
            j = self._fetch_journey(jid)
            if j is None:
                continue
            if day not in j["days"]:
                continue
            idx_a = next((i for i, s in enumerate(j["stops"]) if s["naptan"] in naptans_a), None)
            idx_b = next((i for i, s in enumerate(j["stops"]) if s["naptan"] in naptans_b), None)
            if idx_a is None or idx_b is None or idx_a >= idx_b:
                continue
            arr_b = j["stops"][idx_b].get("arrival")
            if arr_b and arr_b[:8] <= target_s:
                names = self.stop_names_bulk([j["stops"][idx_a]["naptan"], j["stops"][idx_b]["naptan"]])
                plans.append({
                    "type": "direct",
                    "legs": [{
                        "operator": j["operator"], "route": j["route"],
                        "board": names.get(j["stops"][idx_a]["naptan"], "Unknown"),
                        "depart": j["stops"][idx_a].get("departure"),
                        "alight": names.get(j["stops"][idx_b]["naptan"], "Unknown"),
                        "arrive": arr_b}],
                    "total_changes": 0})
        return plans

    def plan_one_change(self, naptans_a: set, naptans_b: set, day: str,
                        target_s: str) -> list[dict]:
        plans = []
        # Leg 1: from A to some mid stop
        for jid1 in self._journeys_touching(naptans_a):
            j1 = self._fetch_journey(jid1)
            if j1 is None:
                continue
            if day not in j1["days"]:
                continue
            idx_a1 = next((i for i, s in enumerate(j1["stops"]) if s["naptan"] in naptans_a), None)
            if idx_a1 is None:
                continue
            arr_a1 = j1["stops"][idx_a1].get("arrival") or j1["stops"][idx_a1].get("departure")
            if arr_a1 is None:
                continue
            for mid_idx in range(idx_a1 + 1, len(j1["stops"])):
                mid = j1["stops"][mid_idx]
                mid_arr = mid.get("arrival")
                if mid_arr is None:
                    continue
                # Leg 2 candidates: depart mid after mid_arr, reach B by target
                for jid2 in self._journeys_departing(mid["naptan"], mid_arr[:8]):
                    j2 = self._fetch_journey(jid2)
                    if j2 is None:
                        continue
                    if j2["id"] == j1["id"]:
                        continue
                    if day not in j2["days"]:
                        continue
                    idx_mid2 = next((i for i, s in enumerate(j2["stops"]) if s["naptan"] == mid["naptan"]), None)
                    idx_b2 = next((i for i, s in enumerate(j2["stops"]) if s["naptan"] in naptans_b), None)
                    if idx_mid2 is None or idx_b2 is None or idx_mid2 >= idx_b2:
                        continue
                    dep2 = j2["stops"][idx_mid2].get("departure") or j2["stops"][idx_mid2].get("arrival")
                    arr2 = j2["stops"][idx_b2].get("arrival")
                    if dep2 is None or arr2 is None or dep2 < mid_arr or arr2 > target_s:
                        continue
                    names = self.stop_names_bulk([
                        j1["stops"][idx_a1]["naptan"], mid["naptan"],
                        j2["stops"][idx_b2]["naptan"]])
                    plans.append({
                        "type": "change",
                        "legs": [
                            {"operator": j1["operator"], "route": j1["route"],
                             "board": names.get(j1["stops"][idx_a1]["naptan"], "Unknown"),
                             "depart": j1["stops"][idx_a1].get("departure"),
                             "alight": names.get(mid["naptan"], "Unknown"),
                             "arrive": mid_arr},
                            {"operator": j2["operator"], "route": j2["route"],
                             "board": names.get(mid["naptan"], "Unknown"),
                             "depart": dep2,
                             "alight": names.get(j2["stops"][idx_b2]["naptan"], "Unknown"),
                             "arrive": arr2}],
                        "total_changes": 1})
        return plans

    # -- provenance / maintenance (used by delta loader) --------------------
    def dataset_meta(self, ds_id: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT v FROM meta WHERE k='dataset_meta'").fetchone()
        if not row:
            return None
        meta = json.loads(row[0])
        return meta.get(str(ds_id))

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None


class TimetableWriter:
    """Read-write facade over index.db for the loader paths.

    One transaction per dataset: the DB is always consistent, so a load
    interrupted at any point simply resumes from the last committed
    dataset — checkpointing is implicit.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    def ensure_schema(self):
        """Create tables if missing; upgrade legacy meta-JSON provenance."""
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS journeys (
            id INTEGER PRIMARY KEY, ds_id INT, op TEXT, route TEXT,
            direction TEXT, code TEXT, days TEXT, json TEXT);
        CREATE TABLE IF NOT EXISTS stops (
            naptan TEXT PRIMARY KEY, name TEXT, lat REAL, lon REAL);
        CREATE TABLE IF NOT EXISTS routes (
            key TEXT PRIMARY KEY, op TEXT, num TEXT, directions TEXT,
            stops TEXT, ds_id INT DEFAULT 0);
        CREATE TABLE IF NOT EXISTS stop_to_routes (naptan TEXT, key TEXT);
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS loaded_datasets (
            ds_id INTEGER PRIMARY KEY, modified TEXT, operator TEXT);
        CREATE INDEX IF NOT EXISTS j_ds ON journeys(ds_id);
        CREATE INDEX IF NOT EXISTS s2r_n ON stop_to_routes(naptan);
        CREATE INDEX IF NOT EXISTS j_oproute ON journeys(op, route);
        """)
        # Legacy DBs: routes table predates ds_id tagging.
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(routes)")}
        if "ds_id" not in cols:
            self.conn.execute(
                "ALTER TABLE routes ADD COLUMN ds_id INT DEFAULT 0")
        # Legacy DBs kept provenance in meta.dataset_meta JSON: promote it.
        row = self.conn.execute(
            "SELECT v FROM meta WHERE k='dataset_meta'").fetchone()
        if row:
            try:
                legacy_meta = json.loads(row[0])
            except (ValueError, TypeError):
                legacy_meta = {}
            for ds_id_str, m in legacy_meta.items():
                # Junk keys (non-numeric ds ids, non-dict values) must not
                # abort the schema upgrade — skip them.
                try:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO loaded_datasets VALUES (?,?,?)",
                        (int(ds_id_str), m.get("modified"), m.get("operator")))
                except (ValueError, TypeError, AttributeError):
                    continue
            self.conn.execute("DELETE FROM meta WHERE k='dataset_meta'")
        else:
            # Converted-by-script DBs: watermark lives in meta.loaded_datasets.
            row = self.conn.execute(
                "SELECT v FROM meta WHERE k='loaded_datasets'").fetchone()
            if row:
                for ds_id in json.loads(row[0]):
                    self.conn.execute(
                        "INSERT OR IGNORE INTO loaded_datasets (ds_id) VALUES (?)",
                        (ds_id,))
        if self.conn.execute(
                "SELECT COUNT(*) FROM loaded_datasets").fetchone()[0] == 0:
            # Last resort: seed from tagged journeys if any exist.
            self.conn.execute(
                "INSERT OR IGNORE INTO loaded_datasets (ds_id) "
                "SELECT DISTINCT ds_id FROM journeys WHERE ds_id > 0")
        self.conn.commit()

    # -- read helpers (loader decisions) ------------------------------------
    def loaded_ids(self) -> set[int]:
        return {r[0] for r in self.conn.execute(
            "SELECT ds_id FROM loaded_datasets")}

    def counts(self) -> tuple[int, int, int, int]:
        j = self.conn.execute("SELECT COUNT(*) FROM journeys").fetchone()[0]
        s = self.conn.execute("SELECT COUNT(*) FROM stops").fetchone()[0]
        r = self.conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0]
        d = self.conn.execute("SELECT COUNT(*) FROM loaded_datasets").fetchone()[0]
        return j, s, r, d

    def last_refresh(self) -> Optional[str]:
        row = self.conn.execute(
            "SELECT v FROM meta WHERE k='last_refresh'").fetchone()
        return row[0] if row and row[0] else None

    # -- writers -------------------------------------------------------------
    def add_stop(self, naptan: str, name: str, lat=None, lon=None):
        self.conn.execute(
            "INSERT INTO stops (naptan, name, lat, lon) VALUES (?,?,?,?) "
            "ON CONFLICT(naptan) DO UPDATE SET name="
            "CASE WHEN excluded.name != '' THEN excluded.name ELSE stops.name END",
            (naptan, name, lat, lon))

    def upsert_route(self, op: str, num: str, directions: set, stop_list: list,
                     ds_id: int):
        """Merge semantics: union directions, keep the longest stop sequence."""
        key = f"{op}|{num}"
        dirs_json = json.dumps(sorted(directions))
        stops_json = json.dumps(stop_list)
        row = self.conn.execute(
            "SELECT directions, stops FROM routes WHERE key=?", (key,)).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO routes (key, op, num, directions, stops, ds_id) "
                "VALUES (?,?,?,?,?,?)", (key, op, num, dirs_json, stops_json, ds_id))
        else:
            old_dirs = set(json.loads(row[0])) | set(directions)
            old_stops = json.loads(row[1])
            best = stop_list if len(stop_list) > len(old_stops) else old_stops
            self.conn.execute(
                "UPDATE routes SET directions=?, stops=? WHERE key=?",
                (json.dumps(sorted(old_dirs)), json.dumps(best), key))
        self.conn.executemany(
            "INSERT OR IGNORE INTO stop_to_routes VALUES (?,?)",
            [(n, key) for n in stop_list])

    def add_journey(self, op: str, num: str, direction: str, code: str,
                    days: set, stops: list, ds_id: int):
        j = {"operator": op, "route_num": num, "direction": direction,
             "journey_code": code, "dataset_id": ds_id,
             "days": sorted(days), "stops": [dict(st) for st in stops]}
        self.conn.execute(
            "INSERT INTO journeys (ds_id, op, route, direction, code, days, json) "
            "VALUES (?,?,?,?,?,?,?)",
            (ds_id, op, num, direction, code, json.dumps(sorted(days)), json.dumps(j)))

    def commit(self):
        self.conn.commit()

    def mark_dataset_loaded(self, ds_id: int, modified: Optional[str],
                            operator: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO loaded_datasets VALUES (?,?,?)",
            (ds_id, modified, operator))

    def set_last_refresh(self, iso: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO meta VALUES ('last_refresh', ?)", (iso,))

    def discard_untagged_for(self, op: str, route_num: str):
        """Remove pre-SQLite-era journeys (ds_id=0) for one operator+route.

        Used when a legacy (untagged) dataset is refreshed: the fresh tagged
        insert supersedes the old rows, which cannot be purged by ds_id.
        """
        self.conn.execute(
            "DELETE FROM journeys WHERE ds_id=0 AND op=? AND route=?",
            (op, route_num))

    def discard_dataset(self, ds_id: int):
        """Purge one dataset's journeys + route discoverability (no commit).

        Journeys are tagged with their source dataset, so they purge exactly.
        Route *rows* are merged state across datasets — the ds_id column only
        records the LAST contributor — so deleting by ds_id would drop routes
        still served by a surviving dataset. Instead, route rows are left in
        place and a route with no surviving journeys becomes undiscoverable
        (its stop_to_routes refs go); it self-heals when a replacement
        dataset is re-downloaded (upsert keeps the longest stop sequence),
        and is fully removed by the next full rebuild. The caller owns the
        transaction: purge + rewrite must commit together.
        """
        self.conn.execute("DELETE FROM journeys WHERE ds_id=?", (ds_id,))
        self.conn.execute(
            "DELETE FROM stop_to_routes WHERE key NOT IN "
            "(SELECT DISTINCT op || '|' || route FROM journeys)")
        self.conn.execute("DELETE FROM loaded_datasets WHERE ds_id=?", (ds_id,))