"""Gateway middleware: metrics around every call, and concurrency ceilings.

Two concerns, one file, because both hinge on knowing which upstream a call will
reach:

* :class:`MetricsMiddleware` times and counts tool calls, resource reads, prompt
  renders and messages, attributing each one to its upstream (spec 3);
* :class:`ConcurrencyLimitMiddleware` holds the gateway-wide and per-upstream
  semaphores for the duration of a tool call, so a slow upstream cannot be
  stampeded by concurrent clients (spec 4).

Middleware runs inside the server, which means it wraps the whole proxy round trip:
the latency recorded here is what the caller actually experiences, upstream
included. ``on_message`` additionally sees notifications and requests that never
reach a handler, which is how malformed ``tools/call`` params show up in the
counters.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext

from .logging_utils import get_logger, log_event
from .metrics import MetricsRecorder, Timer
from .upstreams import UpstreamPool


class MetricsMiddleware(Middleware):
    """Record counts and latency for everything the gateway serves."""

    def __init__(self, pool: UpstreamPool, metrics: MetricsRecorder) -> None:
        super().__init__()
        self.pool = pool
        self.metrics = metrics
        self._logger = get_logger("middleware.metrics")

    # -- helpers -----------------------------------------------------------

    def _upstream(self, tool_name: str) -> tuple[str | None, Any]:
        entry = self.pool.registry.upstream_of_tool(tool_name)
        return (entry.name if entry else None), entry

    # -- hooks -------------------------------------------------------------

    async def on_message(self, context: MiddlewareContext[Any], call_next: Any) -> Any:
        self.metrics.record_message(context.method)
        return await call_next(context)

    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: Any) -> Any:
        tool_name = getattr(context.message, "name", "<unknown>")
        upstream, _entry = self._upstream(tool_name)
        timer = Timer()
        try:
            result = await call_next(context)
        except Exception as exc:
            self.metrics.record_tool_call(
                upstream=upstream, tool=tool_name, seconds=timer.elapsed, error=exc
            )
            raise
        self.metrics.record_tool_call(upstream=upstream, tool=tool_name, seconds=timer.elapsed)
        return result

    async def on_read_resource(self, context: MiddlewareContext[Any], call_next: Any) -> Any:
        uri = getattr(context.message, "uri", "<unknown>")
        entry = self.pool.registry.upstream_of_resource(uri)
        timer = Timer()
        try:
            result = await call_next(context)
        except Exception as exc:
            self.metrics.record_upstream_call(
                upstream=entry.name if entry else "gateway", operation="read_resource", seconds=timer.elapsed, error=exc
            )
            raise
        self.metrics.record_upstream_call(
            upstream=entry.name if entry else "gateway",
            operation="read_resource",
            seconds=timer.elapsed,
            uri=str(uri),
        )
        return result

    async def on_get_prompt(self, context: MiddlewareContext[Any], call_next: Any) -> Any:
        name = getattr(context.message, "name", "<unknown>")
        upstream, _entry = self._upstream(name)
        timer = Timer()
        try:
            result = await call_next(context)
        except Exception as exc:
            self.metrics.record_upstream_call(
                upstream=upstream or "gateway", operation="get_prompt", seconds=timer.elapsed, error=exc
            )
            raise
        self.metrics.record_upstream_call(
            upstream=upstream or "gateway", operation="get_prompt", seconds=timer.elapsed, prompt=name
        )
        return result

    async def on_list_tools(self, context: MiddlewareContext[Any], call_next: Any) -> Any:
        timer = Timer()
        try:
            tools = await call_next(context)
        except Exception as exc:
            self.metrics.record_upstream_call(upstream="gateway", operation="list_tools", seconds=timer.elapsed, error=exc)
            raise
        self.metrics.record_upstream_call(
            upstream="gateway", operation="list_tools", seconds=timer.elapsed, tool_count=len(tools)
        )
        return tools


class ConcurrencyLimitMiddleware(Middleware):
    """Bound concurrent tool calls, failing fast when the pool stays saturated.

    Waiting is bounded by ``concurrency.acquire_timeout_seconds``: past that the
    caller gets a ``ToolError`` naming the busy upstream instead of queueing
    indefinitely, which is the behaviour a gateway should have under overload.

    Time spent queueing is recorded as the ``slot_wait`` upstream operation, so a
    latency series that includes queueing can be told apart from upstream time.
    """

    #: Waits shorter than this are not worth a metric line.
    WAIT_LOG_THRESHOLD_SECONDS = 0.001

    def __init__(self, pool: UpstreamPool) -> None:
        super().__init__()
        self.pool = pool
        self._logger = get_logger("middleware.concurrency")
        self._acquire_timeout = pool.registry.gateway.concurrency.acquire_timeout_seconds

    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: Any) -> Any:
        tool_name = getattr(context.message, "name", "<unknown>")
        entry = self.pool.registry.upstream_of_tool(tool_name)
        label = entry.name if entry is not None else "gateway"
        semaphores = self.pool.semaphores_for_tool(tool_name)
        acquired: list[asyncio.Semaphore] = []
        wait_timer = Timer()
        try:
            for semaphore in semaphores:
                try:
                    await asyncio.wait_for(semaphore.acquire(), timeout=self._acquire_timeout)
                except (TimeoutError, asyncio.TimeoutError) as exc:
                    self._record_wait(label, wait_timer.elapsed)
                    log_event(
                        self._logger,
                        "concurrency_rejected",
                        f"rejected {tool_name}: {label} is at its concurrency limit",
                        level=30,  # logging.WARNING
                        tool=tool_name,
                        upstream=label,
                        wait_seconds=round(wait_timer.elapsed, 4),
                        limit=self._acquire_timeout,
                    )
                    raise ToolError(
                        f"Gateway is busy: '{label}' refused '{tool_name}' after waiting "
                        f"{self._acquire_timeout:g}s for a free slot. Retry shortly."
                    ) from exc
                acquired.append(semaphore)
            self._record_wait(label, wait_timer.elapsed)
            return await call_next(context)
        finally:
            for semaphore in reversed(acquired):
                semaphore.release()

    def _record_wait(self, label: str, seconds: float) -> None:
        if seconds < self.WAIT_LOG_THRESHOLD_SECONDS:
            return
        self.pool.metrics.record_upstream_call(upstream=label, operation="slot_wait", seconds=seconds)
