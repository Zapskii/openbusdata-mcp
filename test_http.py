# test_http.py
import asyncio
import httpx
import pytest

import openbusdata_mcp.server as server


def _client():
    counts = {"n": 0}

    def counting_handler(request):
        counts["n"] += 1
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(counting_handler))
    return client, counts


def test_spec_tool_reuses_shared_client():
    client, counts = _client()
    server.set_http_client(client)
    try:
        # Build a minimal fake spec tool the same way register_tools_from_specs
        # does, then call it twice: the shared client must serve both requests.
        async def probe_tool(**kwargs):
            c = server.get_http_client()
            resp = await c.get("https://example.test/api/v1/probe")
            return resp.text
        asyncio.run(probe_tool())
        asyncio.run(probe_tool())
        assert counts["n"] == 2
    finally:
        server.set_http_client(None)


def test_get_http_client_lazy_singleton():
    server.set_http_client(None)
    try:
        c1 = server.get_http_client()
        c2 = server.get_http_client()
        assert c1 is c2, "shared client must be a singleton"
    finally:
        server.set_http_client(None)


import time


def test_ttl_cache_hit_then_expiry(monkeypatch):
    from openbusdata_mcp.server import TTLCache
    cache = TTLCache(ttl_seconds=20.0)
    cache.put("k", {"a": 1})
    assert cache.get("k") == {"a": 1}
    fake_now = time.monotonic() + 21.0
    monkeypatch.setattr(time, "monotonic", lambda: fake_now)
    assert cache.get("k") is None, "expired entry must not be served"


def test_live_buses_ttl_cache_skips_second_request():
    # Real datafeed XML is namespace-qualified (see server.py: ns = siri.org.uk);
    # the fixture must declare xmlns or the parser finds no VehicleActivity.
    sirii = b'<?xml version="1.0"?><Siri xmlns="http://www.siri.org.uk/siri">' \
            b'<VehicleActivity>' \
            b'<MonitoredVehicleJourney><VehicleRef>V1</VehicleRef>' \
            b'</MonitoredVehicleJourney></VehicleActivity></Siri>'

    def xml_handler(request):
        counts["n"] += 1
        return httpx.Response(200, content=sirii)

    _, counts = _client()  # replaced below; reuse the counting dict
    client = httpx.AsyncClient(transport=httpx.MockTransport(xml_handler))
    server.set_http_client(client)
    server._LIVE_CACHE = server.TTLCache(20.0)
    try:
        r1 = asyncio.run(server.get_live_buses_on_route("OPX", "12"))
        r2 = asyncio.run(server.get_live_buses_on_route("OPX", "12"))
        assert counts["n"] == 1, "second call within TTL must be served from cache"
        assert r1 == r2
        r3 = asyncio.run(server.get_live_buses_on_route("OPY", "12"))
        assert counts["n"] == 2, "a different route key must miss the cache"
    finally:
        server.set_http_client(None)
        server._LIVE_CACHE = server.TTLCache(20.0)


def _assert_url_has(url, param: str) -> None:
    """Check a query parameter is present without ever rendering the URL.

    The captured URL carries ?api_key=..., and pytest renders an assert
    statement's operands on failure -- with a real key in the environment that
    would print the credential into the test output. Inline raise instead.
    (Ruling R34.)
    """
    if param not in url:
        raise AssertionError(f"request URL is missing {param}")


def test_live_buses_passes_server_side_filters():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(200, content=b'<Siri xmlns="http://www.siri.org.uk/siri"/>')

    server.set_http_client(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    server._LIVE_CACHE = server.TTLCache(20.0)
    try:
        out = asyncio.run(server.get_live_buses_on_route(
            "OPX", "12", origin_ref="490A", destination_ref="490B",
            vehicle_ref="V1", bounding_box="-0.2,51.4,0.0,51.6"))
        _assert_url_has(captured["url"], "originRef=490A")
        _assert_url_has(captured["url"], "destinationRef=490B")
        _assert_url_has(captured["url"], "vehicleRef=V1")
        _assert_url_has(captured["url"], "boundingBox=-0.2%2C51.4%2C0.0%2C51.6")
        _assert_url_has(captured["url"], "operatorRef=OPX")
        _assert_url_has(captured["url"], "lineRef=12")
        assert "No live buses found." in out
    finally:
        server.set_http_client(None)
        server._LIVE_CACHE = server.TTLCache(20.0)
