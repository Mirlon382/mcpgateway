"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

from mcpgateway.metrics import MetricsRecorder
from mcpgateway.registry import Registry
from mcpgateway.upstreams import UpstreamPool

from support import build_registry


@pytest.fixture
def make_registry(tmp_path: Path) -> Callable[..., Registry]:
    def _make(
        *,
        servers: list[dict[str, Any]] | None = None,
        gateway: dict[str, Any] | None = None,
        name: str = "registry.yaml",
    ) -> Registry:
        return build_registry(tmp_path, servers=servers, gateway=gateway, name=name)

    return _make


@pytest.fixture
def registry(make_registry: Callable[..., Registry]) -> Registry:
    return make_registry()


@pytest.fixture
def metrics() -> MetricsRecorder:
    return MetricsRecorder()


@pytest.fixture
def pool(registry: Registry, metrics: MetricsRecorder) -> UpstreamPool:
    """A real pool: it only touches the network when a probe runs."""
    return UpstreamPool(registry, metrics)
