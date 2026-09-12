#!/usr/bin/env python3
"""List every tool the openbusdata-mcp server registers, by both paths.

Enumerates by the property (every registration site) rather than by a proxy for
it -- a grep for `@mcp.tool()` finds only the 13 hand-written tools and misses
the 9 built at startup from the OpenAPI specs.

Static analysis only: reads server.py and the spec YAMLs. Never imports the
server (import runs ensure_schema() and touches the cache dir), never makes a
network call, never reads the API key.

The authoritative list at runtime is:
    [t.name for t in mcp._tool_manager.list_tools()]
Use that if you need the registry exactly as constructed; use this to see the
surface without starting the server.

Usage:
    uv run python .claude/skills/openbusdata-mcp-endpoints/scripts/list_endpoints.py [repo_root]
"""
from __future__ import annotations

import ast
import pathlib
import sys

try:
    import yaml
except ImportError:  # pragma: no cover
    print("PyYAML is required (it is a runtime dependency of the server).", file=sys.stderr)
    raise SystemExit(2)


def repo_root(argv: list[str]) -> pathlib.Path:
    if len(argv) > 1:
        return pathlib.Path(argv[1]).resolve()
    # scripts/ -> skill dir -> skills/ -> .claude/ -> repo root
    return pathlib.Path(__file__).resolve().parents[4]


def hand_written(server_py: pathlib.Path) -> list[tuple[str, int]]:
    """Every function carrying an @mcp.tool() decorator."""
    tree = ast.parse(server_py.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            target = ast.unparse(dec.func if isinstance(dec, ast.Call) else dec)
            if target == "mcp.tool":
                found.append((node.name, node.lineno))
    return sorted(found, key=lambda item: item[1])


def spec_generated(specs_dir: pathlib.Path) -> list[tuple[str, str, str]]:
    """Mirror register_tools_from_specs(): one tool per GET operation.

    Naming is operationId when present, else f"{tag}_{clean_path}" -- and none
    of the bundled specs define operationId, so the fallback is what produces
    every name. Renames here are breaking changes for callers.
    """
    generated = []
    for yml in sorted(specs_dir.glob("*.yml")):
        spec = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
        servers = spec.get("servers") or [{}]
        base_path = servers[0].get("url", "/api/v1/")
        for path_template, methods in (spec.get("paths") or {}).items():
            for method, operation in methods.items():
                if method.lower() != "get":
                    continue
                tag = "general"
                if operation.get("tags"):
                    tag = operation["tags"][0].replace(" ", "_").replace("-", "_")
                op_id = operation.get("operationId")
                if op_id:
                    name = op_id
                else:
                    clean = (
                        path_template.strip("/")
                        .replace("/", "_")
                        .replace("{", "by_")
                        .replace("}", "")
                    )
                    name = f"{tag}_{clean}"
                name = name.replace("-", "_").replace(".", "_")
                generated.append((name, f"GET {path_template}", yml.name))
    return generated


def main() -> int:
    root = repo_root(sys.argv)
    server_py = root / "src" / "openbusdata_mcp" / "server.py"
    specs_dir = root / "src" / "openbusdata_mcp" / "openapi-schema"

    if not server_py.is_file():
        print(f"server.py not found at {server_py}", file=sys.stderr)
        return 1

    static = hand_written(server_py)
    generated = spec_generated(specs_dir)

    print(f"Hand-written (@mcp.tool() in server.py): {len(static)}")
    for name, line in static:
        print(f"  {name}  (server.py:{line})")

    print(f"\nGenerated from OpenAPI specs ({specs_dir.name}/*.yml): {len(generated)}")
    for name, route, source in generated:
        print(f"  {name}  [{route}]  <- {source}")

    total = len(static) + len(generated)
    print(f"\nTOTAL: {total}")
    if not generated:
        print("  (no specs found -- the passthrough tools would be unavailable)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
