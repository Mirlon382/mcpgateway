"""Upstream pool: one mounted proxy per registry entry, plus probes and limits.

Responsibilities:

* build a ``ProxyProvider`` + ``ProxyClient`` per enabled upstream and hand back a
  server to mount on the gateway (spec 1);
* track reachability of every upstream with a real round trip and remember the
  result for the health endpoint (spec 2);
* fetch and cache each upstream's tool inventory for the catalog resources (spec 5);
* enforce per-upstream and gateway-wide concurrency limits and per-call timeouts
  (spec 4) — the same semaphores guard proxied tool calls (via middleware) and the
  gateway's own outbound calls.

Reachability is measured with ``list_tools()`` rather than ``ping()`` on purpose:
on the sessionless ``2026-07-28`` protocol era, which FastMCP clients negotiate by
default, ``ping`` is not routed and raises ``Method not found``. ``list_tools()``
is meaningful on both eras, and it doubles as an inventory refresh.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from fastmcp import Client, FastMCP
from fastmcp.server.providers.proxy import ProxyClient, ProxyMetadataMiddleware, ProxyProvider

from .logging_utils import get_logger, log_event
from .metrics import MetricsRecorder, Timer
from .registry import Registry, ServerEntry

#: Upstream states reported by the health endpoint.
STATE_UNKNOWN = "unknown"
STATE_OK = "ok"
STATE_DEGRADED = "degraded"
STATE_DOWN = "down"


@dataclass
class UpstreamStatus:
    """Last known reachability of one upstream."""

    state: str = STATE_UNKNOWN
    consecutive_failures: int = 0
    last_checked_at: datetime | None = None
    last_ok_at: datetime | None = None
    latency_ms: float | None = None
    last_error: str | None = None
    tool_count: int | None = None

    def as_dict(self, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(tz=timezone.utc)
        return {
            "state": self.state,
            "latency_ms": self.latency_ms,
            "tool_count": self.tool_count,
            "consecutive_failures": self.consecutive_failures,
            "last_checked_at": self.last_checked_at.isoformat(timespec="seconds") if self.last_checked_at else None,
            "last_ok_at": self.last_ok_at.isoformat(timespec="seconds") if self.last_ok_at else None,
            "age_seconds": round((now - self.last_checked_at).total_seconds(), 1) if self.last_checked_at else None,
            "last_error": self.last_error,
        }


@dataclass
class ToolSummary:
    """One tool advertised by an upstream, as exposed through the gateway."""

    name: str
    gateway_name: str
    description: str = ""
    parameters: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "gateway_name": self.gateway_name,
            "description": self.description,
            "parameters": self.parameters,
        }


@runtime_checkable
class PoolLike(Protocol):
    """The slice of the pool that the health endpoint and the catalog need.

    Typing those consumers against this Protocol keeps them testable with a stub
    that never opens a socket.
    """

    registry: Registry

    def statuses(self) -> dict[str, "UpstreamStatus"]: ...

    def status(self, name: str) -> "UpstreamStatus": ...

    async def probe_all(self, *, timeout: float | None = None) -> dict[str, "UpstreamStatus"]: ...

    async def inventory(self, name: str, *, force: bool = False) -> list["ToolSummary"]: ...


class UpstreamPool:
    """Owns per-upstream proxy plumbing, status, semaphores and inventory cache."""

    def __init__(self, registry: Registry, metrics: MetricsRecorder) -> None:
        self.registry = registry
        self.metrics = metrics
        self._logger = get_logger("upstreams")
        self._providers: dict[str, ProxyProvider] = {}
        self._proxy_servers: dict[str, FastMCP] = {}
        self._status: dict[str, UpstreamStatus] = {entry.name: UpstreamStatus() for entry in registry.enabled_servers()}
        self._semaphores: dict[str, asyncio.Semaphore] = {
            entry.name: asyncio.Semaphore(entry.max_concurrency) for entry in registry.enabled_servers()
        }
        self._global_semaphore = asyncio.Semaphore(registry.gateway.concurrency.global_limit)
        self._inventory_cache: dict[str, tuple[float, list[ToolSummary]]] = {}
        self._prober_task: asyncio.Task[None] | None = None

    # -- composition -------------------------------------------------------

    def proxy_server(self, entry: ServerEntry) -> FastMCP:
        """Build (once) the server that exposes one upstream, ready to be mounted.

        Constructed from an explicit ``ProxyProvider`` rather than
        ``create_proxy(...)`` so the gateway keeps a handle on the provider: the
        catalog resources read component lists through it, and each upstream keeps
        its own component-list cache TTL.
        """
        existing = self._proxy_servers.get(entry.name)
        if existing is not None:
            return existing

        provider = ProxyProvider(
            lambda: ProxyClient(entry.url, timeout=entry.timeout, mode="auto"),
            cache_ttl=entry.cache_ttl,
        )
        server = FastMCP(
            entry.name,
            instructions=entry.description or None,
            providers=[provider],
            # Forwards the upstream's instructions and namespaced _meta to clients
            # while keeping the gateway's own identity.
            middleware=[ProxyMetadataMiddleware(provider)],
        )
        self._providers[entry.name] = provider
        self._proxy_servers[entry.name] = server
        return server

    @property
    def mounted_servers(self) -> list[tuple[ServerEntry, FastMCP]]:
        return [(entry, self.proxy_server(entry)) for entry in self.registry.enabled_servers()]

    # -- limits ------------------------------------------------------------

    @property
    def global_semaphore(self) -> asyncio.Semaphore:
        return self._global_semaphore

    def semaphore(self, name: str) -> asyncio.Semaphore | None:
        return self._semaphores.get(name)

    def semaphores_for_tool(self, tool_name: str) -> list[asyncio.Semaphore]:
        """Semaphores a tool call must hold: gateway-wide first, then per upstream."""
        entry = self.registry.upstream_of_tool(tool_name)
        sems = [self._global_semaphore]
        if entry is not None and entry.name in self._semaphores:
            sems.append(self._semaphores[entry.name])
        return sems

    # -- reachability ------------------------------------------------------

    def status(self, name: str) -> UpstreamStatus:
        return self._status.setdefault(name, UpstreamStatus())

    def statuses(self) -> dict[str, UpstreamStatus]:
        return dict(self._status)

    async def probe(self, entry: ServerEntry, *, timeout: float | None = None) -> UpstreamStatus:
        """Check one upstream with a real round trip and update its status."""
        probe_timeout = timeout if timeout is not None else self.registry.gateway.health.probe_timeout_seconds
        status = self.status(entry.name)
        now = datetime.now(tz=timezone.utc)
        status.last_checked_at = now

        semaphore = self._semaphores.get(entry.name)
        timer = Timer()
        try:
            # The per-upstream semaphore bounds probes together with tool calls, so
            # a slow upstream cannot accumulate unbounded concurrent connections.
            # The gateway-wide tool-call budget is deliberately not consumed here:
            # health checks must not be starved by traffic.
            async with _optional_semaphore(semaphore):
                client = Client(entry.url, timeout=probe_timeout, mode="auto")
                async with client:
                    tools = await client.list_tools()
        except Exception as exc:  # any failure means "not reachable right now"
            status.consecutive_failures += 1
            status.last_error = f"{type(exc).__name__}: {exc}"[:300]
            status.state = STATE_DOWN if status.consecutive_failures >= self.registry.gateway.health.failure_threshold else STATE_DEGRADED
            self.metrics.record_upstream_call(
                upstream=entry.name, operation="probe", seconds=timer.elapsed, error=exc, url=entry.url
            )
            return status

        status.consecutive_failures = 0
        status.state = STATE_OK
        status.last_ok_at = now
        status.last_error = None
        status.latency_ms = round(timer.elapsed * 1000, 2)
        status.tool_count = len(tools)
        self.metrics.record_upstream_call(
            upstream=entry.name,
            operation="probe",
            seconds=timer.elapsed,
            tool_count=len(tools),
            url=entry.url,
        )
        return status

    async def probe_all(self, *, timeout: float | None = None) -> dict[str, UpstreamStatus]:
        """Probe every enabled upstream concurrently."""
        entries = self.registry.enabled_servers()
        if not entries:
            return {}
        results = await asyncio.gather(*(self.probe(entry, timeout=timeout) for entry in entries))
        return {entry.name: result for entry, result in zip(entries, results, strict=True)}

    def start_prober(self) -> None:
        """Start the background reachability loop (no-op if already running)."""
        settings = self.registry.gateway.health
        if not settings.enabled or settings.probe_interval_seconds <= 0:
            return
        if self._prober_task is not None and not self._prober_task.done():
            return
        self._prober_task = asyncio.create_task(self._prober_loop())

    async def stop_prober(self) -> None:
        task = self._prober_task
        self._prober_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _prober_loop(self) -> None:
        interval = self.registry.gateway.health.probe_interval_seconds
        log_event(
            self._logger,
            "prober_started",
            "upstream reachability prober started",
            interval_seconds=interval,
            upstreams=[entry.name for entry in self.registry.enabled_servers()],
        )
        try:
            while True:
                await self.probe_all()
                log_event(
                    self._logger,
                    "probe_cycle",
                    "reachability cycle complete",
                    states={name: status.state for name, status in self._status.items()},
                )
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            log_event(self._logger, "prober_stopped", "upstream reachability prober stopped")
            raise

    # -- inventory ---------------------------------------------------------

    async def inventory(self, name: str, *, force: bool = False) -> list[ToolSummary]:
        """Tool inventory for one upstream, cached for ``cache_ttl`` seconds."""
        entry = self.registry.by_name(name)
        cached = self._inventory_cache.get(name)
        now = asyncio.get_running_loop().time()
        ttl = min(entry.cache_ttl, self.registry.gateway.catalog.inventory_stale_after_seconds)
        if cached is not None and not force and (now - cached[0]) < ttl:
            return cached[1]

        self.proxy_server(entry)  # ensures the provider exists
        provider = self._providers[name]
        semaphore = self._semaphores.get(name)
        timer = Timer()
        try:
            async with _optional_semaphore(semaphore):
                tools = await asyncio.wait_for(provider.list_tools(), timeout=entry.timeout)
        except Exception as exc:
            self.metrics.record_upstream_call(upstream=name, operation="inventory", seconds=timer.elapsed, error=exc)
            log_event(
                self._logger,
                "inventory_failed",
                f"could not list tools from {name}",
                level=40,  # logging.WARNING
                upstream=name,
                duration_ms=round(timer.elapsed * 1000, 2),
                error=f"{type(exc).__name__}: {exc}"[:300],
            )
            if cached is not None:
                return cached[1]
            raise

        summaries = [self._summarise(entry, tool) for tool in tools]
        self._inventory_cache[name] = (now, summaries)
        self.metrics.record_upstream_call(
            upstream=name, operation="inventory", seconds=timer.elapsed, tool_count=len(summaries)
        )
        if self.status(name).tool_count is None:
            self.status(name).tool_count = len(summaries)
        return summaries

    @staticmethod
    def _summarise(entry: ServerEntry, tool: Any) -> ToolSummary:
        """Summarise an upstream tool from whatever object the provider returned.

        FastMCP's internal component objects (``ProxyTool``, ``FunctionTool``) carry
        the JSON schema in ``parameters``; the protocol-facing ``Tool`` model from
        ``mcp.types`` calls it ``input_schema``. Both are read here so the catalog
        does not silently report an empty parameter list, which is exactly what
        happened when only ``input_schema`` was consulted.
        """
        schema = _input_schema_of(tool)
        properties = schema.get("properties") if isinstance(schema, dict) else None
        return ToolSummary(
            name=getattr(tool, "name", "<unnamed>"),
            gateway_name=f"{entry.name}_{getattr(tool, 'name', '<unnamed>')}",
            description=(getattr(tool, "description", "") or "").strip(),
            parameters=sorted(properties) if isinstance(properties, dict) else [],
        )

    def cached_inventory(self, name: str) -> list[ToolSummary] | None:
        cached = self._inventory_cache.get(name)
        return cached[1] if cached is not None else None


class _optional_semaphore:
    """``async with`` on a possibly-absent semaphore."""

    def __init__(self, semaphore: asyncio.Semaphore | None) -> None:
        self._semaphore = semaphore

    async def __aenter__(self) -> None:
        if self._semaphore is not None:
            await self._semaphore.acquire()

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._semaphore is not None:
            self._semaphore.release()
        return None


def _input_schema_of(tool: Any) -> dict[str, Any]:
    """Return a tool's input JSON schema, whichever name the object uses.

    Checked in order of reliability:

    1. ``parameters`` — FastMCP's own component model (``ProxyTool``, ``FunctionTool``);
    2. ``input_schema`` / ``inputSchema`` — the protocol model, and the camelCase
       alias that FastMCP's compatibility bridge still resolves;
    3. ``to_mcp_tool().input_schema`` — the documented protocol-facing conversion,
       used as a last resort for objects that expose neither attribute.
    """
    for attribute in ("parameters", "input_schema", "inputSchema"):
        candidate = getattr(tool, attribute, None)
        if isinstance(candidate, dict) and candidate:
            return candidate

    to_mcp_tool = getattr(tool, "to_mcp_tool", None)
    if callable(to_mcp_tool):
        try:
            mcp_tool = to_mcp_tool()
        except Exception:  # pragma: no cover - defensive, a broken tool must not break the catalog
            return {}
        for attribute in ("input_schema", "inputSchema", "parameters"):
            candidate = getattr(mcp_tool, attribute, None)
            if isinstance(candidate, dict) and candidate:
                return candidate

    return {}
