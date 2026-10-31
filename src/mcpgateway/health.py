"""Health endpoint (spec 2).

A single unauthenticated ``GET`` route that answers two questions:

1. *is the gateway process up?* — it answered at all;
2. *are its upstreams reachable?* — per-upstream state, latency and last error,
   maintained by the background prober and refreshable on demand.

Custom routes in FastMCP are deliberately never protected by the server's auth
middleware (they exist for probes), so the default payload stays small and omits
upstream URLs: a health endpoint should not be a directory listing. ``?details=1``
returns the full per-upstream picture plus the metrics snapshot.

Status code contract:

* ``200`` — every enabled upstream is ``ok``, or none has been probed yet;
* ``503`` — at least one enabled upstream is ``down`` (see
  ``health.failure_threshold``); a single blip reports ``degraded`` and still
  answers ``200`` so that a transient upstream hiccup cannot restart the gateway.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .logging_utils import get_logger, log_event
from .metrics import MetricsRecorder
from .registry import Registry
from .upstreams import STATE_DOWN, STATE_OK, PoolLike

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _flag(request: Request, name: str) -> bool:
    return str(request.query_params.get(name, "")).strip().lower() in _TRUE_VALUES


def build_health_payload(
    registry: Registry,
    pool: PoolLike,
    metrics: MetricsRecorder,
    *,
    details: bool = False,
) -> tuple[dict[str, Any], int]:
    """Build the health payload and the HTTP status code that goes with it."""
    statuses = pool.statuses()
    enabled = registry.enabled_servers()
    states = {name: status.state for name, status in statuses.items()}

    down = [name for name, state in states.items() if state == STATE_DOWN]
    degraded = [name for name, state in states.items() if state not in {STATE_OK, STATE_DOWN}]
    ok = [name for name, state in states.items() if state == STATE_OK]

    if not enabled:
        status = "empty"
    elif down:
        status = "down" if len(down) == len(enabled) else "degraded"
    elif degraded:
        status = "degraded"
    else:
        status = "ok"

    http_status = 503 if down else 200

    payload: dict[str, Any] = {
        "status": status,
        "gateway": registry.gateway.name,
        "checked_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "uptime_seconds": round(metrics.uptime_seconds, 1),
        "servers": {
            "registered": len(registry.servers),
            "enabled": len(enabled),
            "disabled": len(registry.servers) - len(enabled),
            "ok": len(ok),
            "degraded": len(degraded),
            "down": len(down),
        },
    }

    if details:
        upstreams: dict[str, Any] = {}
        for entry in registry.servers:
            status_payload: dict[str, Any] = (
                statuses[entry.name].as_dict() if entry.name in statuses else {"state": "disabled"}
            )
            upstreams[entry.name] = {
                "url": entry.url,
                "domain": entry.domain,
                "enabled": entry.enabled,
                **status_payload,
            }
        payload["upstreams"] = upstreams
        payload["metrics"] = metrics.snapshot()
        payload["probe"] = {
            "interval_seconds": registry.gateway.health.probe_interval_seconds,
            "timeout_seconds": registry.gateway.health.probe_timeout_seconds,
            "failure_threshold": registry.gateway.health.failure_threshold,
        }
    else:
        payload["unreachable"] = sorted(down)

    return payload, http_status


def register_health_route(
    gateway: FastMCP,
    *,
    registry: Registry,
    pool: PoolLike,
    metrics: MetricsRecorder,
) -> str | None:
    """Register the health route; returns the path when registered."""
    settings = registry.gateway.health
    if not settings.enabled:
        return None

    logger = get_logger("health")

    @gateway.custom_route(settings.path, methods=["GET"], include_in_schema=False)
    async def health(request: Request) -> Response:
        details = _flag(request, "details")
        refresh = _flag(request, "refresh")

        # First call (or an explicit refresh) probes now instead of waiting for the
        # next scheduled cycle: a freshly started gateway should not report
        # "unknown" for a whole probe interval.
        unprobed = any(status.state == "unknown" for status in pool.statuses().values())
        if refresh or unprobed:
            await pool.probe_all()

        payload, http_status = build_health_payload(registry, pool, metrics, details=details)
        log_event(
            logger,
            "health_check",
            f"health check: {payload['status']}",
            level=30 if http_status != 200 else 20,  # WARNING when not 200
            status=payload["status"],
            http_status=http_status,
            details=details,
            servers=payload["servers"],
        )
        return JSONResponse(payload, status_code=http_status)

    return settings.path
