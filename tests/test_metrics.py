"""Metrics and structured logging (spec 3)."""

from __future__ import annotations

import json
import logging

import pytest

from mcpgateway.logging_utils import JsonLogFormatter, TextLogFormatter, configure_logging, get_logger, log_event
from mcpgateway.metrics import MetricsRecorder, Timer


def test_tool_calls_are_counted_and_timed() -> None:
    metrics = MetricsRecorder()
    metrics.record_tool_call(upstream="alpha", tool="alpha_search", seconds=0.10)
    metrics.record_tool_call(upstream="alpha", tool="alpha_search", seconds=0.30)
    metrics.record_tool_call(upstream="alpha", tool="alpha_search", seconds=0.20, error=RuntimeError("boom"))

    series = metrics.snapshot()["tool_calls"]["alpha.alpha_search"]
    assert series["count"] == 3
    assert series["errors"] == 1
    assert series["error_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert series["latency_ms"]["min"] == pytest.approx(100.0)
    assert series["latency_ms"]["max"] == pytest.approx(300.0)
    assert series["latency_ms"]["mean"] == pytest.approx(200.0)
    assert series["latency_ms"]["p50"] is not None
    assert series["latency_ms"]["p95"] is not None


def test_series_are_partitioned_per_upstream_and_tool() -> None:
    metrics = MetricsRecorder()
    metrics.record_tool_call(upstream="alpha", tool="alpha_a", seconds=0.01)
    metrics.record_tool_call(upstream="beta", tool="beta_b", seconds=0.02)
    metrics.record_tool_call(upstream=None, tool="unknown_tool", seconds=0.03)

    assert set(metrics.snapshot()["tool_calls"]) == {"alpha.alpha_a", "beta.beta_b", "unknown.unknown_tool"}
    assert set(metrics.series_for(upstream="alpha")) == {"alpha.alpha_a"}


def test_upstream_calls_record_extra_fields() -> None:
    metrics = MetricsRecorder()
    metrics.record_upstream_call(upstream="alpha", operation="probe", seconds=0.05, tool_count=7)

    series = metrics.snapshot()["upstream_calls"]["alpha.probe"]
    assert series["count"] == 1
    assert series["errors"] == 0
    assert metrics.upstream_series_for("alpha")["probe"]["count"] == 1


def test_messages_and_totals() -> None:
    metrics = MetricsRecorder()
    metrics.record_message("tools/call")
    metrics.record_message("tools/call")
    metrics.record_message("notifications/initialized")
    metrics.record_message(None)
    metrics.record_tool_call(upstream="alpha", tool="alpha_a", seconds=0.01, error=ValueError("nope"))

    totals = metrics.totals()
    assert totals["tool_calls"] == 1
    assert totals["tool_errors"] == 1
    assert totals["messages"] == 4
    assert metrics.snapshot()["messages"]["tools/call"] == 2


def test_latency_sample_window_is_bounded() -> None:
    metrics = MetricsRecorder(sample_window=16)
    for index in range(100):
        metrics.record_tool_call(upstream="alpha", tool="alpha_a", seconds=index / 1000)

    series = metrics.snapshot()["tool_calls"]["alpha.alpha_a"]
    assert series["count"] == 100  # counters are exact
    assert series["sample_size"] == 16  # percentiles use the bounded window


def test_json_formatter_emits_one_object_per_event() -> None:
    configure_logging("INFO", json_logs=True)
    logger = get_logger("test")
    log_event(logger, "tool_call", "tool alpha_search", upstream="alpha", duration_ms=12.5)

    record = logging.LogRecord(
        name="mcpgateway.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="tool alpha_search",
        args=(),
        exc_info=None,
    )
    record.mcpgateway_event = "tool_call"
    record.mcpgateway_fields = {"upstream": "alpha", "duration_ms": 12.5}

    payload = json.loads(JsonLogFormatter().format(record))
    assert payload["event"] == "tool_call"
    assert payload["upstream"] == "alpha"
    assert payload["duration_ms"] == 12.5
    assert payload["level"] == "INFO"


def test_text_formatter_keeps_the_fields() -> None:
    record = logging.LogRecord(
        name="mcpgateway.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="upstream alpha failed",
        args=(),
        exc_info=None,
    )
    record.mcpgateway_event = "upstream_call"
    record.mcpgateway_fields = {"upstream": "alpha", "status": "error"}

    rendered = TextLogFormatter().format(record)
    assert "upstream_call" in rendered
    assert "upstream=alpha" in rendered
    assert "status=error" in rendered


def test_log_event_never_raises() -> None:
    """A broken log call must not fail a request."""

    class Exploding(logging.Logger):
        def log(self, *args, **kwargs):  # type: ignore[override]
            raise RuntimeError("logging backend is down")

    log_event(Exploding("x"), "anything", "message", field=1)


def test_configure_logging_is_idempotent() -> None:
    configure_logging("DEBUG", json_logs=False)
    configure_logging("INFO", json_logs=True)
    logger = get_logger()
    assert len(logger.handlers) == 1
    assert logger.level == logging.INFO


def test_timer_measures_elapsed_time() -> None:
    with Timer() as timer:
        pass
    assert timer.elapsed >= 0.0
