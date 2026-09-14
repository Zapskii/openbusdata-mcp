"""SQLite-backed timetable store: disk-backed index for openbusdata-mcp.

Replaces the in-memory timetable index for query tools. Same tool output
shapes, but memory is O(query) instead of O(entire UK timetable).
"""
import bisect
import functools
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional

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


def _split_nocs(text) -> list[str]:
    """Parse a loaded_datasets.noc value (comma-joined, as stored) into a
    de-duplicated list, preserving order."""
    seen, out = set(), []
    for noc in (text or "").split(","):
        noc = noc.strip()
        if noc and noc not in seen:
            seen.add(noc)
            out.append(noc)
    return out


class Candidate(NamedTuple):
    """One A-boards-before-B journey resolved entirely in SQL."""
    journey_id: int
    operator: str
    route: str
    direction: str
    code: str
    naptan_a: str
    naptan_b: str
    depart_a: Optional[str]
    arrive_b: Optional[str]
    seq_a: int
    seq_b: int


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
        """Matches across ALL datasets for (operator, route) — several
        regions can publish the same name+number (per-ds route rows), and
        each sibling is reported with its ds_id rather than deduped."""
        fts = _fts_query(f"{route} {operator}".strip())
        if fts:
            rows = self.conn.execute(
                "SELECT r.op, r.num, r.directions, r.stops, r.ds_id FROM routes_fts f "
                "JOIN routes r ON r.rowid = f.rowid "
                "WHERE routes_fts MATCH ? ORDER BY rank LIMIT 50", (fts,)).fetchall()
        else:
            rows = []
        if not rows:  # FTS returned nothing (or query had no tokenizable terms) -> LIKE substring fallback
            q = (f"%{route.lower()}%", f"%{operator.lower()}%")
            rows = self.conn.execute(
                "SELECT op, num, directions, stops, ds_id FROM routes "
                "WHERE LOWER(num) LIKE ? AND LOWER(op) LIKE ?", q).fetchall()
        matches = []
        seen = set()
        for op, num, directions, stops, ds_id in rows:
            dirs = json.loads(directions)
            if direction and direction.lower() not in [d.lower() for d in dirs]:
                continue
            stop_list = json.loads(stops)
            # FTS can surface several rows of one dataset's pack; collapse
            # those, but keep every dataset sibling distinct.
            marker = (op, num, ds_id)
            if marker in seen:
                continue
            seen.add(marker)
            names = self.stop_names_bulk(stop_list)
            matches.append({
                "operator": op, "route": num, "ds_id": ds_id,
                "directions": sorted(dirs),
                "stops": [{"naptan": n, "name": names.get(n, "Unknown")}
                          for n in stop_list]})
        return matches

    # -- journeys -----------------------------------------------------------
    def _candidate_journeys(self, naptans_a: set, naptans_b: set, day: str,
                            target_s: str):
        """Yield Candidates for journeys boarding in A, alighting in B, running
        on day, arriving at B by target_s — resolved in one indexed SQL query.

        First-occurrence semantics preserved: MIN(seq) per journey reproduces
        the previous Python next() scan. First-occurrence of a naptan in a
        loop route's stop list differs from last-occurrence only for loop
        routes repeating a naptan from A or B — an accepted edge case.
        Variable budget: <= 400 + 400 + 2 = 802 < 999 (see MAX_RESOLVE)."""
        if not naptans_a or not naptans_b:
            return
        a_marks = ",".join("?" * len(naptans_a))
        b_marks = ",".join("?" * len(naptans_b))
        rows = self.conn.execute(f"""
            SELECT j.id, j.op, j.route, j.direction, j.code,
                   sa.naptan, sb.naptan, sa.dep, sb.arr, aa.seq, bb.seq
            FROM journeys j
            JOIN (SELECT journey_id, MIN(seq) AS seq FROM journey_stop_times
                  WHERE naptan IN ({a_marks}) GROUP BY journey_id) aa
              ON aa.journey_id = j.id
            JOIN (SELECT journey_id, MIN(seq) AS seq FROM journey_stop_times
                  WHERE naptan IN ({b_marks}) GROUP BY journey_id) bb
              ON bb.journey_id = j.id
            JOIN journey_stop_times sa ON sa.journey_id = j.id AND sa.seq = aa.seq
            JOIN journey_stop_times sb ON sb.journey_id = j.id AND sb.seq = bb.seq
            WHERE j.days_mask & ? != 0
              AND aa.seq < bb.seq
              AND sb.arr IS NOT NULL AND sb.arr <= ?
        """, list(naptans_a) + list(naptans_b)
             + [DAY_BITS.get(day, 0), target_s]).fetchall()
        for (jid, op, route, direction, code, na, nb, dep_a, arr_b,
             seq_a, seq_b) in rows:
            yield Candidate(jid, op, route, direction, code, na, nb,
                            dep_a, arr_b, seq_a, seq_b)

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
        cands = list(self._candidate_journeys(set(a), set(b), day, target_s))
        names = self.stop_names_bulk(
            [c.naptan_a for c in cands] + [c.naptan_b for c in cands])
        out = [{
            "operator": c.operator, "route": c.route,
            "direction": c.direction, "journey_code": c.code,
            "board_at": names.get(c.naptan_a, "Unknown"),
            "depart": c.depart_a,
            "alight_at": names.get(c.naptan_b, "Unknown"),
            "arrive": c.arrive_b} for c in cands]
        out.sort(key=lambda x: x["arrive"] or "")
        return out[:20]

    def plan_direct(self, naptans_a: set, naptans_b: set, day: str,
                    target_s: str) -> list[dict]:
        return list(self._plan_direct_cached(
            self._data_key(), frozenset(naptans_a), frozenset(naptans_b), day, target_s))

    @functools.lru_cache(maxsize=128)
    def _plan_direct_cached(self, key: tuple, a: frozenset, b: frozenset,
                            day: str, target_s: str) -> list[dict]:
        cands = list(self._candidate_journeys(set(a), set(b), day, target_s))
        names = self.stop_names_bulk(
            [c.naptan_a for c in cands] + [c.naptan_b for c in cands])
        return [{
            "type": "direct",
            "legs": [{
                "operator": c.operator, "route": c.route,
                "board": names.get(c.naptan_a, "Unknown"),
                "depart": c.depart_a,
                "alight": names.get(c.naptan_b, "Unknown"),
                "arrive": c.arrive_b}],
            "total_changes": 0} for c in cands]

    def plan_one_change(self, naptans_a: set, naptans_b: set, day: str,
                        target_s: str) -> list[dict]:
        return list(self._plan_one_change_cached(
            self._data_key(), frozenset(naptans_a), frozenset(naptans_b), day, target_s))

    @functools.lru_cache(maxsize=128)
    def _plan_one_change_cached(self, key: tuple, a: frozenset, b: frozenset,
                                day: str, target_s: str) -> list[dict]:
        """One-change plans, staged so the worst case stays seconds.

        The previous single SQL self-join was driven from the A-side: for
        every leg-1 journey x every downstream stop it fanned out over all
        later departures at that stop (jst_n) plus a correlated MIN(seq)
        subquery, and its ORDER BY + LIMIT 2000 only pruned AFTER the cross
        product — a request with an empty result set (or a busy corridor)
        ground the 43M-row table for minutes and head-of-line-blocked every
        other tool. Staged version:
          1) mid candidates = stops downstream of an A-journey with a
             published arrival AND before the first B stop on some
             day-running journey; one bounded DISTINCT query per side,
             intersected in Python. Empty intersection -> instant "none".
          2) per-mid leg-1 rides (board at FIRST A occurrence, sa.dep
             published, alight mid with arr published) and leg-2 rides
             (board at FIRST mid occurrence, alight at FIRST B occurrence,
             sb.arr <= target); paired on dep >= leg-1 mid arrival, j2 != j1.
          3) streaming top-2000 pairs by final arrival — the old
             ORDER BY sb.arr + LIMIT 2000 truncation, heap-bounded instead
             of sort-bounded. The tool-level dedupe + top-15 cap follow.
        ponytail: pairing is O(leg1 x leg2) per mid in Python; push it back
        into SQL with a mid temp table if profiling ever shows it hot."""
        a, b = set(a), set(b)
        if not a or not b:
            return []
        a_marks = ",".join("?" * len(a))
        b_marks = ",".join("?" * len(b))
        mask = DAY_BITS.get(day, 0)  # malformed day -> no plans (matches _candidate_journeys)
        board = ("SELECT journey_id, MIN(seq) AS seq FROM journey_stop_times "
                 "WHERE naptan IN ({marks}) GROUP BY journey_id")

        # 1) transfer-stop candidates, one semi-join per side, intersected.
        mids_a = {r[0] for r in self.conn.execute(f"""
            SELECT DISTINCT sm.naptan
            FROM ({board.format(marks=a_marks)}) aa
            JOIN journeys j1 ON j1.id = aa.journey_id AND j1.days_mask & ? != 0
            JOIN journey_stop_times sm ON sm.journey_id = aa.journey_id
              AND sm.seq > aa.seq AND sm.arr IS NOT NULL
        """, list(a) + [mask]).fetchall()}
        mids_b = {r[0] for r in self.conn.execute(f"""
            SELECT DISTINCT sm2.naptan
            FROM ({board.format(marks=b_marks)}) bb
            JOIN journeys j2 ON j2.id = bb.journey_id AND j2.days_mask & ? != 0
            JOIN journey_stop_times sm2 ON sm2.journey_id = bb.journey_id
              AND sm2.seq < bb.seq
        """, list(b) + [mask]).fetchall()}
        mids = mids_a & mids_b
        if not mids:
            return []

        # 2a) leg-1 rides into any candidate mid.
        mid_marks = ",".join("?" * len(mids))
        leg1 = self.conn.execute(f"""
            SELECT j1.id, j1.op, j1.route, j1.direction, j1.code,
                   sa.naptan, sa.dep, sm.naptan, sm.arr
            FROM ({board.format(marks=a_marks)}) aa
            JOIN journeys j1 ON j1.id = aa.journey_id AND j1.days_mask & ? != 0
            JOIN journey_stop_times sa ON sa.journey_id = j1.id
              AND sa.seq = aa.seq
            JOIN journey_stop_times sm ON sm.journey_id = j1.id
              AND sm.seq > aa.seq AND sm.arr IS NOT NULL
              AND sm.naptan IN ({mid_marks})
            WHERE sa.dep IS NOT NULL
            ORDER BY sm.arr
            LIMIT 2000
        """, list(a) + [mask] + list(mids)).fetchall()
        if not leg1:
            return []
        leg1_by_mid = {}
        for row in leg1:
            leg1_by_mid.setdefault(row[7], []).append(row)

        # 2b) leg-2 rides out of each mid, paired against that mid's leg-1
        #     arrivals. Keep the earliest-arriving 2000 pairs: a bisect list
        #     ascending by final arrival, evicting from the tail (times are
        #     strings, so no heap-negation trick is available).
        top: list = []  # ascending by arr_b; tail = worst plan
        for mid in mids:
            leg2 = self.conn.execute(f"""
                SELECT j2.id, j2.op, j2.route, j2.code,
                       sm2.dep, sb.naptan, sb.arr
                FROM (SELECT journey_id, MIN(seq) AS seq
                      FROM journey_stop_times WHERE naptan = ?
                      GROUP BY journey_id) fo
                JOIN journey_stop_times sm2 ON sm2.journey_id = fo.journey_id
                  AND sm2.naptan = ? AND sm2.seq = fo.seq
                JOIN journeys j2 ON j2.id = sm2.journey_id
                  AND j2.days_mask & ? != 0
                JOIN ({board.format(marks=b_marks)}) bb
                  ON bb.journey_id = j2.id
                JOIN journey_stop_times sb ON sb.journey_id = j2.id
                  AND sb.seq = bb.seq
                WHERE fo.seq < bb.seq AND sb.arr IS NOT NULL AND sb.arr <= ?
            """, [mid, mid, mask] + list(b) + [target_s]).fetchall()
            if not leg2:
                continue
            leg2.sort(key=lambda r: r[4] or "")
            deps = [r[4] or "" for r in leg2]
            for (jid1, op1, route1, dir1, code1, na, dep_a, _mid, mid_arr) \
                    in leg1_by_mid.get(mid, ()):
                lo = bisect.bisect_left(deps, mid_arr)
                for (jid2, op2, route2, code2, dep2, nb, arr_b) \
                        in leg2[lo:]:
                    if jid2 == jid1 or dep2 is None:
                        continue
                    row = (op1, route1, na, dep_a, mid, mid_arr,
                           op2, route2, code2, dep2, nb, arr_b)
                    if len(top) < 2000:
                        bisect.insort(top, row, key=lambda p: p[11])
                    elif arr_b < top[-1][11]:
                        top.pop()
                        bisect.insort(top, row, key=lambda p: p[11])
        if not top:
            return []
        rows = top  # already ascending by final arrival

        naptans = {r[2] for r in rows} | {r[4] for r in rows} | {r[10] for r in rows}
        names = self.stop_names_bulk(list(naptans))
        plans = []
        for (op1, route1, na, dep_a, mid, mid_arr,
             op2, route2, code2, dep2, nb, arr_b) in rows:
            plans.append({
                "type": "change",
                "legs": [
                    {"operator": op1, "route": route1,
                     "board": names.get(na, "Unknown"), "depart": dep_a,
                     "alight": names.get(mid, "Unknown"), "arrive": mid_arr},
                    {"operator": op2, "route": route2,
                     "board": names.get(mid, "Unknown"), "depart": dep2,
                     "alight": names.get(nb, "Unknown"), "arrive": arr_b}],
                "total_changes": 1})
        return plans

    def next_departures(self, naptans: set, day: str, from_time: str,
                        limit: int = 20) -> list:
        """Next scheduled departures from any of the given stops on `day`,
        at/after from_time ('HH:MM:SS'), ordered by time. Destination is the
        name of each journey's final stop, falling back to 'Unknown' when that
        stop has no `stops` row OR a row with a blank name (R14's COALESCE
        plus P18's NULLIF). Rides jst_n (naptan, dep) for the candidate scan
        and jst_j (journey_id) for the correlated MAX(seq)."""
        if not naptans:
            return []
        marks = ",".join("?" * len(naptans))
        rows = self.conn.execute(f"""
            SELECT j.op, j.route, j.direction, j.code,
                   MIN(jst.dep) AS dep,
                   COALESCE(NULLIF(s.name, ''), 'Unknown') AS dest
            FROM journey_stop_times jst
            JOIN journeys j ON j.id = jst.journey_id AND j.days_mask & ? != 0
            JOIN journey_stop_times lastj ON lastj.journey_id = jst.journey_id
                 AND lastj.seq = (SELECT MAX(seq) FROM journey_stop_times
                                  WHERE journey_id = jst.journey_id)
            LEFT JOIN stops s ON s.naptan = lastj.naptan
            WHERE jst.naptan IN ({marks}) AND jst.dep IS NOT NULL AND jst.dep >= ?
            GROUP BY jst.journey_id
            ORDER BY dep
            LIMIT ?
        """, [DAY_BITS.get(day, 0)] + list(naptans) + [from_time, limit]).fetchall()
        return [{"operator": r[0], "route": r[1], "direction": r[2],
                 "journey_code": r[3], "depart": r[4], "destination": r[5]}
                for r in rows]

    def resolve_operator(self, operator: str, route: str
                         ) -> Optional[tuple[str, list[str]]]:
        """Resolve a caller's operator_ref to (index operator name, NOC list)
        for `route`, or None when no route matches under either reading.

        The two identifiers live in different key spaces: the index is keyed by
        the BODS dataset `operatorName` (upsert_route's `key = op|num`), while
        the SIRI-VM datafeed wants a NOC. Accepting either here is what lets
        estimate_live_eta serve both backends from one input.

        The NOC list is empty for an index built before NOCs were recorded —
        callers must report that rather than sending the name to the datafeed,
        which rejects it with a 400."""
        rows = self.conn.execute(
            "SELECT DISTINCT r.op, d.noc FROM routes r "
            "LEFT JOIN loaded_datasets d ON d.ds_id = r.ds_id "
            "WHERE r.op=? AND r.num=?", (operator, route)).fetchall()
        if rows:
            # One (op, num) can carry several datasets (Arriva regions publish
            # as one name), but they share the dataset-level operatorName and
            # NOC list, so the resolution is identical across siblings.
            return rows[0][0], _split_nocs(rows[0][1])
        # Not a known name: read the input as a NOC. A NOC belongs to a
        # dataset; its routes carry that dataset's operator name. The
        # ','||noc||',' guard makes the LIKE an exact membership test.
        rows = self.conn.execute(
            "SELECT DISTINCT r.op, d.noc FROM routes r "
            "JOIN loaded_datasets d ON d.ds_id = r.ds_id "
            "WHERE r.num=? "
            "  AND (','||REPLACE(d.noc,' ','')||',') LIKE '%,'||?||',%'",
            (route, operator)).fetchall()
        # One route number can be published by several operators, so a NOC
        # matching more than one leaves the index route undetermined. Report
        # that as a miss rather than picking one and answering from the wrong
        # operator's timetable.
        if len({r[0] for r in rows}) != 1:
            return None
        return rows[0][0], _split_nocs(rows[0][1])

    def route_stop_coords(self, operator: str, route: str,
                          prefer_stops: Optional[set] = None) -> Optional[list]:
        """The route's ordered stop list with names and coordinates (the
        routes.stops JSON joined to stops). None when the route is unknown.
        The list is direction-merged (upsert_route keeps the longest sequence).

        Route rows are per-dataset, and one (operator, route) can carry
        several region packs (Arriva regions publish as one operatorName).
        With several siblings, `prefer_stops` (when given) picks the row
        whose stop list actually contains one of the queried stops — the
        region the caller is asking about. Without a preference the choice
        is ambiguous and the caller reports it."""
        rows = self.conn.execute(
            "SELECT stops FROM routes WHERE op=? AND num=?",
            (operator, route)).fetchall()
        if not rows:
            return None
        stop_lists = [json.loads(r[0]) for r in rows]
        chosen = stop_lists[0]
        if len(stop_lists) > 1 and prefer_stops:
            for lst in stop_lists:
                if prefer_stops & set(lst):
                    chosen = lst
                    break
        naptans = chosen
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
        CREATE TABLE IF NOT EXISTS stop_to_routes (
            naptan TEXT, key TEXT, PRIMARY KEY (naptan, key));
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS loaded_datasets (
            ds_id INTEGER PRIMARY KEY, modified TEXT, operator TEXT,
            noc TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS journey_stops (naptan TEXT, journey_id INT);
        CREATE INDEX IF NOT EXISTS js_n ON journey_stops(naptan);
        CREATE INDEX IF NOT EXISTS js_j ON journey_stops(journey_id);
        CREATE INDEX IF NOT EXISTS j_ds ON journeys(ds_id);
        CREATE INDEX IF NOT EXISTS s2r_n ON stop_to_routes(naptan);
        CREATE INDEX IF NOT EXISTS j_oproute ON journeys(op, route);
        CREATE TABLE IF NOT EXISTS journey_stop_times (
            journey_id INT, seq INT, naptan TEXT, dep TEXT, arr TEXT);
        CREATE INDEX IF NOT EXISTS jst_n ON journey_stop_times(naptan, dep);
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
        # The gate is COLUMN-driven, not flag-driven: a legacy DB already
        # carries the backfill flag ('1', set by the v1 backfill), so an
        # existing flag must never skip a missing-column rebuild.
        cols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(journey_stop_times)")}
        if ("seq" not in cols or "arr" not in cols or not self.conn.execute(
                "SELECT 1 FROM meta WHERE k='journey_stop_times_backfilled'"
            ).fetchone()):
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
                " ('journey_stop_times_backfilled', '2')")
        # jst_j indexes (journey_id, seq): created here — AFTER the ALTERs
        # above have guaranteed the seq column exists, because a legacy v1
        # table (naptan/journey_id/dep only) would make an in-script
        # CREATE INDEX fail with "no such column: seq" at startup.
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS jst_j ON journey_stop_times(journey_id, seq)")
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
        # Migration: route keys move from global `op|num` (one row per
        # name+number worldwide — let one region's pack evict another's,
        # e.g. every Arriva region publishes as "Arriva UK Bus") to
        # per-dataset `op|num|ds_id`. Column-driven gate (like the v2
        # journey_stop_times backfill): a legacy key parses as exactly one
        # '|' with a non-empty, parseable ds_id on the right.
        legacy = self.conn.execute(
            "SELECT COUNT(*) FROM routes WHERE "
            "(LENGTH(key) - LENGTH(REPLACE(key, '|', ''))) = 1").fetchone()[0]
        # stop_to_routes without a real PK duplicates rows again on every
        # re-upsert (INSERT OR IGNORE is then a no-op guard). A legacy table
        # shape (no PK in its CREATE) is rebuilt with the PK in the same
        # migration — dedupe first so the new PK can take.
        s2r_cols = {(r[1], r[5]) for r in self.conn.execute(
            "PRAGMA table_info(stop_to_routes)")}
        s2r_legacy = s2r_cols != {("naptan", 1), ("key", 2)}
        if legacy or s2r_legacy:
            n_fts = self.conn.execute(
                "SELECT COUNT(*) FROM routes").fetchone()[0]
            # Old→new mapping staged in a temp table BEFORE any rekey: the
            # UPDATE must not read the table it is rewriting (self-referential
            # UPDATE behavior on the same btree is not something to bet an
            # 11 GB index on). Single-ds rows keep their recorded ds_id
            # exactly. Multi-ds families keep the row under the ds_id that
            # owns it (its longest-writer; that region's stop list survives)
            # and DROP the sibling regions' rows — their stop lists are
            # already corrupted by the merge, so re-upserting each affected
            # dataset (see loader) is what restores the other regions.
            self.conn.execute(
                "CREATE TEMP TABLE rekey_map "
                "(old_key TEXT PRIMARY KEY, new_key TEXT UNIQUE)")
            self.conn.execute(
                "INSERT INTO rekey_map (old_key, new_key) "
                "SELECT key, op || '|' || num || '|' || ds_id FROM routes "
                "WHERE (LENGTH(key) - LENGTH(REPLACE(key, '|', ''))) = 1")
            self.conn.execute(
                "UPDATE routes SET key = "
                "(SELECT new_key FROM rekey_map WHERE old_key = routes.key) "
                "WHERE key IN (SELECT old_key FROM rekey_map)")
            self.conn.execute("DELETE FROM routes WHERE ds_id = 0")
            # s2r keys follow the rekey; rows with no matching route row are
            # orphans (stale refs) and are swept below rather than rekeyed.
            self.conn.execute(
                "UPDATE OR IGNORE stop_to_routes SET key = "
                "(SELECT new_key FROM rekey_map WHERE old_key = stop_to_routes.key) "
                "WHERE key IN (SELECT old_key FROM rekey_map)")
            self.conn.execute("DROP TABLE rekey_map")
            self.conn.execute(
                "DELETE FROM stop_to_routes WHERE key LIKE '%|0'")
            # Duplicates were legal under the old schema (INSERT OR IGNORE
            # was a no-op guard) — collapse them.
            self.conn.execute(
                "DELETE FROM stop_to_routes WHERE rowid NOT IN "
                "(SELECT MIN(rowid) FROM stop_to_routes GROUP BY naptan, key)")
            # Orphan sweep: refs to keys no route row carries.
            self.conn.execute(
                "DELETE FROM stop_to_routes WHERE key NOT IN "
                "(SELECT key FROM routes)")
            # Legacy table shape (no PK): CREATE TABLE IF NOT EXISTS cannot
            # retrofit one, so INSERT OR IGNORE stays a no-op guard and
            # duplicates re-accumulate on every re-upsert. Rebuild the table
            # with the PK — one DISTINCT copy collapses the dupes.
            if s2r_legacy:
                self.conn.execute("DROP INDEX IF EXISTS s2r_n")
                self.conn.execute(
                    "ALTER TABLE stop_to_routes RENAME TO stop_to_routes_legacy")
                self.conn.execute(
                    "CREATE TABLE stop_to_routes ("
                    "naptan TEXT, key TEXT, PRIMARY KEY (naptan, key))")
                self.conn.execute(
                    "INSERT INTO stop_to_routes (naptan, key) "
                    "SELECT DISTINCT naptan, key FROM stop_to_routes_legacy")
                self.conn.execute("DROP TABLE stop_to_routes_legacy")
                self.conn.execute(
                    "CREATE INDEX IF NOT EXISTS s2r_n ON stop_to_routes(naptan)")
            # Rebuild the FTS index: triggers only fire on subsequent DML.
            self.conn.execute("DELETE FROM routes_fts")
            self.conn.execute(
                "INSERT INTO routes_fts(rowid, num, op, key) "
                "SELECT rowid, num, op, key FROM routes")
            self.conn.execute(
                "INSERT OR REPLACE INTO meta VALUES ('routes_rekeyed', '1')")
            print(f"[OpenBusData MCP] Route-key migration: rekeyed {n_fts} "
                  "route rows to per-dataset keys (op|num|ds_id); reload "
                  "affected datasets to restore their region siblings.")
        # Legacy DBs: loaded_datasets predates NOC storage. Old rows keep the
        # '' default, so resolve_operator reports "no NOC on record" (the
        # caller then asks for a force_refresh) rather than inventing one.
        cols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(loaded_datasets)")}
        if "noc" not in cols:
            self.conn.execute(
                "ALTER TABLE loaded_datasets ADD COLUMN noc TEXT DEFAULT ''")
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
                        "INSERT OR IGNORE INTO loaded_datasets "
                        "(ds_id, modified, operator) VALUES (?,?,?)",
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
        # Conflict update fills blanks (and never blanks a value): name wins
        # only when non-empty, coords win only when the new pair is present.
        # stops are shared across datasets and survive force_refresh's purge
        # (discard_dataset), so this upsert is what backfills coordinates into
        # pre-existing rows when a refreshed dataset re-reports its stops.
        self.conn.execute(
            "INSERT INTO stops (naptan, name, lat, lon) VALUES (?,?,?,?) "
            "ON CONFLICT(naptan) DO UPDATE SET "
            "name=CASE WHEN excluded.name != '' THEN excluded.name "
            "ELSE stops.name END, "
            "lat=COALESCE(excluded.lat, stops.lat), "
            "lon=COALESCE(excluded.lon, stops.lon)",
            (naptan, name, lat, lon))

    def upsert_route(self, op: str, num: str, directions: set, stop_list: list,
                     ds_id: int):
        """One row per (operator, route number, dataset).

        The BODS `operatorName` is not region-unique (every Arriva region
        publishes as "Arriva UK Bus", with byte-identical operator-level NOC
        lists), so keying `op|num` globally let one region's route row evict
        another's. Merged state (union directions, longest stop sequence)
        applies only WITHIN a dataset — the loader's repeated calls for one
        pack are registration variants across its files. Across datasets the
        rows are siblings, never merged.
        """
        key = f"{op}|{num}|{ds_id}"
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
                            operator: str, noc: str = ""):
        self.conn.execute(
            "INSERT OR REPLACE INTO loaded_datasets (ds_id, modified, operator, noc) "
            "VALUES (?,?,?,?)",
            (ds_id, modified, operator, noc))

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

    def discard_dataset(self, ds_id: int):
        """Purge one dataset's journeys + route rows exactly (no commit).

        Journeys are tagged with their source dataset, so they purge exactly.
        Route rows are per-dataset too (key = op|num|ds_id), so their purge is
        exact as well: this dataset's rows and stop_to_routes refs go, and
        sibling datasets serving the same op|num keep theirs. The caller owns
        the transaction: purge + rewrite must commit together.
        """
        self.conn.execute(
            "DELETE FROM journey_stops WHERE journey_id IN "
            "(SELECT id FROM journeys WHERE ds_id=?)", (ds_id,))
        self.conn.execute(
            "DELETE FROM journey_stop_times WHERE journey_id IN "
            "(SELECT id FROM journeys WHERE ds_id=?)", (ds_id,))
        self.conn.execute("DELETE FROM journeys WHERE ds_id=?", (ds_id,))
        # Route rows are per-dataset (key = op|num|ds_id), so the route purge
        # is exact: this dataset's rows go, sibling datasets serving the same
        # op|num keep theirs. No merged state, so the old "leave rows in
        # place and let them self-heal" workaround is gone.
        self.conn.execute(
            "DELETE FROM stop_to_routes WHERE key IN "
            "(SELECT key FROM routes WHERE ds_id=?)", (ds_id,))
        self.conn.execute("DELETE FROM routes WHERE ds_id=?", (ds_id,))
        self.conn.execute("DELETE FROM loaded_datasets WHERE ds_id=?", (ds_id,))