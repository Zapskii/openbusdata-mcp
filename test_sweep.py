# test_sweep.py
import asyncio
from datetime import datetime, timedelta, timezone

import httpx

import openbusdata_mcp.server as server

_NOW = datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _sweep_client(captured):
    def handler(request):
        captured.append(str(request.url))
        return httpx.Response(200, json={"results": []})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_sweep_catalogue_passes_modified_date():
    captured = []
    async def _run():
        async with _sweep_client(captured) as client:
            return await server._sweep_catalogue(
                client, modified_since="2026-09-01T00:00:00+00:00")
    catalog, complete = asyncio.run(_run())
    assert complete and catalog == {}
    assert any("modifiedDate=2026-09-01" in u for u in captured), captured


def test_sweep_catalogue_without_filter_has_no_modified_date():
    captured = []
    async def _run():
        async with _sweep_client(captured) as client:
            return await server._sweep_catalogue(client)
    asyncio.run(_run())
    assert captured and all("modifiedDate" not in u for u in captured), captured


def test_full_sweep_due_default_and_watermark(writer):
    assert writer.full_sweep_due() is True, "no watermark recorded -> due"
    writer.set_last_full_sweep(_iso(_NOW - timedelta(hours=1)))
    assert writer.full_sweep_due() is False
    writer.set_last_full_sweep(_iso(_NOW - timedelta(days=8)))
    assert writer.full_sweep_due() is True, "stale watermark -> due"


def test_delta_uses_filtered_sweep_when_fresh(monkeypatch, writer):
    writer.mark_dataset_loaded(1, None, "OpA")
    writer.set_last_refresh(_iso(_NOW - timedelta(days=2)))
    writer.set_last_full_sweep(_iso(_NOW - timedelta(hours=1)))
    writer.commit()
    seen = {}

    async def fake_sweep(client, modified_since=None):
        seen["modified_since"] = modified_since
        return {99: {"id": 99, "modified": _iso(_NOW - timedelta(days=1))}}, True

    async def fake_load(ds_id, force_reload=False, client=None):
        seen["loaded"] = seen.get("loaded", []) + [ds_id]
        return {}

    monkeypatch.setattr(server, "_sweep_catalogue", fake_sweep)
    monkeypatch.setattr(server, "_load_dataset", fake_load)
    result = asyncio.run(server._load_timetable_delta())
    assert seen["modified_since"] is not None, "fresh watermark must use a filtered sweep"
    assert seen["loaded"] == [99]
    assert "purged" in result and "0 purged" in result, result


def test_delta_full_sweep_when_reconcile_due(monkeypatch, writer):
    writer.mark_dataset_loaded(1, None, "OpA")
    writer.set_last_refresh("2026-09-10T00:00:00+00:00")
    writer.commit()  # no last_full_sweep watermark -> full sweep due
    seen = {}

    async def fake_sweep(client, modified_since=None):
        seen["modified_since"] = modified_since
        return {}, True  # catalogue no longer contains dataset 1

    async def fake_load(ds_id, force_reload=False, client=None):
        return {}

    monkeypatch.setattr(server, "_sweep_catalogue", fake_sweep)
    monkeypatch.setattr(server, "_load_dataset", fake_load)
    result = asyncio.run(server._load_timetable_delta())
    assert seen["modified_since"] is None, "full sweep must be unfiltered"
    assert "1 purged" in result, result  # dataset 1 withdrawn -> purged
