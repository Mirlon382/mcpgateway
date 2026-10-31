"""The gateway guide: composition, guarding its content against drift."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

import pytest
from fastmcp import Client

from mcpgateway.catalog import (
    CATALOG_GUIDE_URI,
    GUIDE_SEPARATOR,
    compose_guide,
    packaged_guide_text,
    register_catalog_resources,
)
from mcpgateway.metrics import MetricsRecorder
from mcpgateway.registry import Registry, RegistryError, load_registry

from support import StubPool

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT_GUIDE = REPO_ROOT / "guide-ssp.md"
REPO_REGISTRY = REPO_ROOT / "registry.yaml"


# --- packaged guide --------------------------------------------------------


def test_packaged_guide_is_importable_from_the_installed_package() -> None:
    """Guards packaging: the file must be inside the package, not just the source tree."""
    text = packaged_guide_text()

    assert text.startswith("---")  # frontmatter, ready for a skills wrapper later
    assert "name:" in text.split("---")[1]
    assert "catalog://servers" in text


def test_packaged_guide_stays_deployment_agnostic() -> None:
    """The packaged guide is shared by every deployment, so it must not name one."""
    text = packaged_guide_text()
    registry = load_registry(REPO_REGISTRY).registry

    for entry in registry.servers:
        assert entry.name not in text, f"packaged guide mentions the deployment-specific server {entry.name!r}"
        assert entry.url not in text


def test_compose_guide_without_a_deployment_guide_returns_the_packaged_text() -> None:
    assert compose_guide(None) == packaged_guide_text().strip()


def test_compose_guide_appends_the_deployment_guide(tmp_path: Path) -> None:
    deployment = tmp_path / "deploiement.md"
    deployment.write_text("# Démarches\n\n1. géocoder\n", encoding="utf-8")

    text = compose_guide(deployment)

    assert text.index("catalog://servers") < text.index("# Démarches")  # mechanics first
    assert GUIDE_SEPARATOR in text
    assert text.endswith("1. géocoder")


def test_compose_guide_reports_an_unreadable_deployment_guide(tmp_path: Path) -> None:
    missing = tmp_path / "disparu.md"

    text = compose_guide(missing)

    assert "Guide de déploiement illisible" in text
    assert "catalog://servers" in text  # the packaged part is still served


# --- the resource ----------------------------------------------------------


async def test_guide_resource_is_served(make_registry: Callable[..., Registry]) -> None:
    from fastmcp import FastMCP

    gateway = FastMCP("Guide Gateway")
    register_catalog_resources(gateway, registry=make_registry(), pool=StubPool(make_registry()), metrics=MetricsRecorder())

    async with Client(gateway) as client:
        text = (await client.read_resource(CATALOG_GUIDE_URI))[0].text  # type: ignore[union-attr]

    assert "search_tools" in text
    assert "{serveur}_{outil}" in text
    assert "Gateway is busy" in text  # the concurrency gotcha is documented


async def test_guide_resource_composes_the_deployment_guide(
    make_registry: Callable[..., Registry], tmp_path: Path
) -> None:
    from fastmcp import FastMCP

    deployment = tmp_path / "deploiement.md"
    deployment.write_text("# Démarches de cette installation\n", encoding="utf-8")
    registry = make_registry(gateway={"catalog": {"guide_path": str(deployment)}})

    gateway = FastMCP("Guide Gateway")
    register_catalog_resources(gateway, registry=registry, pool=StubPool(registry), metrics=MetricsRecorder())

    async with Client(gateway) as client:
        text = (await client.read_resource(CATALOG_GUIDE_URI))[0].text  # type: ignore[union-attr]

    assert "Démarches de cette installation" in text


def test_guide_can_be_switched_off(make_registry: Callable[..., Registry]) -> None:
    from fastmcp import FastMCP

    registry = make_registry(gateway={"catalog": {"include_guide": False, "include_tool_inventory": False}})
    gateway = FastMCP("No Guide Gateway")

    registered = register_catalog_resources(
        gateway, registry=registry, pool=StubPool(registry), metrics=MetricsRecorder()
    )

    assert CATALOG_GUIDE_URI not in registered


# --- drift guards on the deployment content --------------------------------

#: Gateway-authored tools, not upstream ones: excluded from server-prefix checks.
GATEWAY_OWN_TOOLS = {"search_tools", "call_tool", "execute", "list_resources", "read_resource"}


def _referenced_names(text: str) -> set[str]:
    """Tool names the guide tells a caller to invoke (call-shaped mentions)."""
    return set(re.findall(r"\b([a-z][a-z0-9_]*_[a-z][a-z0-9_]*)\s*\(", text)) - GATEWAY_OWN_TOOLS


def _server_of(name: str, registry: Registry) -> str | None:
    """Which registered server owns a gateway tool name (longest prefix wins)."""
    for entry in sorted(registry.servers, key=lambda entry: len(entry.name), reverse=True):
        if name.startswith(f"{entry.name}_"):
            return entry.name
    return None


def _unknown_servers(names: set[str], registry: Registry) -> set[str]:
    """Referenced names owned by no *enabled* server — the guide is then misleading.

    This is what fires the day a server leaves the registry (or is renamed) while the
    guide still sends callers to it.
    """
    enabled = {entry.name for entry in registry.enabled_servers()}
    return {name for name in names if _server_of(name, registry) not in enabled}


def test_deployment_guide_only_names_servers_that_are_enabled() -> None:
    registry = load_registry(REPO_REGISTRY).registry
    names = _referenced_names(DEPLOYMENT_GUIDE.read_text(encoding="utf-8"))

    assert len(names) >= 10, "the deployment guide should reference several real tools"
    unknown = _unknown_servers(names, registry)
    assert not unknown, (
        "the deployment guide names tools of servers that are not enabled in registry.yaml "
        f"(edit guide-ssp.md when a server is removed or renamed): {sorted(unknown)}"
    )


def test_the_guard_fires_when_a_documented_server_is_removed() -> None:
    """Simulates the day `geodata` leaves the registry: the check above must fail."""
    registry = Registry.model_validate(
        {
            "gateway": {"name": "Test Gateway"},
            "servers": [{"name": "mcpdiffusion", "url": "https://example.test/mcp", "domain": "diffusion"}],
        }
    )

    unknown = _unknown_servers(_referenced_names(DEPLOYMENT_GUIDE.read_text(encoding="utf-8")), registry)

    assert unknown, "removing a documented server must be caught"
    assert all(name.startswith("geodata_") for name in unknown)


def test_deployment_guide_defers_the_inventory_to_the_catalogue() -> None:
    """The guide holds narrative, not a copy of the tool list that can go stale."""
    text = DEPLOYMENT_GUIDE.read_text(encoding="utf-8")

    assert "catalog://servers" in text
    assert "catalog://servers/mcpdiffusion/tools" in text
    assert "catalog://servers/geodata/tools" in text


def test_registry_rejects_a_missing_guide_path(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(
        "gateway:\n  catalog: {guide_path: absent.md}\nservers: []\n",
        encoding="utf-8",
    )

    with pytest.raises(RegistryError) as excinfo:
        load_registry(registry_path)

    assert "guide_path" in str(excinfo.value)


def test_registry_resolves_a_relative_guide_path_next_to_the_registry(tmp_path: Path) -> None:
    (tmp_path / "deploiement.md").write_text("# Démarches\n", encoding="utf-8")
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text("gateway:\n  catalog: {guide_path: deploiement.md}\nservers: []\n", encoding="utf-8")

    registry = load_registry(registry_path).registry

    resolved = registry.gateway.catalog.guide_path
    assert resolved is not None
    assert Path(resolved).is_absolute()
    assert Path(resolved) == (tmp_path / "deploiement.md").resolve()


@pytest.mark.integration
async def test_deployment_guide_tool_names_still_exist_upstream() -> None:
    """Live guard: every tool the guide names must exist in the upstream's inventory.

    Skipped when an upstream is unreachable, so the suite stays usable offline.
    """
    registry = load_registry(REPO_REGISTRY).registry
    names = _referenced_names(DEPLOYMENT_GUIDE.read_text(encoding="utf-8"))

    for entry in registry.enabled_servers():
        expected = {
            name[len(entry.name) + 1 :] for name in names if _server_of(name, registry) == entry.name
        }
        if not expected:
            continue
        try:
            async with Client(entry.url, timeout=30.0) as client:
                live = {tool.name for tool in await client.list_tools()}
        except Exception as exc:  # noqa: BLE001 - any failure means "cannot check now"
            pytest.skip(f"upstream {entry.name} unreachable: {type(exc).__name__}: {exc}")

        missing = expected - live
        assert not missing, f"guide references tools that no longer exist on {entry.name}: {sorted(missing)}"
