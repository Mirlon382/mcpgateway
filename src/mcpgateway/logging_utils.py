"""Structured logging helpers.

Every operational event the gateway emits (upstream probe, tool call, cache
refresh, saturation) goes through :func:`log_event`, which attaches a stable
``event`` name plus a field mapping to the log record. With ``json_logs``
enabled the record is rendered as a single-line JSON object, so latency and
error fields can be aggregated by any log collector without parsing prose.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

LOGGER_NAME = "mcpgateway"
_EVENT_ATTR = "mcpgateway_event"
_FIELDS_ATTR = "mcpgateway_fields"


class JsonLogFormatter(logging.Formatter):
    """Render a record as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003 - stdlib signature
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, _EVENT_ATTR, None) or "log",
            "message": record.getMessage(),
        }
        fields = getattr(record, _FIELDS_ATTR, None)
        if fields:
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextLogFormatter(logging.Formatter):
    """Human-readable fallback that keeps the same field information."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003 - stdlib signature
        stamp = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S")
        event = getattr(record, _EVENT_ATTR, None) or "-"
        fields = getattr(record, _FIELDS_ATTR, None) or {}
        rendered = " ".join(f"{key}={value}" for key, value in fields.items())
        line = f"{stamp} {record.levelname:<8} {event:<22} {record.getMessage()}"
        if rendered:
            line = f"{line} | {rendered}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def configure_logging(level: str = "INFO", *, json_logs: bool = True) -> None:
    """Install a single stdout handler on the ``mcpgateway`` logger.

    Safe to call twice: the handler is replaced rather than stacked, which keeps
    ``FastMCP``'s own uvicorn/root loggers untouched.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonLogFormatter() if json_logs else TextLogFormatter())
    logger.addHandler(handler)


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child of the gateway logger so all output shares one formatter."""
    return logging.getLogger(LOGGER_NAME if not name else f"{LOGGER_NAME}.{name}")


def log_event(
    logger: logging.Logger,
    event: str,
    message: str = "",
    *,
    level: int = logging.INFO,
    exc_info: bool = False,
    **fields: Any,
) -> None:
    """Emit a structured event.

    Logging must never break a request, so failures here are swallowed: a broken
    log line is not worth failing a tool call over.
    """
    try:
        logger.log(
            level,
            message or event,
            extra={_EVENT_ATTR: event, _FIELDS_ATTR: fields},
            exc_info=exc_info,
        )
    except Exception:  # pragma: no cover - defensive
        pass
