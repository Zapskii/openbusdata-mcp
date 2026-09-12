"""SQLite-backed timetable store: disk-backed index for openbusdata-mcp.

Replaces the in-memory timetable index for query tools. Same tool output
shapes, but memory is O(query) instead of O(entire UK timetable).
"""
import functools
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path.home() / ".cache" / "openbusdata" / "index.db"

def _like_escape(text: str) -> str:
    """Escape LIKE wildcards so user input matches literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fts_query(query: str) -> Optional[str]:
    """Build an FTS5 MATCH expression from user input, or None if untokenizable.

    Each whitespace token becomes a quoted prefix term, AND-joined. Tokens with
    no alphanumeric characters (bare '%', '_', '\', quotes) are dropped before
    FTS5 sees them — they would match nothing meaningful — so if every token is
    dropped this returns None and the caller falls back to LIKE.
    """
    tokens = [t for t in query.strip().lower().split() if t]
    tokens = [t for t in tokens if any(c.isalnum() for c in t)]
    if not tokens:
        return None
    return " AND ".join(f'"{t.replace(chr(34), chr(34) * 2)}"*' for t in tokens)


# Cap on how many NaPTANs a fuzzy stop query may resolve to: resolved sets
# feed `IN (...)` clauses built from `?` placeholders, and find_routes_between
# binds BOTH sets in one query, so the combined count must stay under SQLite's
# 999-variable limit (pre-3.32). 400 + 400 = 800 < 999.
MAX_RESOLVE = 400

# Operating-day bitmask for SQL-level day filtering: mon=1 .. sun=64.
DAY_BITS = {"mon": 1, "tue": 2, "wed": 4, "thu": 8,
            "fri": 16, "sat": 32, "sun": 64}


def _days_mask(days: set) -> int:
    return sum(DAY_BITS[d] for d in days if d in DAY_BITS)


def _parse_iso(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


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
    if len(parts) > 3 or h < 0 or m < 0 or m > 59 or s < 0 or s > 59:
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
            self._conn.execute("PRAGMA mmap_size=268435456")  # 256MB mmap reads
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
    def _search_stops_like(self, query: str, limit: int = 50) -> list[dict]:
        q = f"%{_like_escape(query.lower())}%"
        rows = self.conn.execute(
            "SELECT naptan, name FROM stops WHERE LOWER(name) LIKE ? ESCAPE '\\' "
            "ORDER BY name LIMIT ?", (q, limit)).fetchall()
        return [{"naptan": n, "name": name} for n, name in rows]

    def search_stops(self, query: str, limit: int = 50) -> list[dict]:
        q = query.strip()
        if not q:
            return []   # whitespace-only: nothing to search (regression fix)
        fts = _fts_query(q)
        if fts:
            rows = self.conn.execute(
                "SELECT naptan, name FROM stops_fts WHERE stops_fts MATCH ? "
                "ORDER BY rank LIMIT ?", (fts, limit)).fetchall()
            if rows:
                return [{"naptan": n, "name": name} for n, name in rows]
        return self._search_stops_like(q, limit)        # FTS5 empty -> LIKE fallback

    def resolve_stop(self, stop_query: str) -> set[str]:
        """NaPTAN-shaped input = literal, else FTS5 match (capped), else LIKE."""
        q = stop_query.strip()
        if q.isdigit() or (len(q) >= 4 and q[:2].isdigit() and q.isalnum()):
            return {q}
        fts = _fts_query(q)
        if fts:
            rows = self.conn.execute(
                "SELECT naptan FROM stops_fts WHERE stops_fts MATCH ? LIMIT ?",
                (fts, MAX_RESOLVE)).fetchall()
            if rows:
                return {r[0] for r in rows}
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
        if not naptans_a or not naptans_b:
            return []
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
        fts = _fts_query(f"{route} {operator}".strip())
        if fts:
            rows = self.conn.execute(
                "SELECT r.op, r.num, r.directions, r.stops FROM routes_fts f "
                "JOIN routes r ON r.rowid = f.rowid "
                "WHERE routes_fts MATCH ? ORDER BY rank LIMIT 50", (fts,)).fetchall()
        else:
            rows = []
        if not rows:  # FTS returned nothing (or query had no tokenizable terms) -> LIKE substring fallback
            q = (f"%{route.lower()}%", f"%{operator.lower()}%")
            rows = self.conn.execute(
                "SELECT op, num, directions, stops FROM routes "
                "WHERE LOWER(num) LIKE ? AND LOWER(op) LIKE ?", q).fetchall()
        matches = []
        for op, num, directions, stops in rows:
            dirs = json.loads(directions)
            if direction and direction.lower() not in [d.lower() for d in dirs]:
                continue
            stop_list = json.loads(stops)
            names = self.stop_names_bulk(stop_list)
            matches.append({
                "operator": op, "route": num,
                "directions": sorted(dirs),
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

    def _fetch_journeys(self, jids: list[int]) -> dict[int, dict]:
        if not jids:
            return {}
        out: dict[int, dict] = {}
        # Chunk past SQLite's variable limit (999 on older builds): a busy
        # stop can touch thousands of journeys, and one giant IN clause would
        # fail with "too many SQL variables".
        for i in range(0, len(jids), 500):
            chunk = jids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT id, ds_id, op, route, direction, code, days, json "
                f"FROM journeys WHERE id IN ({marks})", chunk).fetchall()
            out.update({r[0]: self._journey_row_to_out(r) for r in rows})
        return out

    def _journeys_touching(self, naptans: set) -> list[int]:
        """Journey ids whose stop list contains ANY of naptans (indexed)."""
        if not naptans:
            return []
        marks = ",".join("?" * len(naptans))
        rows = self.conn.execute(
            f"SELECT DISTINCT journey_id FROM journey_stops WHERE naptan IN ({marks})",
            list(naptans)).fetchall()
        return [r[0] for r in rows]

    def _journeys_departing(self, naptan: str, after: str) -> list[int]:
        """Journey ids that depart `naptan` at/after `after` (indexed range seek)."""
        rows = self.conn.execute(
            "SELECT DISTINCT journey_id FROM journey_stop_times "
            "WHERE naptan=? AND dep>=? ORDER BY journey_id", (naptan, after)).fetchall()
        return [r[0] for r in rows]

    def _candidate_journeys(self, naptans_a: set, naptans_b: set, day: str, target_s: str):
        """Yield (journey, idx_a, idx_b) for journeys boarding in A, alighting in B,
        running on day, arriving at B by target_s.

        Stop matching uses FIRST occurrence of a naptan in the stop list
        (next(...)). This is deliberate: plan_direct historically matched the
        first occurrence, and the shared helper unifies both tools on it. It
        only differs from last-occurrence for loop routes whose stop list
        repeats a naptan from A or B — an accepted edge case.

        Candidates are narrowed to journeys touching BOTH stop sets before any
        JSON is fetched: journeys touching B but not A (or vice versa) can never
        yield a plan, and fetching them dominates cost when B resolves to a
        busy/fuzzy set (measured 198ms -> 71ms on a 6,720-journey driving set)."""
        both = set(self._journeys_touching(naptans_a)) & set(self._journeys_touching(naptans_b))
        for jid, j in self._fetch_journeys(sorted(both)).items():
            if day not in j["days"]:
                continue
            idx_a = next((i for i, s in enumerate(j["stops"]) if s["naptan"] in naptans_a), None)
            idx_b = next((i for i, s in enumerate(j["stops"]) if s["naptan"] in naptans_b), None)
            if idx_a is None or idx_b is None or idx_a >= idx_b:
                continue
            arr_b = j["stops"][idx_b].get("arrival")
            if arr_b and arr_b[:8] <= target_s:
                yield j, idx_a, idx_b

    def _data_key(self) -> tuple:
        """Dataset state plan results depend on; changes after any load."""
        return tuple(map(tuple, self.conn.execute(
            "SELECT ds_id, modified FROM loaded_datasets ORDER BY ds_id").fetchall()))

    def find_buses_by_arrival_time(self, naptans_a: set, naptans_b: set,
                                   arrive_by: str, day: str) -> list[dict]:
        target_s = _parse_time(arrive_by)
        if target_s is None:
            return []
        return list(self._find_buses_cached(
            self._data_key(), frozenset(naptans_a), frozenset(naptans_b), day, target_s))

    @functools.lru_cache(maxsize=128)
    def _find_buses_cached(self, key: tuple, a: frozenset, b: frozenset,
                           day: str, target_s: str) -> list[dict]:
        a, b = set(a), set(b)
        out = []
        for j, idx_a, idx_b in self._candidate_journeys(a, b, day, target_s):
            names = self.stop_names_bulk(
                [j["stops"][idx_a]["naptan"], j["stops"][idx_b]["naptan"]])
            out.append({
                "operator": j["operator"], "route": j["route"],
                "direction": j["direction"], "journey_code": j["journey_code"],
                "board_at": names.get(j["stops"][idx_a]["naptan"], "Unknown"),
                "depart": j["stops"][idx_a].get("departure"),
                "alight_at": names.get(j["stops"][idx_b]["naptan"], "Unknown"),
                "arrive": j["stops"][idx_b].get("arrival")})
        out.sort(key=lambda x: x["arrive"] or "")
        return out[:20]

    def plan_direct(self, naptans_a: set, naptans_b: set, day: str,
                    target_s: str) -> list[dict]:
        return list(self._plan_direct_cached(
            self._data_key(), frozenset(naptans_a), frozenset(naptans_b), day, target_s))

    @functools.lru_cache(maxsize=128)
    def _plan_direct_cached(self, key: tuple, a: frozenset, b: frozenset,
                            day: str, target_s: str) -> list[dict]:
        a, b = set(a), set(b)
        plans = []
        for j, idx_a, idx_b in self._candidate_journeys(a, b, day, target_s):
            names = self.stop_names_bulk(
                [j["stops"][idx_a]["naptan"], j["stops"][idx_b]["naptan"]])
            plans.append({
                "type": "direct",
                "legs": [{
                    "operator": j["operator"], "route": j["route"],
                    "board": names.get(j["stops"][idx_a]["naptan"], "Unknown"),
                    "depart": j["stops"][idx_a].get("departure"),
                    "alight": names.get(j["stops"][idx_b]["naptan"], "Unknown"),
                    "arrive": j["stops"][idx_b].get("arrival")}],
                "total_changes": 0})
        return plans

    def plan_one_change(self, naptans_a: set, naptans_b: set, day: str,
                        target_s: str) -> list[dict]:
        return list(self._plan_one_change_cached(
            self._data_key(), frozenset(naptans_a), frozenset(naptans_b), day, target_s))

    @functools.lru_cache(maxsize=128)
    def _plan_one_change_cached(self, key: tuple, a: frozenset, b: frozenset,
                                day: str, target_s: str) -> list[dict]:
        a, b = set(a), set(b)
        plans = []
        for jid1, j1 in self._fetch_journeys(self._journeys_touching(a)).items():
            if day not in j1["days"]:
                continue
            idx_a1 = next((i for i, s in enumerate(j1["stops"]) if s["naptan"] in a), None)
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
                for jid2, j2 in self._fetch_journeys(
                        self._journeys_departing(mid["naptan"], mid_arr[:8])).items():
                    if j2["id"] == j1["id"]:
                        continue
                    if day not in j2["days"]:
                        continue
                    idx_mid2 = next((i for i, s in enumerate(j2["stops"]) if s["naptan"] == mid["naptan"]), None)
                    idx_b2 = next((i for i, s in enumerate(j2["stops"]) if s["naptan"] in b), None)
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
        CREATE TABLE IF NOT EXISTS journey_stops (naptan TEXT, journey_id INT);
        CREATE INDEX IF NOT EXISTS js_n ON journey_stops(naptan);
        CREATE INDEX IF NOT EXISTS js_j ON journey_stops(journey_id);
        CREATE INDEX IF NOT EXISTS j_ds ON journeys(ds_id);
        CREATE INDEX IF NOT EXISTS s2r_n ON stop_to_routes(naptan);
        CREATE INDEX IF NOT EXISTS j_oproute ON journeys(op, route);
        CREATE TABLE IF NOT EXISTS journey_stop_times (
            journey_id INT, seq INT, naptan TEXT, dep TEXT, arr TEXT);
        CREATE INDEX IF NOT EXISTS jst_n ON journey_stop_times(naptan, dep);
        CREATE INDEX IF NOT EXISTS jst_j ON journey_stop_times(journey_id, seq);
        CREATE VIRTUAL TABLE IF NOT EXISTS stops_fts USING fts5(
            name, naptan UNINDEXED, tokenize='unicode61');
        CREATE TRIGGER IF NOT EXISTS stops_fts_ai AFTER INSERT ON stops BEGIN
            INSERT INTO stops_fts(rowid, name, naptan) VALUES (new.rowid, new.name, new.naptan);
        END;
        CREATE TRIGGER IF NOT EXISTS stops_fts_ad AFTER DELETE ON stops BEGIN
            DELETE FROM stops_fts WHERE rowid = old.rowid;
        END;
        CREATE TRIGGER IF NOT EXISTS stops_fts_au AFTER UPDATE ON stops BEGIN
            UPDATE stops_fts SET name = new.name, naptan = new.naptan WHERE rowid = old.rowid;
        END;
        CREATE VIRTUAL TABLE IF NOT EXISTS routes_fts USING fts5(
            num, op, key UNINDEXED, tokenize='unicode61');
        CREATE TRIGGER IF NOT EXISTS routes_fts_ai AFTER INSERT ON routes BEGIN
            INSERT INTO routes_fts(rowid, num, op, key) VALUES (new.rowid, new.num, new.op, new.key);
        END;
        CREATE TRIGGER IF NOT EXISTS routes_fts_au AFTER UPDATE ON routes BEGIN
            UPDATE routes_fts SET num = new.num, op = new.op WHERE rowid = old.rowid;
        END;
        CREATE TRIGGER IF NOT EXISTS routes_fts_ad AFTER DELETE ON routes BEGIN
            DELETE FROM routes_fts WHERE rowid = old.rowid;
        END;
        """)
        # One-time backfill: journey_stops only gets populated by add_journey,
        # so a DB upgraded in place (journeys already present) would have an
        # empty index table and every stop->journey query would silently return
        # nothing. Backfill from the stored stops JSON once, keyed on a meta
        # flag so it never re-runs.
        if not self.conn.execute(
                "SELECT 1 FROM meta WHERE k='journey_stops_backfilled'").fetchone():
            self.conn.execute(
                "INSERT INTO journey_stops (naptan, journey_id) "
                "SELECT json_extract(value, '$.naptan'), j.id "
                "FROM journeys j, json_each(j.json, '$.stops')")
            self.conn.execute(
                "INSERT OR REPLACE INTO meta VALUES ('journey_stops_backfilled', '1')")
        # One-time backfill: stops_fts only gets populated by the triggers, so
        # a DB upgraded in place (stops already present) would have an empty
        # FTS index and every FTS search would silently return nothing.
        # Backfill from stops once, keyed on a meta flag so it never re-runs.
        if not self.conn.execute(
                "SELECT 1 FROM meta WHERE k='stops_fts_backfilled'").fetchone():
            self.conn.execute(
                "INSERT OR REPLACE INTO stops_fts(rowid, name, naptan) "
                "SELECT rowid, name, naptan FROM stops")
            self.conn.execute(
                "INSERT OR REPLACE INTO meta VALUES ('stops_fts_backfilled', '1')")
        # One-time backfill: routes_fts only gets populated by the triggers, so
        # a DB upgraded in place (routes already present) would have an empty
        # FTS index and every route search would silently return nothing.
        # Backfill from routes once, keyed on a meta flag so it never re-runs.
        if not self.conn.execute(
                "SELECT 1 FROM meta WHERE k='routes_fts_backfilled'").fetchone():
            self.conn.execute(
                "INSERT INTO routes_fts(rowid, num, op, key) "
                "SELECT rowid, num, op, key FROM routes")
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('routes_fts_backfilled', '1')")
        # journey_stop_times v2 (seq + arr): populated by add_journey; a DB
        # upgraded in place (pre-v2 rows, or an empty table with journeys
        # present) is rebuilt from the stored stops JSON once. dep follows
        # COALESCE(departure, arrival) to match add_journey's write side;
        # arr is the raw arrival (NULL where the timetable publishes none).
        if not self.conn.execute(
                "SELECT 1 FROM meta WHERE k='journey_stop_times_backfilled'").fetchone():
            cols = {r[1] for r in self.conn.execute(
                "PRAGMA table_info(journey_stop_times)")}
            for c in ("seq", "arr"):
                if c not in cols:
                    self.conn.execute(
                        f"ALTER TABLE journey_stop_times ADD COLUMN {c} INT"
                        if c == "seq" else
                        f"ALTER TABLE journey_stop_times ADD COLUMN {c} TEXT")
            self.conn.execute("DELETE FROM journey_stop_times")
            self.conn.execute(
                "INSERT INTO journey_stop_times (journey_id, seq, naptan, dep, arr) "
                "SELECT j.id, e.key, "
                "       json_extract(e.value, '$.naptan'), "
                "       COALESCE(json_extract(e.value, '$.departure'),"
                "                json_extract(e.value, '$.arrival')), "
                "       json_extract(e.value, '$.arrival') "
                "FROM journeys j, json_each(j.json, '$.stops') e")
            self.conn.execute(
                "INSERT OR REPLACE INTO meta VALUES"
                " ('journey_stop_times_backfilled', '1')")
        # journeys.days_mask: SQL-level day filter (7-bit, mon=1 .. sun=64).
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(journeys)")}
        if "days_mask" not in cols:
            self.conn.execute(
                "ALTER TABLE journeys ADD COLUMN days_mask INT DEFAULT 0")
        if not self.conn.execute(
                "SELECT 1 FROM meta WHERE k='days_mask_backfilled'").fetchone():
            for jid, days_json in self.conn.execute(
                    "SELECT id, days FROM journeys WHERE days_mask=0").fetchall():
                try:
                    day_set = set(json.loads(days_json or "[]"))
                except (ValueError, TypeError):
                    day_set = set()
                self.conn.execute(
                    "UPDATE journeys SET days_mask=? WHERE id=?",
                    (_days_mask(day_set), jid))
            self.conn.execute(
                "INSERT OR REPLACE INTO meta VALUES ('days_mask_backfilled', '1')")
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
        if stop_list:
            marks = ",".join("?" * len(stop_list))
            self.conn.execute(
                f"DELETE FROM stop_to_routes WHERE key=? AND naptan NOT IN ({marks})",
                [key] + list(stop_list))
        else:
            self.conn.execute("DELETE FROM stop_to_routes WHERE key=?", (key,))

    def add_journey(self, op: str, num: str, direction: str, code: str,
                    days: set, stops: list, ds_id: int):
        j = {"operator": op, "route_num": num, "direction": direction,
             "journey_code": code, "dataset_id": ds_id,
             "days": sorted(days), "stops": [dict(st) for st in stops]}
        cur = self.conn.execute(
            "INSERT INTO journeys (ds_id, op, route, direction, code, days, days_mask, json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ds_id, op, num, direction, code, json.dumps(sorted(days)),
             _days_mask(days), json.dumps(j)))
        self.conn.executemany(
            "INSERT INTO journey_stops VALUES (?,?)",
            [(s["naptan"], cur.lastrowid) for s in stops])
        self.conn.executemany(
            "INSERT INTO journey_stop_times (naptan, journey_id, seq, dep, arr) "
            "VALUES (?,?,?,?,?)",
            [(s["naptan"], cur.lastrowid, i,
              s.get("departure") or s.get("arrival"), s.get("arrival"))
             for i, s in enumerate(stops)])

    def commit(self):
        self.conn.commit()

    def mark_dataset_loaded(self, ds_id: int, modified: Optional[str],
                            operator: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO loaded_datasets VALUES (?,?,?)",
            (ds_id, modified, operator))

    def last_full_sweep(self) -> Optional[str]:
        row = self.conn.execute(
            "SELECT v FROM meta WHERE k='last_full_sweep'").fetchone()
        return row[0] if row and row[0] else None

    def set_last_full_sweep(self, iso: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO meta VALUES ('last_full_sweep', ?)", (iso,))

    def full_sweep_due(self, max_age_days: int = 7) -> bool:
        """True when a full (unfiltered) catalogue sweep is overdue.

        Withdrawal purges need the full catalogue, so they run on full sweeps
        only; otherwise a filtered delta sweep is enough until this watermark
        ages out (default 7 days)."""
        iso = self.last_full_sweep()
        ts = _parse_iso(iso) if iso else None
        if ts is None:
            return True
        return (datetime.now(timezone.utc) - ts).total_seconds() > max_age_days * 86400

    def set_last_refresh(self, iso: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO meta VALUES ('last_refresh', ?)", (iso,))

    def optimize_fts(self):
        """Merge/coalesce FTS b-trees after bulk loads (query-speed tail win)."""
        self.conn.execute("INSERT INTO stops_fts(stops_fts) VALUES('optimize')")
        self.conn.execute("INSERT INTO routes_fts(routes_fts) VALUES('optimize')")

    def discard_untagged_for(self, op: str, route_num: str):
        """Remove pre-SQLite-era journeys (ds_id=0) for one operator+route.

        Used when a legacy (untagged) dataset is refreshed: the fresh tagged
        insert supersedes the old rows, which cannot be purged by ds_id.
        """
        self.conn.execute(
            "DELETE FROM journey_stops WHERE journey_id IN "
            "(SELECT id FROM journeys WHERE ds_id=0 AND op=? AND route=?)",
            (op, route_num))
        self.conn.execute(
            "DELETE FROM journey_stop_times WHERE journey_id IN "
            "(SELECT id FROM journeys WHERE ds_id=0 AND op=? AND route=?)",
            (op, route_num))
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
        self.conn.execute(
            "DELETE FROM journey_stops WHERE journey_id IN "
            "(SELECT id FROM journeys WHERE ds_id=?)", (ds_id,))
        self.conn.execute(
            "DELETE FROM journey_stop_times WHERE journey_id IN "
            "(SELECT id FROM journeys WHERE ds_id=?)", (ds_id,))
        self.conn.execute("DELETE FROM journeys WHERE ds_id=?", (ds_id,))
        # Surviving route keys via an index-only scan on journeys(op, route)
        # (j_oproute index) instead of a full-table scan + expression eval.
        self.conn.execute(
            "DELETE FROM stop_to_routes WHERE key NOT IN ("
            "SELECT op || '|' || route FROM (SELECT DISTINCT op, route FROM journeys))")
        self.conn.execute("DELETE FROM loaded_datasets WHERE ds_id=?", (ds_id,))