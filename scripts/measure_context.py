"""Empreinte sur le contexte : ce qu'un client MCP reçoit à la connexion.

    uv run python scripts/measure_context.py [chemin/registry.yaml]

Mesure, avec et sans recherche d'outils, les charges utiles envoyées *avant* toute
lecture — `instructions`, `tools/list`, `resources/list`,
`resources/templates/list` — puis la taille des ressources, qui ne partent que sur
`resources/read`. Sert à vérifier les chiffres cités dans le README et à objectiver
l'effet d'un changement de registre ou de transformée.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from fastmcp import Client

from mcpgateway.catalog import CATALOG_GUIDE_URI
from mcpgateway.registry import load_registry
from mcpgateway.server import build_gateway

INSTRUCTIONS_HEAD = 90


def _bytes(payload: Any) -> int:
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json", exclude_none=True)
    return len(json.dumps(payload, ensure_ascii=False, default=str))


def _kb(size: int) -> str:
    return f"{size / 1024:.1f} Ko"


async def measure(path: str, *, tool_search: bool) -> dict[str, int]:
    registry = load_registry(path).registry
    registry.gateway.tool_search.enabled = tool_search
    gateway, _pool, metrics = build_gateway(registry)

    label = "recherche d'outils ACTIVÉE" if tool_search else "recherche d'outils désactivée"
    async with Client(gateway) as client:
        instructions = client.instructions or ""
        tools = await client.list_tools_mcp()
        resources = await client.list_resources_mcp()
        templates = await client.list_resource_templates_mcp()

        sizes = {
            "instructions": len(instructions),
            "tools/list": _bytes(tools),
            "resources/list": _bytes(resources),
            "resources/templates/list": _bytes(templates),
        }
        sizes["total"] = sum(sizes.values())

        print(f"\n=== {label} ===")
        print(f"  instructions            : {sizes['instructions']:>8,} o  ({instructions.splitlines()[0][:INSTRUCTIONS_HEAD]})")
        print(f"  tools/list              : {sizes['tools/list']:>8,} o  ({len(tools.tools)} outil(s))")
        print(f"  resources/list          : {sizes['resources/list']:>8,} o  ({len(resources.resources)} ressource(s), en-têtes seuls)")
        print(f"  resources/templates/list: {sizes['resources/templates/list']:>8,} o  ({len(templates.resource_templates)} gabarit(s))")
        print(f"  TOTAL à la connexion    : {sizes['total']:>8,} o  ({_kb(sizes['total'])})")

        if tool_search:
            print(f"\n  outils exposés : {[tool.name for tool in tools.tools]}")
            print(f"  ressources     : {[str(resource.uri) for resource in resources.resources]}")
            guide = (await client.read_resource(CATALOG_GUIDE_URI))[0]  # type: ignore[union-attr]
            guide_size = len(guide.text or "")  # type: ignore[union-attr]
            print(f"\n  resources/read catalog://guide : {guide_size:,} caractères "
                  f"({guide_size / max(sizes['total'], 1):.1f}x tout ce qui part à la connexion)")

        snapshot = metrics.snapshot() if metrics else {}
        upstream_calls = snapshot.get("upstream_calls", {}) if isinstance(snapshot, dict) else {}
        if upstream_calls:
            print("\n  inventaires amont (mesurés par la passerelle) :")
            for name, stats in sorted(upstream_calls.items()):
                print(f"    {name:<24} {stats}")

    return sizes


async def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "registry.yaml"
    with_search = await measure(path, tool_search=True)
    without_search = await measure(path, tool_search=False)

    ratio = without_search["total"] / max(with_search["total"], 1)
    print(f"\n=== synthèse ===")
    print(f"  avec recherche d'outils : {_kb(with_search['total'])}")
    print(f"  sans                    : {_kb(without_search['total'])}")
    print(f"  rapport                 : x{ratio:.1f}")


asyncio.run(main())
