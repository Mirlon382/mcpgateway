"""Catalog MCP resources (spec 5).

The resources are read through an in-memory FastMCP client, which exercises the
real registration path (URIs, JSON payloads, error handling) without a socket.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import pytest
from fastmcp import Client, FastMCP

from mcpgateway.catalog import (
    CATALOG_DOMAINS_URI,
    CATALOG_SERVER_TOOLS_URI,
    CATALOG_SERVER_URI,
    CATALOG_SERVERS_URI,
    register_catalog_resources,
)
from mcpgateway.metrics import MetricsRecorder
from mcpgateway.registry import Registry

from support import StubPool, tool


def build_catalog(
    registry: Registry,
    *,
    states: dict[str, str] | None = None,
    inventories: dict[str, list[Any]] | None = None,
    inventory_error: Exception | None = None,
    metrics: MetricsRecorder | None = None,
) -> tuple[FastMCP, StubPool]:
    gateway = FastMCP("Catalog Test Gateway")
    pool = StubPool(
        registry,
        states=states or {"alpha": "ok"},
        inventories=inventories or {},
        inventory_error=inventory_error,
    )
    register_catalog_resources(
        gateway, registry=registry, pool=pool, metrics=metrics or MetricsRecorder()
    )
    return gateway, pool


async def read_json(client: Client[Any], uri: str) -> dict[str, Any]:
    contents = await client.read_resource(uri)
    return json.loads(contents[0].text)  # type: ignore[union-attr]


async def test_servers_resource_lists_metadata_and_status(make_registry: Callable[..., Registry]) -> None:
    gateway, _pool = build_catalog(make_registry())

    async with Client(gateway) as client:
        payload = await read_json(client, CATALOG_SERVERS_URI)

    assert payload["gateway"]["server_count"] == 2
    assert payload["gateway"]["enabled_count"] == 1
    assert payload["gateway"]["disabled_count"] == 1
    assert payload["gateway"]["status"] == "ok"
    assert payload["domains"] == ["geographie", "stats"]

    alpha = next(server for server in payload["servers"] if server["name"] == "alpha")
    assert alpha["domain"] == "stats"
    assert alpha["tags"] == ["dataset"]
    assert alpha["enabled"] is True
    assert alpha["tool_prefix"] == "alpha_"
    assert alpha["status"]["state"] == "ok"
    # Every entry carries the operational limits so a caller can reason about them.
    assert alpha["timeout_seconds"] == 30.0
    assert alpha["max_concurrency"] == 4

    beta = next(server for server in payload["servers"] if server["name"] == "beta")
    assert beta["enabled"] is False
    assert beta["status"]["state"] == "disabled"


async def test_domains_resource_groups_servers(make_registry: Callable[..., Registry]) -> None:
    gateway, _pool = build_catalog(make_registry())

    async with Client(gateway) as client:
        payload = await read_json(client, CATALOG_DOMAINS_URI)

    domains = {domain["domain"]: domain for domain in payload["domains"]}
    assert domains["stats"]["servers"] == ["alpha"]
    assert domains["stats"]["enabled_count"] == 1
    assert domains["geographie"]["enabled_count"] == 0  # beta is disabled
    assert domains["stats"]["tags"] == ["dataset"]


async def test_server_detail_includes_its_metrics(make_registry: Callable[..., Registry]) -> None:
    registry = make_registry()
    metrics = MetricsRecorder()
    metrics.record_tool_call(upstream="alpha", tool="alpha_search", seconds=0.25)
    gateway, _pool = build_catalog(registry, metrics=metrics)

    async with Client(gateway) as client:
        payload = await read_json(client, "catalog://servers/alpha")

    assert payload["server"]["name"] == "alpha"
    assert payload["metrics"]["tool_calls"]["alpha.alpha_search"]["count"] == 1
    assert payload["metrics"]["tool_calls"]["alpha.alpha_search"]["latency_ms"]["max"] == 250.0


async def test_tool_inventory_lists_gateway_side_names(make_registry: Callable[..., Registry]) -> None:
    gateway, pool = build_catalog(
        make_registry(),
        inventories={"alpha": [tool("search", "Cherche", ["query", "limit"]), tool("fetch")]},
    )

    async with Client(gateway) as client:
        payload = await read_json(client, "catalog://servers/alpha/tools")

    assert payload["status"] == "ok"
    assert payload["count"] == 2
    names = {entry["name"] for entry in payload["tools"]}
    assert names == {"search", "fetch"}
    # gateway_name is what a client must actually call.
    assert {entry["gateway_name"] for entry in payload["tools"]} == {"alpha_search", "alpha_fetch"}
    search = next(entry for entry in payload["tools"] if entry["name"] == "search")
    assert search["parameters"] == ["query", "limit"]
    assert pool.inventory_calls == 1


async def test_inventory_of_an_unreachable_upstream_is_reported_not_raised(
    make_registry: Callable[..., Registry],
) -> None:
    """A dead upstream must not break the catalog resource."""
    registry = make_registry()
    gateway, _pool = build_catalog(registry, states={"alpha": "down"}, inventory_error=RuntimeError("connect failed"))

    async with Client(gateway) as client:
        payload = await read_json(client, CATALOG_SERVER_TOOLS_URI.replace("{name}", "alpha"))

    assert payload["status"] == "unavailable"
    assert payload["count"] == 0
    assert "connect failed" in payload["error"]


async def test_inventory_of_a_disabled_upstream_is_explained(make_registry: Callable[..., Registry]) -> None:
    gateway, pool = build_catalog(make_registry())

    async with Client(gateway) as client:
        payload = await read_json(client, "catalog://servers/beta/tools")

    assert payload["enabled"] is False
    assert payload["count"] == 0
    assert "disabled" in payload["note"]
    assert pool.inventory_calls == 0  # no upstream call is attempted for a disabled server


async def test_unknown_server_is_an_error_with_the_known_names(make_registry: Callable[..., Registry]) -> None:
    gateway, _pool = build_catalog(make_registry())

    async with Client(gateway) as client:
        with pytest.raises(Exception) as excinfo:  # noqa: PT011 - message asserted below
            await client.read_resource("catalog://servers/nope")
    message = str(excinfo.value)
    assert "unknown server" in message
    assert "alpha" in message


async def test_resources_are_absent_when_the_catalog_is_disabled(make_registry: Callable[..., Registry]) -> None:
    registry = make_registry(gateway={"catalog": {"enabled": False}})

    async with Client(FastMCP("empty")) as empty_client:
        uris = {str(template.uri_template) for template in await empty_client.list_resource_templates()}
    assert uris == set()

    gateway = FastMCP("Disabled Catalog Gateway")
    registered = register_catalog_resources(
        gateway, registry=registry, pool=StubPool(registry), metrics=MetricsRecorder()
    )
    assert registered == []


async def test_tool_inventory_can_be_disabled_independently(make_registry: Callable[..., Registry]) -> None:
    registry = make_registry(gateway={"catalog": {"enabled": True, "include_tool_inventory": False}})
    gateway = FastMCP("No Inventory Gateway")
    registered = register_catalog_resources(
        gateway, registry=registry, pool=StubPool(registry), metrics=MetricsRecorder()
    )

    assert CATALOG_SERVERS_URI in registered
    assert CATALOG_DOMAINS_URI in registered
    assert CATALOG_SERVER_URI in registered
    assert CATALOG_SERVER_TOOLS_URI not in registered
