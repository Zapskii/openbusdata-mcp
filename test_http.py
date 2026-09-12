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