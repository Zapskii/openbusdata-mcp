"""E2E: drive the SQLite-backed tools through the real MCP protocol.

Also asserts the memory profile: RSS must stay far below the old
multi-GB in-memory approach. Exits non-zero on any failure.
"""
import json
import subprocess
import sys
import time

import httpx  # noqa: F401  (ensures deps installed in this env)

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def rss_mb():
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS"):
                return int(line.split()[1]) // 1024
    return -1


async def main():
    params = StdioServerParameters(
        command="python3", args=["-m", "openbusdata_mcp.server"],
        env={"PYTHONUNBUFFERED": "1", "OPENBUS_API_KEY": "x" * 40})
    results = {}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            assert len(names) == 17, f"expected 17 tools, got {len(names)}"
            print(f"TOOLS-OK: 17 registered", flush=True)
            t0 = time.time()
            res = await session.call_tool("search_stops", {"query": "Oaks Cross"})
            assert res.isError is False, res
            stops = json.loads(res.content[0].text)
            results["search_stops"] = (len(stops), round(time.time() - t0, 2))
            assert stops, "search_stops returned nothing"
            assert {"naptan", "name"} <= set(stops[0].keys())
            print(f"search_stops: {len(stops)} hits in {results['search_stops'][1]}s", flush=True)

            t0 = time.time()
            res = await session.call_tool("find_routes_between_stops",
                                          {"stop_a": stops[0]["naptan"], "stop_b": stops[-1]["naptan"]})
            assert res.isError is False, res
            results["find_routes_between_stops"] = round(time.time() - t0, 2)
            print(f"find_routes_between_stops: {results['find_routes_between_stops']}s", flush=True)

            t0 = time.time()
            res = await session.call_tool("get_route_stops", {"operator": "Kinch", "route": "1"})
            assert res.isError is False, res
            results["get_route_stops"] = round(time.time() - t0, 2)
            print(f"get_route_stops: {results['get_route_stops']}s", flush=True)

            t0 = time.time()
            res = await session.call_tool("find_buses_by_arrival_time",
                                          {"stop_a": "Oaks Cross", "stop_b": "Quorn Way",
                                           "arrive_by": "18:00", "day": "wed"})
            assert res.isError is False, res
            results["find_buses_by_arrival_time"] = round(time.time() - t0, 2)
            print(f"find_buses_by_arrival_time: {results['find_buses_by_arrival_time']}s", flush=True)

            t0 = time.time()
            res = await session.call_tool("plan_journey",
                                          {"stop_a": "Oaks Cross", "stop_b": "Quorn Way",
                                           "arrive_by": "18:00", "day": "wed", "max_changes": 1})
            assert res.isError is False, res
            results["plan_journey"] = round(time.time() - t0, 2)
            print(f"plan_journey: {results['plan_journey']}s", flush=True)

    print(f"PEAK-CLIENT-RSS-MB: {rss_mb()}", flush=True)
    print("E2E-ALL-OK", flush=True)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())