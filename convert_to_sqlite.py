"""One-shot migration: timetable_cache.json -> index.db (SQLite).

Runs once on a quiet host (peak ~9.5 GB RSS for the JSON parse). After this,
the SQLite file is the durable index; the JSON cache is retired.
"""
import argparse
import json
import os
import resource
import sqlite3
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--input", default="/data/timetable_cache.json",
                    help="Path to the legacy timetable_cache.json")
parser.add_argument("--output", default="/data/index.db",
                    help="Path of the SQLite index.db to create")
args = parser.parse_args()

t0 = time.time()
print("parsing 2.5GB json (peak ~9.5GB RSS, host quiet)...", flush=True)
with open(args.input, encoding="utf-8") as f:
    cache = json.load(f)
print(f"parsed {time.time() - t0:.0f}s, peak {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024}MB", flush=True)

db = sqlite3.connect(args.output)
db.execute("PRAGMA journal_mode=OFF")
db.execute("PRAGMA synchronous=OFF")
db.executescript("""
DROP TABLE IF EXISTS journeys; DROP TABLE IF EXISTS stops; DROP TABLE IF EXISTS routes;
DROP TABLE IF EXISTS stop_to_routes; DROP TABLE IF EXISTS meta;
CREATE TABLE journeys (id INTEGER PRIMARY KEY, ds_id INT, op TEXT, route TEXT,
                       direction TEXT, code TEXT, days TEXT, json TEXT);
CREATE TABLE stops (naptan TEXT PRIMARY KEY, name TEXT, lat REAL, lon REAL);
CREATE TABLE routes (key TEXT PRIMARY KEY, op TEXT, num TEXT, directions TEXT, stops TEXT);
CREATE TABLE stop_to_routes (naptan TEXT, key TEXT);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX j_ds ON journeys(ds_id);
CREATE INDEX s2r_n ON stop_to_routes(naptan);
""")

db.executemany(
    "INSERT INTO journeys VALUES (?,?,?,?,?,?,?,?)",
    [(i, x.get("dataset_id", 0), x["operator"], x["route_num"], x["direction"],
      x["journey_code"], json.dumps(x["days"]), json.dumps(x))
     for i, x in enumerate(cache["journeys"])])
db.executemany(
    "INSERT INTO stops VALUES (?,?,?,?)",
    [(k, v["name"], v.get("lat"), v.get("lon")) for k, v in cache["stops"].items()])
db.executemany(
    "INSERT INTO routes VALUES (?,?,?,?,?)",
    [(k, v["operator"], v["route_num"], json.dumps(v.get("directions", [])),
      json.dumps(v.get("stops", []))) for k, v in cache["routes"].items()])
db.executemany(
    "INSERT INTO stop_to_routes VALUES (?,?)",
    [(k, r) for k, rs in cache["stop_to_routes"].items() for r in rs])
db.executemany(
    "INSERT INTO meta VALUES (?,?)",
    [("loaded_datasets", json.dumps(cache.get("loaded_datasets", []))),
     ("dataset_meta", json.dumps(cache.get("dataset_meta", {}))),
     ("last_refresh", cache.get("last_refresh") or "")])
db.commit()

print(f"DB: {os.path.getsize(args.output) // 1000000}MB, "
      f"{time.time() - t0:.0f}s total, peak {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024}MB",
      flush=True)
print("MIGRATION COMPLETE", flush=True)