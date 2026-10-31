"""Health payload and status-code contract (spec 2)."""

from __future__ import annotations

from typing import Any, Callable

from mcpgateway.health import build_health_payload
from mcpgateway.metrics import MetricsRecorder
from mcpgateway.registry import Registry

from support import StubPool, tool


def payload(
    registry: Registry,
    *,
    states: dict[str, str],
    metrics: MetricsRecorder | None = None,
    details: bool = False,
) -> tuple[dict[str, Any], int]:
    pool = StubPool(registry, states=states, inventories={"alpha": [tool("search")]})
    return build_health_payload(registry, pool, metrics or MetricsRecorder(), details=details)


def test_all_upstreams_ok(make_registry: Callable[..., Registry]) -> None:
    registry = make_registry()
    body, status = payload(registry, states={"alpha": "ok"})

    assert status == 200
    assert body["status"] == "ok"
    assert body["servers"]["enabled"] == 1
    assert body["servers"]["disabled"] == 1
    assert body["servers"]["ok"] == 1
    assert body["unreachable"] == []


def test_single_failure_is_degraded_but_still_200(make_registry: Callable[..., Registry]) -> None:
    """A transient blip must not make an orchestrator restart the gateway."""
    registry = make_registry()
    body, status = payload(registry, states={"alpha": "degraded"})

    assert status == 200
    assert body["status"] == "degraded"
    assert body["servers"]["degraded"] == 1


def test_persistent_failure_is_down_and_503(make_registry: Callable[..., Registry]) -> None:
    registry = make_registry()
    body, status = payload(registry, states={"alpha": "down"})

    assert status == 503
    assert body["status"] == "down"
    assert body["unreachable"] == ["alpha"]


def test_partial_outage_is_degraded_and_503(make_registry: Callable[..., Registry]) -> None:
    registry = make_registry(
        servers=[
            {"name": "alpha", "url": "https://alpha.example.org/mcp", "domain": "stats"},
            {"name": "beta", "url": "https://beta.example.org/mcp", "domain": "stats"},
        ]
    )
    body, status = payload(registry, states={"alpha": "ok", "beta": "down"})

    assert status == 503
    assert body["status"] == "degraded"
    assert body["unreachable"] == ["beta"]


def test_unprobed_upstreams_report_unknown_without_failing(make_registry: Callable[..., Registry]) -> None:
    registry = make_registry()
    body, status = payload(registry, states={"alpha": "unknown"})

    assert status == 200
    assert body["status"] == "degraded"  # not yet known, but the process is alive


def test_no_enabled_servers_is_not_an_error(make_registry: Callable[..., Registry]) -> None:
    registry = make_registry(
        servers=[{"name": "alpha", "url": "https://alpha.example.org/mcp", "domain": "stats", "enabled": False}]
    )
    body, status = payload(registry, states={})

    assert status == 200
    assert body["status"] == "empty"
    assert body["servers"]["enabled"] == 0


def test_details_expose_upstreams_probe_config_and_metrics(make_registry: Callable[..., Registry]) -> None:
    registry = make_registry()
    metrics = MetricsRecorder()
    metrics.record_tool_call(upstream="alpha", tool="alpha_search", seconds=0.2)

    body, status = payload(registry, states={"alpha": "ok"}, metrics=metrics, details=True)

    assert status == 200
    assert body["upstreams"]["alpha"]["state"] == "ok"
    assert body["upstreams"]["alpha"]["latency_ms"] == 42.0
    assert body["upstreams"]["beta"]["state"] == "disabled"  # disabled servers stay visible
    assert body["probe"]["failure_threshold"] == 2
    assert body["metrics"]["tool_calls"]["alpha.alpha_search"]["count"] == 1
    # The summary payload must not leak URLs; the detailed one may.
    assert "url" in body["upstreams"]["alpha"]


def test_summary_payload_omits_urls(make_registry: Callable[..., Registry]) -> None:
    registry = make_registry()
    body, _ = payload(registry, states={"alpha": "ok"}, details=False)

    assert "upstreams" not in body
    assert "metrics" not in body
