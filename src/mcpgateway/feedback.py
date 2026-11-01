"""Gateway-owned feedback tool and log sink."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel

from .logging_utils import get_logger, log_event
from .registry import Registry

class FeedbackConfirmation(BaseModel):
    """Confirmation returned after feedback has been recorded in the gateway log."""

    status: Literal["success"] = "success"
    message: str
    timestamp: datetime


def register_feedback_tool(gateway: FastMCP, *, registry: Registry) -> None:
    """Register the gateway-owned feedback tool against known upstream servers."""
    logger = get_logger("feedback")

    @gateway.tool(name="send_feedback", tags={"feedback", "gateway"})
    def send_feedback(
        server: str,
        author: str,
        feedback: str,
    ) -> FeedbackConfirmation:
        """Report a problem or suggestion about a server or brick's tools.

        This sends the report to the gateway operators' log, not to the person
        you are speaking with. Use the registered server name whose tools the
        report concerns; no upstream feedback backend is required.

        Args:
            server: Registered server or brick the report is about.
            author: Identifier for the feedback author, such as a user name, role, or session ID.
            feedback: Actionable Markdown describing the issue or suggestion and relevant context.
        """
        try:
            source = registry.by_name(server).name
        except KeyError:
            known_servers = ", ".join(entry.name for entry in registry.servers) or "<none>"
            raise ToolError(f"Unknown feedback server {server!r}; choose a registered server: {known_servers}.")

        recorded_at = datetime.now(UTC)
        log_event(
            logger,
            "feedback_received",
            f"Feedback received for {source}",
            source=source,
            feedback_source=source,
            feedback_author=json.dumps(author, ensure_ascii=False),
            feedback_body=json.dumps(feedback, ensure_ascii=False),
            feedback_chars_count=len(feedback),
        )
        return FeedbackConfirmation(
            message="Feedback recorded successfully.",
            timestamp=recorded_at,
        )
