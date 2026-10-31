"""Test helpers shared by the unit tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from mcpgateway.metrics import MetricsRecorder
from mcpgateway.registry import Registry, load_registry
from mcpgateway.upstreams import STATE_OK, STATE_UNKNOWN, ToolSummary, UpstreamStatus

DEFAULT_SERVERS: list[dict[str, Any]] = [
    {
        "name": "alpha",
        "url": "https://alpha.example.org/mcp",
        "domain": "stats",
        "description": "Serveur Alpha",
        "tags": ["dataset"],
    },
    {
        "name": "beta",
        "url": "https://beta.example.org/mcp",
        "domain": "geographie",
        "enabled": False,
    },
]


def build_registry(
    tmp_path: Path,
    *,
    servers: list[dict[str, Any]] | None = None,
    gateway: dict[str, Any] | None = None,
    name: str = "registry.yaml",
) -> Registry:
    """Write a registry document to ``tmp_path`` and load it through the real loader."""
    document: dict[str, Any] = {
        "gateway": gateway if gateway is not None else {"name": "Test Gateway"},
        "servers": DEFAULT_SERVERS if servers is None else servers,
    }
    path = tmp_path / name
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    return load_registry(path).registry


class StubPool:
    """Duck-typed stand-in for :class:`~mcpgateway.upstreams.UpstreamPool`.

    Lets the catalog and health payloads be tested without any network: the
    status map and the inventories are supplied by the test.
    """

    def __init__(
        self,
        registry: Registry,
        *,
        metrics: MetricsRecorder | None = None,
        states: dict[str, str] | None = None,
        inventories: dict[str, list[ToolSummary]] | None = None,
        inventory_error: Exception | None = None,
    ) -> None:
        self.registry = registry
        self.metrics = metrics or MetricsRecorder()
        self._states = states or {}
        self._inventories = inventories or {}
        self._inventory_error = inventory_error
        self.inventory_calls = 0
        self.probe_calls = 0

        self._statuses: dict[str, UpstreamStatus] = {}
        for entry in registry.enabled_servers():
            state = self._states.get(entry.name, STATE_UNKNOWN)
            status = UpstreamStatus(state=state)
            if state == STATE_OK:
                status.latency_ms = 42.0
                status.tool_count = len(self._inventories.get(entry.name, []))
            self._statuses[entry.name] = status

    # -- used by health.py -------------------------------------------------

    def statuses(self) -> dict[str, UpstreamStatus]:
        return dict(self._statuses)

    # -- used by catalog.py ------------------------------------------------

    def status(self, name: str) -> UpstreamStatus:
        return self._statuses.setdefault(name, UpstreamStatus())

    async def inventory(self, name: str, *, force: bool = False) -> list[ToolSummary]:
        self.inventory_calls += 1
        if self._inventory_error is not None:
            raise self._inventory_error
        return self._inventories.get(name, [])

    async def probe_all(self, *, timeout: float | None = None) -> dict[str, UpstreamStatus]:
        self.probe_calls += 1
        return dict(self._statuses)


def tool(name: str, description: str = "", parameters: list[str] | None = None) -> ToolSummary:
    return ToolSummary(
        name=name,
        gateway_name=f"alpha_{name}",
        description=description,
        parameters=parameters or [],
    )
