"""Middleware: metrics attribution and the concurrency ceiling (specs 3 and 4)."""

from __future__ import annotations

import asyncio
import types
from typing import Any

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import MiddlewareContext

from mcpgateway.metrics import MetricsRecorder
from mcpgateway.middleware import ConcurrencyLimitMiddleware, MetricsMiddleware
from mcpgateway.upstreams import UpstreamPool


def context_for(tool: str, arguments: dict[str, Any] | None = None) -> MiddlewareContext[Any]:
    message = types.SimpleNamespace(name=tool, arguments=arguments or {})
    return MiddlewareContext(message=message, method="tools/call", source="client", type="request")


def resource_context(uri: str) -> MiddlewareContext[Any]:
    message = types.SimpleNamespace(uri=uri)
    return MiddlewareContext(message=message, method="resources/read", source="client", type="request")


async def echo(context: MiddlewareContext[Any]) -> str:
    return "ok"


# --- metrics ---------------------------------------------------------------


async def test_metrics_middleware_attributes_tool_calls_to_their_upstream(
    pool: UpstreamPool, metrics: MetricsRecorder
) -> None:
    middleware = MetricsMiddleware(pool, metrics)

    await middleware.on_call_tool(context_for("alpha_search", {"q": "x"}), echo)
    await middleware.on_call_tool(context_for("beta_search"), echo)

    snapshot = metrics.snapshot()["tool_calls"]
    assert snapshot["alpha.alpha_search"]["count"] == 1
    # beta is disabled in the registry, so its tool cannot be attributed.
    assert snapshot["unknown.beta_search"]["count"] == 1


async def test_metrics_middleware_records_failures_and_reraises(
    pool: UpstreamPool, metrics: MetricsRecorder
) -> None:
    middleware = MetricsMiddleware(pool, metrics)

    async def boom(context: MiddlewareContext[Any]) -> str:
        raise RuntimeError("upstream exploded")

    with pytest.raises(RuntimeError, match="upstream exploded"):
        await middleware.on_call_tool(context_for("alpha_search"), boom)

    series = metrics.snapshot()["tool_calls"]["alpha.alpha_search"]
    assert series["count"] == 1
    assert series["errors"] == 1


async def test_metrics_middleware_counts_messages_and_resource_reads(
    pool: UpstreamPool, metrics: MetricsRecorder
) -> None:
    middleware = MetricsMiddleware(pool, metrics)

    await middleware.on_message(context_for("alpha_search"), echo)
    await middleware.on_read_resource(resource_context("data://alpha/config"), echo)

    snapshot = metrics.snapshot()
    assert snapshot["messages"]["tools/call"] == 1
    assert snapshot["upstream_calls"]["alpha.read_resource"]["count"] == 1


async def test_metrics_middleware_records_list_tools_latency(
    pool: UpstreamPool, metrics: MetricsRecorder
) -> None:
    middleware = MetricsMiddleware(pool, metrics)

    async def list_tools(context: MiddlewareContext[Any]) -> list[str]:
        return ["a", "b"]

    await middleware.on_list_tools(context_for("tools/list"), list_tools)
    series = metrics.snapshot()["upstream_calls"]["gateway.list_tools"]
    assert series["count"] == 1


# --- concurrency -----------------------------------------------------------


async def test_concurrency_limit_passes_calls_through(make_registry: Any, metrics: MetricsRecorder) -> None:
    registry = make_registry(gateway={"concurrency": {"global_limit": 2, "acquire_timeout_seconds": 1}})
    pool = UpstreamPool(registry, metrics)
    middleware = ConcurrencyLimitMiddleware(pool)

    assert await middleware.on_call_tool(context_for("alpha_search"), echo) == "ok"
    # Slots are released again: the same call can be repeated up to the limit.
    assert await middleware.on_call_tool(context_for("alpha_search"), echo) == "ok"


async def test_concurrency_limit_rejects_when_saturated(make_registry: Any, metrics: MetricsRecorder) -> None:
    registry = make_registry(
        servers=[{"name": "alpha", "url": "https://alpha.example.org/mcp", "domain": "stats", "max_concurrency": 1}],
        gateway={"concurrency": {"global_limit": 8, "acquire_timeout_seconds": 0.05}},
    )
    pool = UpstreamPool(registry, metrics)
    middleware = ConcurrencyLimitMiddleware(pool)

    # Hold the only slot for this upstream.
    await pool.semaphore("alpha").acquire()  # type: ignore[union-attr]
    try:
        with pytest.raises(ToolError) as excinfo:
            await middleware.on_call_tool(context_for("alpha_search"), echo)
        assert "alpha" in str(excinfo.value)
    finally:
        pool.semaphore("alpha").release()  # type: ignore[union-attr]

    # Once the slot is free again the call succeeds.
    assert await middleware.on_call_tool(context_for("alpha_search"), echo) == "ok"


