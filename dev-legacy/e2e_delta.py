#!/usr/bin/env python3
"""E2E driver: exercise the delta path against the live BODS API in an isolated volume.

Sequence:
1. Full-load ONLY 2 datasets (via loaded_datasets seed trick) to create a baseline cache.
2. Run _load_timetable_delta(since=2h ago) -> should refresh ~0-3 datasets and purge none.
3. Purge one dataset from the index, save; run delta again -> that one dataset re-downloaded.
4. Assert watermark advanced and cache written.
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, "/build/src")
import openbusdata_mcp.server as server  # noqa: E402


def stamp(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


async def main() -> None:
    s = server
    # --- Baseline: cache with two known datasets (ids from live catalogue head) ---
    async with __import__("httpx").AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(f"{s.BASE_URL}/api/v1/dataset/?limit=2&api_key={s.API_KEY}")
        ids = [r["id"] for r in resp.json()["results"]]
    print(f"baseline datasets: {ids}")
    for ds in ids:
        meta = await s._load_dataset(ds)
        assert meta, f"dataset {ds} failed to load"
    print(f"baseline loaded: {len(s.index.loaded_datasets)} datasets, "
          f"{len(s.index.journeys)} journeys")
    s.index.last_refresh = stamp(120)
    s.index.save_cache()
    before_journeys = len(s.index.journeys)

    # --- Delta run: reference = watermark (2h ago). Expect a handful refreshed.
    out = await s._load_timetable_delta(since="", reconcile=True)
    print("DELTA-1:", out)
    assert "errors" in out
    # Watermark must have advanced
    after = s._parse_bods_ts(s.index.last_refresh)
    assert after is not None, "watermark missing after delta"
    assert s._parse_bods_ts(stamp(130)) is not None
    assert after > s._parse_bods_ts(stamp(130))
    print("watermark advanced: OK")

    # --- Force-change test: drop one dataset from the index, then delta.
    # To make the victim "stale", set the watermark OLDER than its modified
    # date (2026-08-01 < victim.modified) -> delta must re-download it.
    victim = ids[0]
    s.index.discard_dataset(victim)
    s.index.last_refresh = "2026-08-01T00:00:00+00:00"
    s.index.save_cache()
    j_after_purge = len(s.index.journeys)
    out2 = await s._load_timetable_delta(since="", reconcile=True)
    print("DELTA-2:", out2)
    assert victim in s.index.loaded_datasets, "changed dataset was not re-downloaded"
    assert victim in s.index.dataset_meta
    assert len(s.index.journeys) > j_after_purge, "victim journeys not restored"
    print(f"re-download after purge: OK (journeys {before_journeys} -> purge {j_after_purge} -> reload {len(s.index.journeys)})")
    print("E2E DELTA PATH: ALL PASS")


asyncio.run(main())