"""Catalog MCP resources (spec 5).

The gateway publishes what it aggregates as **resources**, not tools, so the
catalog costs a client nothing until it asks: reading a resource is an explicit
``resources/read``. This matters because the default configuration hides the
upstream tool list behind the tool-search transform (``tools/list`` returns two
synthetic discovery tools), which leaves the resources as the way to enumerate
what actually exists.

Every resource is registered only when ``gateway.catalog.enabled`` is true, and
the per-server tool inventories additionally require
``gateway.catalog.include_tool_inventory``:

* ``catalog://servers`` — every server: metadata, domain, enablement, reachability;
* ``catalog://domains`` — servers grouped by functional domain;
* ``catalog://servers/{name}`` — one server in detail, with its own metrics;
* ``catalog://servers/{name}/tools`` — its tool inventory (names as the gateway
  exposes them, i.e. prefixed with the server namespace).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from importlib.resources import files as _package_files
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from .logging_utils import get_logger, log_event
from .metrics import MetricsRecorder
from .registry import Registry, ServerEntry
from .upstreams import PoolLike

CATALOG_SERVERS_URI = "catalog://servers"
CATALOG_DOMAINS_URI = "catalog://domains"
CATALOG_SERVER_URI = "catalog://servers/{name}"
CATALOG_SERVER_TOOLS_URI = "catalog://servers/{name}/tools"
CATALOG_GUIDE_URI = "catalog://guide"

#: Mechanics guide shipped inside the package. It is deliberately free of
#: deployment specifics (no upstream or tool names), so it stays true for any
#: registry; a deployment adds its own narrative with ``catalog.guide_path``.
PACKAGED_GUIDE_NAME = "guide.md"

#: Separator between the packaged guide and a deployment guide.
GUIDE_SEPARATOR = "\n\n---\n\n"


def packaged_guide_text() -> str:
    """Read the packaged mechanics guide."""
    return _package_files("mcpgateway").joinpath(PACKAGED_GUIDE_NAME).read_text(encoding="utf-8")


def compose_guide(guide_path: str | Path | None = None, *, logger: Any = None) -> str:
    """Compose the served guide: packaged mechanics, then the deployment guide.

    The packaged part is read once per call rather than cached, so a release that
    fixes the mechanics takes effect on the next read; the deployment part is read
    per call too, so an operator can edit it without restarting the gateway.
    """
    log = logger or get_logger("catalog")

    try:
        parts = [packaged_guide_text().strip()]
    except (FileNotFoundError, OSError) as exc:  # pragma: no cover - packaging bug
        log_event(
            log,
            "guide_packaged_missing",
            "the packaged guide could not be read",
            level=30,  # logging.WARNING
            error=f"{type(exc).__name__}: {exc}",
        )
        parts = ["# Guide indisponible\n\nLe guide générique n'a pas pu être chargé."]

    if guide_path:
        try:
            parts.append(Path(guide_path).read_text(encoding="utf-8").strip())
        except OSError as exc:
            # Validated at startup, so this only happens if the file disappeared.
            log_event(
                log,
                "guide_deployment_unreadable",
                f"deployment guide {guide_path} could not be read",
                level=30,  # logging.WARNING
                path=str(guide_path),
                error=f"{type(exc).__name__}: {exc}",
            )
            parts.append(f"> ⚠️ Guide de déploiement illisible ({guide_path}) : {type(exc).__name__}.")

    return GUIDE_SEPARATOR.join(part for part in parts if part)


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _server_summary(entry: ServerEntry, pool: PoolLike) -> dict[str, Any]:
    status = pool.status(entry.name) if entry.enabled else None
    return {
        "name": entry.name,
        "domain": entry.domain,
        "description": entry.description,
        "tags": sorted(entry.tags),
        "url": entry.url,
        "transport": entry.transport,
        "enabled": entry.enabled,
        "tool_prefix": entry.tool_prefix,
        "timeout_seconds": entry.timeout,
        "max_concurrency": entry.max_concurrency,
        "cache_ttl_seconds": entry.cache_ttl,
        "status": status.as_dict() if status is not None else {"state": "disabled"},
    }


def register_catalog_resources(
    gateway: FastMCP,
    *,
    registry: Registry,
    pool: PoolLike,
    metrics: MetricsRecorder,
) -> list[str]:
    """Register the catalog resources, returning the URIs that were added."""
    settings = registry.gateway.catalog
    if not settings.enabled:
        return []

    logger = get_logger("catalog")
    registered: list[str] = []

    @gateway.resource(
        CATALOG_SERVERS_URI,
        name="Gateway catalog",
        description="Every upstream MCP server aggregated by this gateway: metadata, domain, status.",
        mime_type="application/json",
        tags={"catalog"},
    )
    async def catalog_servers() -> str:
        entries = registry.servers
        enabled = [entry for entry in entries if entry.enabled]
        states = {name: status.state for name, status in pool.statuses().items()}
        payload = {
            "generated_at": _now(),
            "gateway": {
                "name": registry.gateway.name,
                "server_count": len(entries),
                "enabled_count": len(enabled),
                "disabled_count": len(entries) - len(enabled),
                "status": _overall_state(states),
            },
            "domains": sorted(registry.domains()),
            "servers": [_server_summary(entry, pool) for entry in entries],
        }
        log_event(logger, "catalog_read", "catalog://servers read", servers=len(entries), status=payload["gateway"]["status"])
        return _dump(payload)

    @gateway.resource(
        CATALOG_DOMAINS_URI,
        name="Gateway domains",
        description="Upstream servers grouped by functional domain.",
        mime_type="application/json",
        tags={"catalog"},
    )
    async def catalog_domains() -> str:
        grouped = registry.domains()
        payload = {
            "generated_at": _now(),
            "domains": [
                {
                    "domain": domain,
                    "server_count": len(entries),
                    "enabled_count": sum(1 for entry in entries if entry.enabled),
                    "servers": [entry.name for entry in entries],
                    "tags": sorted({tag for entry in entries for tag in entry.tags}),
                }
                for domain, entries in grouped.items()
            ],
        }
        log_event(logger, "catalog_read", "catalog://domains read", domains=len(grouped))
        return _dump(payload)

    @gateway.resource(
        CATALOG_SERVER_URI,
        name="Gateway server detail",
        description="One upstream server in detail, including its own latency and error metrics.",
        mime_type="application/json",
        tags={"catalog"},
    )
    async def catalog_server(name: str) -> str:
        entry = _require_server(registry, name)
        payload = {
            "generated_at": _now(),
            "server": _server_summary(entry, pool),
            "metrics": {
                "tool_calls": metrics.series_for(upstream=entry.name),
                "upstream_calls": metrics.upstream_series_for(entry.name),
            },
        }
        log_event(logger, "catalog_read", f"catalog://servers/{entry.name} read", upstream=entry.name)
        return _dump(payload)

    registered.extend([CATALOG_SERVERS_URI, CATALOG_DOMAINS_URI, CATALOG_SERVER_URI])

    if settings.include_guide:

        @gateway.resource(
            CATALOG_GUIDE_URI,
            name="Gateway guide",
            description=(
                "How to use this gateway: component naming, discovering the hidden tool catalogue, "
                "reading the other catalog:// resources, and the deployment's own workflows."
            ),
            mime_type="text/markdown",
            tags={"catalog", "guide"},
        )
        async def catalog_guide() -> str:
            text = compose_guide(settings.guide_path, logger=logger)
            log_event(
                logger,
                "catalog_read",
                "catalog://guide read",
                characters=len(text),
                deployment_guide=bool(settings.guide_path),
            )
            return text

        registered.append(CATALOG_GUIDE_URI)

    if settings.include_tool_inventory:

        @gateway.resource(
            CATALOG_SERVER_TOOLS_URI,
            name="Gateway tool inventory",
            description="Tool inventory of one upstream server, with the gateway-side tool names.",
            mime_type="application/json",
            tags={"catalog"},
        )
        async def catalog_server_tools(name: str) -> str:
            entry = _require_server(registry, name)
            if not entry.enabled:
                payload = {
                    "generated_at": _now(),
                    "server": entry.name,
                    "enabled": False,
                    "count": 0,
                    "tools": [],
                    "note": "server is disabled in the registry; no inventory is available",
                }
                return _dump(payload)

            try:
                tools = await pool.inventory(entry.name)
            except Exception as exc:
                payload = {
                    "generated_at": _now(),
                    "server": entry.name,
                    "enabled": True,
                    "status": "unavailable",
                    "count": 0,
                    "tools": [],
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                }
                log_event(
                    logger,
                    "catalog_inventory_unavailable",
                    f"tool inventory for {entry.name} unavailable",
                    level=30,  # logging.WARNING
                    upstream=entry.name,
                    error=f"{type(exc).__name__}: {exc}"[:300],
                )
                return _dump(payload)

            payload = {
                "generated_at": _now(),
                "server": entry.name,
                "domain": entry.domain,
                "enabled": True,
                "status": "ok",
                "count": len(tools),
                "tools": [tool.as_dict() for tool in tools],
            }
            log_event(
                logger,
                "catalog_read",
                f"catalog://servers/{entry.name}/tools read",
                upstream=entry.name,
                tool_count=len(tools),
            )
            return _dump(payload)

        registered.append(CATALOG_SERVER_TOOLS_URI)

    return registered


def _require_server(registry: Registry, name: str) -> ServerEntry:
    try:
        return registry.by_name(name)
    except KeyError as exc:
        known = ", ".join(entry.name for entry in registry.servers) or "<none>"
        raise ValueError(f"unknown server {name!r}; known servers: {known}") from exc


def _overall_state(states: dict[str, str]) -> str:
    if not states:
        return "empty"
    if all(state == "ok" for state in states.values()):
        return "ok"
    if any(state == "down" for state in states.values()):
        return "down"
    if any(state in {"degraded", "down"} for state in states.values()):
        return "degraded"
    return "unknown"
