"""Upstream pool internals that can be exercised without a network."""

from __future__ import annotations

from typing import Any, Callable

from mcpgateway.metrics import MetricsRecorder
from mcpgateway.registry import Registry
from mcpgateway.upstreams import STATE_UNKNOWN, UpstreamPool


class FakeFastMCPTool:
    """Mimics FastMCP's component model, which exposes ``parameters``."""

    def __init__(self, name: str, description: str = "", parameters: dict[str, Any] | None = None) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters or {}


class FakeProtocolTool:
    """Mimics ``mcp.types.Tool``, which exposes ``input_schema``."""

    def __init__(self, name: str, input_schema: dict[str, Any]) -> None:
        self.name = name
        self.description = ""
        self.input_schema = input_schema


class FakeConvertibleTool:
    """Exposes neither attribute, only the documented ``to_mcp_tool()`` conversion."""

    def __init__(self, name: str, input_schema: dict[str, Any]) -> None:
        self.name = name
        self.description = ""
        self._mcp = FakeProtocolTool(name, input_schema)

    def to_mcp_tool(self) -> FakeProtocolTool:
        return self._mcp


def test_summarise_reads_fastmcp_parameters_attribute(registry: Registry) -> None:
    """Regression: the inventory used to report an empty parameter list for every tool."""
    summary = UpstreamPool._summarise(  # noqa: SLF001 - unit-testing a pure helper
        registry.by_name("alpha"),
        FakeFastMCPTool(
            "search_melodi_datasets",
            "  Recherche un jeu de données.  ",
            {"type": "object", "properties": {"query": {}, "limit": {}, "lang": {}}},
        ),
    )

    assert summary.name == "search_melodi_datasets"
    assert summary.gateway_name == "alpha_search_melodi_datasets"
    assert summary.parameters == ["lang", "limit", "query"]
    assert summary.description == "Recherche un jeu de données."


def test_summarise_also_accepts_the_protocol_shape(registry: Registry) -> None:
    summary = UpstreamPool._summarise(  # noqa: SLF001
        registry.by_name("alpha"),
        FakeProtocolTool("geocode", {"type": "object", "properties": {"text": {}}}),
    )
    assert summary.parameters == ["text"]


def test_summarise_falls_back_to_to_mcp_tool(registry: Registry) -> None:
    summary = UpstreamPool._summarise(  # noqa: SLF001
        registry.by_name("alpha"),
        FakeConvertibleTool("altitude", {"type": "object", "properties": {"lon": {}, "lat": {}}}),
    )
    assert summary.parameters == ["lat", "lon"]


def test_summarise_tolerates_a_tool_without_any_schema(registry: Registry) -> None:
    class Bare:
        name = "no_schema"

    summary = UpstreamPool._summarise(registry.by_name("alpha"), Bare())  # noqa: SLF001
    assert summary.parameters == []
    assert summary.description == ""
    assert summary.as_dict() == {
        "name": "no_schema",
        "gateway_name": "alpha_no_schema",
        "description": "",
        "parameters": [],
    }


def test_status_defaults_to_unknown_and_serialises(registry: Registry, metrics: MetricsRecorder) -> None:
    pool = UpstreamPool(registry, metrics)
    status = pool.status("alpha")

    assert status.state == STATE_UNKNOWN
    payload = status.as_dict()
    assert payload["state"] == "unknown"
    assert payload["last_checked_at"] is None
    assert payload["age_seconds"] is None
    assert set(payload) == {
        "state",
        "latency_ms",
        "tool_count",
        "consecutive_failures",
        "last_checked_at",
        "last_ok_at",
        "age_seconds",
        "last_error",
    }


def test_semaphores_come_from_the_registry_entries(
    registry: Registry, metrics: MetricsRecorder
) -> None:
    pool = UpstreamPool(registry, metrics)

    assert set(pool.statuses()) == {"alpha"}  # only enabled servers are tracked
    assert pool.semaphore("beta") is None  # beta is disabled
    alpha = pool.semaphore("alpha")
    assert alpha is not None
    assert alpha._value == registry.by_name("alpha").max_concurrency  # noqa: SLF001
    assert pool.global_semaphore._value == registry.gateway.concurrency.global_limit  # noqa: SLF001


def test_semaphores_for_a_gateway_owned_tool_only_include_the_global_slot(
    registry: Registry, metrics: MetricsRecorder
) -> None:
    pool = UpstreamPool(registry, metrics)
    assert pool.semaphores_for_tool("catalog_like_tool") == [pool.global_semaphore]


def test_mounted_servers_are_built_lazily_and_cached(registry: Registry, metrics: MetricsRecorder) -> None:
    pool = UpstreamPool(registry, metrics)
    entry = registry.by_name("alpha")

    first = pool.proxy_server(entry)
    assert pool.proxy_server(entry) is first  # cached, not rebuilt per call
    assert [entry.name for entry, _server in pool.mounted_servers] == ["alpha"]  # disabled servers are not mounted


def test_cached_inventory_is_none_until_fetched(registry: Registry, metrics: MetricsRecorder) -> None:
    pool = UpstreamPool(registry, metrics)
    assert pool.cached_inventory("alpha") is None


def test_prober_is_not_started_when_the_interval_is_zero(
    make_registry: Callable[..., Registry], metrics: MetricsRecorder
) -> None:
    registry = make_registry(gateway={"health": {"enabled": True, "probe_interval_seconds": 0}})
    pool = UpstreamPool(registry, metrics)

    pool.start_prober()
    assert pool._prober_task is None  # noqa: SLF001 - assertion on the documented no-op
