"""Assembly and CLI behaviour."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from mcpgateway.catalog import CATALOG_SERVERS_URI, register_catalog_resources
from mcpgateway.health import register_health_route
from mcpgateway.metrics import MetricsRecorder
from mcpgateway.registry import Registry
from mcpgateway.server import _registry_check, build_gateway, build_instructions, main
from mcpgateway.upstreams import UpstreamPool

from support import StubPool


def test_check_mode_prints_pure_json(tmp_path: Path, capsys: pytest.CaptureFixture[str], registry: Registry) -> None:
    """--check feeds CI: stdout must contain JSON and nothing else."""
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(_yaml_for(registry), encoding="utf-8")

    exit_code = main(["--registry", str(registry_path), "--check"])
    captured = capsys.readouterr()

    assert exit_code == 0
    payload = json.loads(captured.out)  # raises if a log line leaked into stdout
    assert payload["gateway"] == "Test Gateway"
    assert payload["enabled"] == ["alpha"]
    assert payload["disabled"] == ["beta"]
    assert payload["domains"] == {"geographie": ["beta"], "stats": ["alpha"]}
    assert payload["features"]["health"] is True


def _yaml_for(registry: Registry) -> str:
    import yaml

    return yaml.safe_dump(
        {
            "gateway": {"name": registry.gateway.name},
            "servers": [
                {
                    "name": entry.name,
                    "url": entry.url,
                    "domain": entry.domain,
                    "enabled": entry.enabled,
                    **({"description": entry.description} if entry.description else {}),
                    **({"tags": sorted(entry.tags)} if entry.tags else {}),
                }
                for entry in registry.servers
            ],
        },
        allow_unicode=True,
    )


def test_missing_registry_exits_with_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["--registry", str(tmp_path / "absent.yaml")])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "no registry file found" in captured.err


def test_registry_check_summarises_limits(registry: Registry) -> None:
    payload = _registry_check(registry)

    assert payload["limits"]["global_concurrency"] == registry.gateway.concurrency.global_limit
    alpha = next(entry for entry in payload["servers"] if entry["name"] == "alpha")
    assert alpha["limits"] == {"timeout_seconds": 30.0, "max_concurrency": 4, "cache_ttl_seconds": 300.0}


def test_instructions_describe_servers_domains_and_discovery(registry: Registry) -> None:
    text = build_instructions(registry)

    assert "alpha" in text
    assert "stats" in text
    assert "beta" in text  # disabled servers are named as such
    assert "désactivés" in text
    assert CATALOG_SERVERS_URI in text
    assert "catalog://guide" in text
    assert "recherche" in text  # tool-search is enabled by default
    # The old implementation dumped Python reprs of the metadata dict into the prompt.
    assert "{'" not in text


def test_instructions_without_any_enabled_server(make_registry: Callable[..., Registry]) -> None:
    text = build_instructions(make_registry(servers=[{"name": "alpha", "url": "https://a.example.org/mcp", "domain": "x", "enabled": False}]))
    assert "Aucun serveur amont n'est activé" in text


def test_instructions_omit_the_guide_when_it_is_disabled(make_registry: Callable[..., Registry]) -> None:
    text = build_instructions(make_registry(gateway={"catalog": {"include_guide": False}}))

    assert "catalog://servers" in text
    assert "catalog://guide" not in text


def test_build_gateway_mounts_only_enabled_upstreams_without_touching_the_network(
    registry: Registry,
) -> None:
    gateway, pool, metrics = build_gateway(registry)

    assert gateway.name == registry.gateway.name
    assert [entry.name for entry, _server in pool.mounted_servers] == ["alpha"]
    assert isinstance(metrics, MetricsRecorder)
    assert pool.statuses().keys() == {"alpha"}


def test_build_gateway_registers_health_and_catalog(registry: Registry) -> None:
    gateway, pool, metrics = build_gateway(registry)

    # build_gateway already registered the route; registering it again on a fresh
    # gateway must produce the same path, and the catalog URIs stay un-namespaced.
    assert gateway.name == registry.gateway.name
    assert register_health_route(_gateway_for(registry), registry=registry, pool=pool, metrics=metrics) == "/health"

    fresh_gateway = _gateway_for(registry)
    registered = register_catalog_resources(
        fresh_gateway, registry=registry, pool=StubPool(registry), metrics=metrics
    )
    assert CATALOG_SERVERS_URI in registered


def _gateway_for(registry: Registry) -> Any:
    from fastmcp import FastMCP

    return FastMCP(registry.gateway.name)


def test_build_gateway_respects_disabled_features(make_registry: Callable[..., Registry]) -> None:
    registry = make_registry(
        gateway={
            "health": {"enabled": False},
            "catalog": {"enabled": False},
            "tool_search": {"enabled": False},
        }
    )
    gateway, pool, metrics = build_gateway(registry)

    assert register_health_route(gateway, registry=registry, pool=pool, metrics=metrics) is None
    assert (
        register_catalog_resources(gateway, registry=registry, pool=StubPool(registry), metrics=metrics) == []
    )


def test_pool_and_registry_are_wired_together(registry: Registry) -> None:
    _gateway, pool, _metrics = build_gateway(registry)

    assert isinstance(pool, UpstreamPool)
    assert pool.registry is registry
    # The concurrency ceiling comes from the registry, not from a hard-coded default.
    assert pool.global_semaphore._value == registry.gateway.concurrency.global_limit  # noqa: SLF001
