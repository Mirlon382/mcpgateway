"""Metrics collection for the gateway (spec 3).

Three families are tracked:

* **tool calls** — count, errors and latency per ``(upstream, tool)``, recorded by
  the metrics middleware around every ``tools/call`` the gateway serves;
* **upstream calls** — count, errors and latency per ``(upstream, operation)`` for
  the gateway's own outbound calls (reachability probes, inventory refreshes);
* **messages** — a cheap counter per MCP method, including notifications and
  requests that fail before reaching a handler (``on_message`` sees those in
  FastMCP 4).

Latency is kept as count/sum/min/max plus a bounded sample window, which is
enough for p50/p95 without unbounded memory growth. Every record is also emitted
as a structured log event, so the data is useful whether or not anyone scrapes
the in-process snapshot.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .logging_utils import get_logger, log_event

_UNKNOWN = "unknown"


def _percentile(sorted_values: list[float], fraction: float) -> float | None:
    if not sorted_values:
        return None
    index = max(0, min(len(sorted_values) - 1, math.ceil(fraction * len(sorted_values)) - 1))
    return sorted_values[index]


@dataclass
class LatencyStats:
    """Count/sum/min/max plus a bounded sample window for percentiles."""

    count: int = 0
    total_seconds: float = 0.0
    min_seconds: float | None = None
    max_seconds: float | None = None
    errors: int = 0
    samples: deque[float] = field(default_factory=lambda: deque(maxlen=512))

    def observe(self, seconds: float, *, error: bool = False) -> None:
        self.count += 1
        self.total_seconds += seconds
        self.min_seconds = seconds if self.min_seconds is None else min(self.min_seconds, seconds)
        self.max_seconds = seconds if self.max_seconds is None else max(self.max_seconds, seconds)
        if error:
            self.errors += 1
        self.samples.append(seconds)

    def snapshot(self) -> dict[str, Any]:
        ordered = sorted(self.samples)
        mean = (self.total_seconds / self.count) if self.count else None
        return {
            "count": self.count,
            "errors": self.errors,
            "error_rate": round(self.errors / self.count, 4) if self.count else 0.0,
            "latency_ms": {
                "mean": round(mean * 1000, 2) if mean is not None else None,
                "min": round(self.min_seconds * 1000, 2) if self.min_seconds is not None else None,
                "max": round(self.max_seconds * 1000, 2) if self.max_seconds is not None else None,
                "p50": _round_ms(_percentile(ordered, 0.50)),
                "p95": _round_ms(_percentile(ordered, 0.95)),
            },
            "sample_size": len(ordered),
        }


def _round_ms(value: float | None) -> float | None:
    return round(value * 1000, 2) if value is not None else None


def _error_label(error: BaseException | str | None) -> str | None:
    if error is None:
        return None
    if isinstance(error, BaseException):
        return f"{type(error).__name__}: {error}"[:500]
    return str(error)[:500]


class MetricsRecorder:
    """In-process metrics registry with structured log emission."""

    def __init__(self, *, logger: logging.Logger | None = None, sample_window: int = 512) -> None:
        self._logger = logger or get_logger("metrics")
        self._sample_window = sample_window
        self._tool_calls: dict[tuple[str, str], LatencyStats] = {}
        self._upstream_calls: dict[tuple[str, str], LatencyStats] = {}
        self._messages: dict[str, int] = {}
        self._started_at = datetime.now(tz=timezone.utc)

    # -- recording ---------------------------------------------------------

    def record_message(self, method: str | None) -> None:
        key = method or _UNKNOWN
        self._messages[key] = self._messages.get(key, 0) + 1

    def record_tool_call(
        self,
        *,
        upstream: str | None,
        tool: str,
        seconds: float,
        error: BaseException | str | None = None,
    ) -> None:
        """Record one ``tools/call`` served by the gateway."""
        stats = self._series(self._tool_calls, (upstream or _UNKNOWN, tool))
        stats.observe(seconds, error=error is not None)
        log_event(
            self._logger,
            "tool_call",
            f"tool {tool}",
            level=logging.ERROR if error is not None else logging.INFO,
            upstream=upstream or _UNKNOWN,
            tool=tool,
            duration_ms=round(seconds * 1000, 2),
            status="error" if error is not None else "ok",
            error=_error_label(error),
        )

    def record_upstream_call(
        self,
        *,
        upstream: str,
        operation: str,
        seconds: float,
        error: BaseException | str | None = None,
        **extra: Any,
    ) -> None:
        """Record one outbound call made by the gateway itself."""
        stats = self._series(self._upstream_calls, (upstream, operation))
        stats.observe(seconds, error=error is not None)
        log_event(
            self._logger,
            "upstream_call",
            f"upstream {upstream} {operation}",
            level=logging.ERROR if error is not None else logging.INFO,
            upstream=upstream,
            operation=operation,
            duration_ms=round(seconds * 1000, 2),
            status="error" if error is not None else "ok",
            error=_error_label(error),
            **extra,
        )

    def _series(self, table: dict[tuple[str, str], LatencyStats], key: tuple[str, str]) -> LatencyStats:
        stats = table.get(key)
        if stats is None:
            stats = LatencyStats(samples=deque(maxlen=self._sample_window))
            table[key] = stats
        return stats

    # -- reading -----------------------------------------------------------

    @property
    def uptime_seconds(self) -> float:
        return (datetime.now(tz=timezone.utc) - self._started_at).total_seconds()

    def series_for(self, *, upstream: str | None = None) -> dict[str, Any]:
        """Snapshot of the tool-call series, optionally filtered to one upstream."""
        return {
            f"{key_upstream}.{tool}": stats.snapshot()
            for (key_upstream, tool), stats in sorted(self._tool_calls.items())
            if upstream is None or key_upstream == upstream
        }

    def upstream_series_for(self, upstream: str) -> dict[str, Any]:
        return {
            operation: stats.snapshot()
            for (series_upstream, operation), stats in sorted(self._upstream_calls.items())
            if series_upstream == upstream
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "uptime_seconds": round(self.uptime_seconds, 3),
            "started_at": self._started_at.isoformat(timespec="seconds"),
            "tool_calls": {
                f"{upstream}.{tool}": stats.snapshot()
                for (upstream, tool), stats in sorted(self._tool_calls.items())
            },
            "upstream_calls": {
                f"{upstream}.{operation}": stats.snapshot()
                for (upstream, operation), stats in sorted(self._upstream_calls.items())
            },
            "messages": dict(sorted(self._messages.items())),
        }

    def totals(self) -> dict[str, int]:
        tool_stats = list(self._tool_calls.values())
        upstream_stats = list(self._upstream_calls.values())
        return {
            "tool_calls": sum(stats.count for stats in tool_stats),
            "tool_errors": sum(stats.errors for stats in tool_stats),
            "upstream_calls": sum(stats.count for stats in upstream_stats),
            "upstream_errors": sum(stats.errors for stats in upstream_stats),
            "messages": sum(self._messages.values()),
        }


class Timer:
    """Small monotonic timer used around awaited operations."""

    def __init__(self) -> None:
        self._start = time.perf_counter()

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self._start

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None