async def test_global_concurrency_limit_applies_to_unattributed_tools(
    make_registry: Any, metrics: MetricsRecorder
) -> None:
    registry = make_registry(gateway={"concurrency": {"global_limit": 1, "acquire_timeout_seconds": 0.05}})
    pool = UpstreamPool(registry, metrics)
    middleware = ConcurrencyLimitMiddleware(pool)

    await pool.global_semaphore.acquire()
    try:
        with pytest.raises(ToolError):
            await middleware.on_call_tool(context_for("some_gateway_tool"), echo)
    finally:
        pool.global_semaphore.release()


async def test_gateway_wide_slot_is_released_after_a_failure(make_registry: Any, metrics: MetricsRecorder) -> None:
    registry = make_registry(gateway={"concurrency": {"global_limit": 1, "acquire_timeout_seconds": 0.05}})
    pool = UpstreamPool(registry, metrics)
    middleware = ConcurrencyLimitMiddleware(pool)

    async def boom(context: MiddlewareContext[Any]) -> str:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await middleware.on_call_tool(context_for("alpha_search"), boom)

    # The only slot must be free again, otherwise the gateway would wedge.
    assert pool.global_semaphore._value == 1  # noqa: SLF001 - deliberate internal check
    assert await middleware.on_call_tool(context_for("alpha_search"), echo) == "ok"


async def test_semaphores_are_shared_between_middleware_and_probes(pool: UpstreamPool) -> None:
    """Probes and tool calls contend for the same per-upstream slot (spec 4)."""
    assert pool.semaphores_for_tool("alpha_search")[0] is pool.global_semaphore
    assert pool.semaphore("alpha") in pool.semaphores_for_tool("alpha_search")

    # Semaphores are per upstream, sized from the registry entry.
    entry = pool.registry.by_name("alpha")
    alpha_semaphore = pool.semaphore("alpha")
    assert alpha_semaphore is not None
    assert alpha_semaphore._value == entry.max_concurrency  # noqa: SLF001 - deliberate internal check


async def test_limit_waits_for_a_slot_that_frees_up(make_registry: Any, metrics: MetricsRecorder) -> None:
    registry = make_registry(gateway={"concurrency": {"global_limit": 1, "acquire_timeout_seconds": 1.0}})
    pool = UpstreamPool(registry, metrics)
    middleware = ConcurrencyLimitMiddleware(pool)

    await pool.global_semaphore.acquire()

    async def release_soon() -> None:
        await asyncio.sleep(0.05)
        pool.global_semaphore.release()

    releaser = asyncio.create_task(release_soon())
    try:
        assert await middleware.on_call_tool(context_for("alpha_search"), echo) == "ok"
    finally:
        await releaser

    # Queueing time is accounted for separately from the call itself.
    waits = metrics.snapshot()["upstream_calls"]["alpha.slot_wait"]
    assert waits["count"] == 1
    assert waits["latency_ms"]["max"] >= 40.0


async def test_slot_wait_is_recorded_for_an_upstream_specific_limit(
    make_registry: Any, metrics: MetricsRecorder
) -> None:
    registry = make_registry(
        servers=[{"name": "alpha", "url": "https://alpha.example.org/mcp", "domain": "stats", "max_concurrency": 1}],
        gateway={"concurrency": {"global_limit": 4, "acquire_timeout_seconds": 1.0}},
    )
    pool = UpstreamPool(registry, metrics)
    middleware = ConcurrencyLimitMiddleware(pool)

    await pool.semaphore("alpha").acquire()  # type: ignore[union-attr]

    async def release_soon() -> None:
        await asyncio.sleep(0.05)
        pool.semaphore("alpha").release()  # type: ignore[union-attr]

    releaser = asyncio.create_task(release_soon())
    try:
        assert await middleware.on_call_tool(context_for("alpha_search"), echo) == "ok"
    finally:
        await releaser

    assert "alpha.slot_wait" in metrics.snapshot()["upstream_calls"]


async def test_fast_calls_do_not_emit_slot_wait_noise(make_registry: Any, metrics: MetricsRecorder) -> None:
    registry = make_registry(gateway={"concurrency": {"global_limit": 2, "acquire_timeout_seconds": 1.0}})
    pool = UpstreamPool(registry, metrics)
    middleware = ConcurrencyLimitMiddleware(pool)

    await middleware.on_call_tool(context_for("alpha_search"), echo)

    assert metrics.snapshot()["upstream_calls"] == {}
