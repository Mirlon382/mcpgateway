"""Gateway assembly and command-line entry point.

Run with ``uv run mcpgateway`` (HTTP transport by default) after writing a
``registry.yaml``. The assembly is deliberately explicit:

1. load and validate the registry (spec 1);
2. mount one proxy per enabled upstream, namespaced by server name;
3. add the tool-search transform so agents get discovery instead of the whole
   catalog in their context;
4. add metrics middleware and the concurrency ceiling (specs 3 and 4);
5. register the health route (spec 2) and the catalog resources (spec 5).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import AsyncIterator, Sequence
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan
from fastmcp.server.transforms.search import BM25SearchTransform

from .catalog import register_catalog_resources
from .feedback import register_feedback_tool
from .health import register_health_route
from .logging_utils import configure_logging, get_logger, log_event
from .metrics import MetricsRecorder
from .middleware import ConcurrencyLimitMiddleware, MetricsMiddleware
from .registry import Registry, RegistryError, load_registry
from .upstreams import UpstreamPool

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
DEFAULT_MCP_PATH = "/mcp"


def build_instructions(registry: Registry) -> str:
    """Gateway instructions handed to every client (and to the LLM behind it)."""
    enabled = registry.enabled_servers()
    catalogue_uri = "catalog://servers"

    lines: list[str] = []
    if registry.gateway.instructions.strip():
        lines.append(registry.gateway.instructions.strip())
    else:
        lines.append(
            "Passerelle MCP du Service Statistique Public : agrège les serveurs MCP "
            "des producteurs de statistiques publiques derrière un point d'entrée unique."
        )

    if enabled:
        lines.append("")
        lines.append(
            "Chaque outil est préfixé par le nom du serveur d'origine "
            "(<serveur>_<outil>) ; le préfixe identifie la source à interroger."
        )
        lines.append("Serveurs disponibles :")
        for entry in enabled:
            description = f" — {entry.description}" if entry.description else ""
            tags = f" [tags: {', '.join(sorted(entry.tags))}]" if entry.tags else ""
            lines.append(f"  - {entry.name} (domaine {entry.domain}){description}{tags}")
        disabled = registry.disabled_servers()
        if disabled:
            lines.append(
                "Serveurs désactivés (présents dans le registre mais non exposés) : "
                + ", ".join(entry.name for entry in disabled)
            )
        lines.append("")
        if registry.gateway.tool_search.enabled:
            lines.append(
                "Le catalogue d'outils est masqué derrière la recherche : utilisez les outils "
                "de recherche pour découvrir un outil avant de l'appeler."
            )
        if registry.gateway.catalog.enabled:
            guide_hint = (
                " Guide d'utilisation (nommage, découverte, démarches types) : lisez la ressource "
                "catalog://guide."
                if registry.gateway.catalog.include_guide
                else ""
            )
            lines.append(
                f"Le catalogue complet est lisible comme ressources MCP : {catalogue_uri} "
                f"(serveurs, domaines, et inventaire des outils par serveur).{guide_hint}"
            )
    else:
        lines.append("")
        lines.append("Aucun serveur amont n'est activé dans le registre.")

    if registry.servers:
        lines.append(
            "Pour signaler un problème lié à un serveur, appelez `send_feedback` avec son nom, "
            "l'auteur et le rapport ; celui-ci est enregistré dans le journal de la passerelle."
        )

    return "\n".join(lines).strip()


def build_gateway(registry: Registry) -> tuple[FastMCP, UpstreamPool, MetricsRecorder]:
    """Build the gateway server, its upstream pool and its metrics recorder."""
    logger = get_logger("server")
    metrics = MetricsRecorder(sample_window=registry.gateway.metrics.sample_window)
    pool = UpstreamPool(registry, metrics)

    @lifespan
    async def gateway_lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        pool.start_prober()
        log_event(
            logger,
            "gateway_started",
            f"gateway {registry.gateway.name} started",
            gateway=registry.gateway.name,
            upstreams=[entry.name for entry in registry.enabled_servers()],
            disabled=[entry.name for entry in registry.disabled_servers()],
            tool_search=registry.gateway.tool_search.enabled,
            catalog=registry.gateway.catalog.enabled,
            health=registry.gateway.health.enabled,
            global_concurrency=registry.gateway.concurrency.global_limit,
        )
        try:
            yield {"registry": registry, "pool": pool, "metrics": metrics}
        finally:
            await pool.stop_prober()
            log_event(
                logger,
                "gateway_stopped",
                f"gateway {registry.gateway.name} stopped",
                totals=metrics.totals(),
            )

    gateway = FastMCP(
        registry.gateway.name,
        instructions=build_instructions(registry),
        lifespan=gateway_lifespan,
        middleware=[
            # Metrics outermost: every call is counted, including the ones the
            # concurrency limiter rejects. Queue wait is recorded separately as
            # the "slot_wait" upstream operation.
            MetricsMiddleware(pool, metrics),
            ConcurrencyLimitMiddleware(pool),
        ],
    )

    for entry, proxy_server in pool.mounted_servers:
        gateway.mount(proxy_server, namespace=entry.name)

    if registry.gateway.tool_search.enabled:
        gateway.add_transform(BM25SearchTransform(max_results=registry.gateway.tool_search.max_results))

    register_feedback_tool(gateway, registry=registry)
    health_path = register_health_route(gateway, registry=registry, pool=pool, metrics=metrics)
    catalog_uris = register_catalog_resources(gateway, registry=registry, pool=pool, metrics=metrics)

    log_event(
        logger,
        "gateway_assembled",
        "gateway assembled",
        upstreams=[entry.name for entry in registry.enabled_servers()],
        health_path=health_path,
        catalog_resources=catalog_uris,
    )
    return gateway, pool, metrics


def _registry_check(registry: Registry) -> dict[str, Any]:
    """Machine-readable summary used by ``--check``."""
    return {
        "gateway": registry.gateway.name,
        "servers": [
            {
                "name": entry.name,
                "domain": entry.domain,
                "url": entry.url,
                "transport": entry.transport,
                "enabled": entry.enabled,
                "tags": sorted(entry.tags),
                "limits": {
                    "timeout_seconds": entry.timeout,
                    "max_concurrency": entry.max_concurrency,
                    "cache_ttl_seconds": entry.cache_ttl,
                },
            }
            for entry in registry.servers
        ],
        "domains": {domain: [entry.name for entry in entries] for domain, entries in registry.domains().items()},
        "enabled": [entry.name for entry in registry.enabled_servers()],
        "disabled": [entry.name for entry in registry.disabled_servers()],
        "features": {
            "tool_search": registry.gateway.tool_search.enabled,
            "catalog": registry.gateway.catalog.enabled,
            "tool_inventory": registry.gateway.catalog.include_tool_inventory,
            "guide": registry.gateway.catalog.include_guide,
            "health": registry.gateway.health.enabled,
        },
        "guide_path": registry.gateway.catalog.guide_path,
        "limits": {
            "global_concurrency": registry.gateway.concurrency.global_limit,
            "acquire_timeout_seconds": registry.gateway.concurrency.acquire_timeout_seconds,
            "probe_interval_seconds": registry.gateway.health.probe_interval_seconds,
        },
    }


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mcpgateway",
        description="Passerelle MCP agrégent les serveurs MCP du Service Statistique Public.",
    )
    parser.add_argument("--registry", "-c", default=None, help="path to the registry YAML file")
    parser.add_argument("--transport", "-t", default="http", choices=["http", "stdio", "sse"])
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", "-p", type=int, default=DEFAULT_PORT)
    parser.add_argument("--path", default=DEFAULT_MCP_PATH, help="MCP endpoint path (HTTP transport)")
    parser.add_argument("--log-level", default=None, help="override gateway.metrics.log_level")
    parser.add_argument("--text-logs", action="store_true", help="human-readable logs instead of JSON")
    parser.add_argument("--check", action="store_true", help="validate the registry and print a summary, then exit")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point."""
    args = _parse_args(argv)

    try:
        loaded = load_registry(args.registry)
    except RegistryError as exc:
        # Logging is not configured yet: this must work with plain stderr.
        print(f"mcpgateway: {exc}", file=sys.stderr)
        return 2

    registry = loaded.registry

    # --check is consumed by scripts and CI: keep stdout pure JSON and configure
    # logging only once we are actually going to serve.
    if args.check:
        print(json.dumps(_registry_check(registry), ensure_ascii=False, indent=2))
        return 0

    configure_logging(
        args.log_level or registry.gateway.metrics.log_level,
        json_logs=not args.text_logs and registry.gateway.metrics.json_logs,
    )
    logger = get_logger("main")
    log_event(logger, "registry_loaded", f"registry loaded from {loaded.path}", path=str(loaded.path))

    gateway, _pool, _metrics = build_gateway(registry)

    run_kwargs: dict[str, Any] = {"transport": args.transport}
    if args.transport in {"http", "sse"}:
        run_kwargs.update(host=args.host, port=args.port)
        if args.transport == "http":
            run_kwargs["path"] = args.path

    log_event(
        logger,
        "gateway_listening",
        f"listening on {args.transport}",
        transport=args.transport,
        host=args.host if args.transport in {"http", "sse"} else None,
        port=args.port if args.transport in {"http", "sse"} else None,
        mcp_path=run_kwargs.get("path"),
    )
    gateway.run(**run_kwargs)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
