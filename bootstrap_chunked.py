"""Chunked full-load driver: memory-bounded incremental bootstrap.

The full 941-dataset index (~10-11 GB parsed) cannot fit in one container
pass (7.7 GB cgroup limit) - three single-pass attempts died in the OOM
killer. This driver loads the catalogue in slices of 60 datasets, saving
the cache to disk after each slice, so peak RSS stays ~1-2 GB. Resumable:
run it repeatedly; already-loaded datasets are skipped, withdrawn datasets
are reconciled away, and the cache only grows. Exit code 0 = fully loaded.
"""
import asyncio
import sys

sys.path.insert(0, "/build/src")
import httpx  # noqa: E402
import openbusdata_mcp.server as server  # noqa: E402

SLICE = 60


def report() -> str:
    i = server.index
    return (f"{len(i.loaded_datasets)} datasets, {len(i.stops)} stops, "
            f"{len(i.routes)} routes, {len(i.journeys)} journeys")


async def catalogue_ids() -> list[int]:
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        all_ids: list[int] = []
        offset = 0
        limit = 100
        while True:
            resp = await client.get(
                f"{server.BASE_URL}/api/v1/dataset/?limit={limit}&offset={offset}&api_key={server.API_KEY}")
            if resp.status_code != 200:
                break
            results = resp.json().get("results", [])
            if not results:
                break
            all_ids.extend(r["id"] for r in results)
            if len(results) < limit:
                break
            offset += limit
    return all_ids


async def main() -> None:
    server.index.load_cache()
    print(f"resume point: {report()}", flush=True)

    all_ids = await catalogue_ids()
    if not all_ids:
        print("catalogue sweep failed - aborting without touching cache", flush=True)
        sys.exit(2)

    # Reconcile away withdrawn datasets before loading (keeps memory honest).
    known = set(all_ids)
    for ds_id in [i for i in server.index.loaded_datasets if i not in known]:
        server.index.discard_dataset(ds_id)
    server.index.save_cache()

    todo = [i for i in all_ids if i not in server.index.loaded_datasets]
    print(f"catalogue: {len(all_ids)} datasets, {len(todo)} to load", flush=True)

    total = len(todo)
    for start in range(0, total, SLICE):
        chunk = todo[start:start + SLICE]
        for ds_id in chunk:
            try:
                await server.load_dataset(ds_id)
            except Exception as e:
                print(f"  ds {ds_id}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        # Save + RELEASE: trim any non-essential object churn between slices.
        server.index.save_cache()
        print(f"[{start + len(chunk)}/{total}] {report()}", flush=True)

    server.index.last_refresh = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).isoformat()
    server.index.save_cache()
    print(f"FULL LOAD COMPLETE: {report()}", flush=True)


asyncio.run(main())