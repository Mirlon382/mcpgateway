"""Gateway-owned feedback tool behavior."""

from __future__ import annotations

import json
import logging

from fastmcp import Client

from mcpgateway.logging_utils import JsonLogFormatter
from mcpgateway.server import build_gateway


async def test_gateway_feedback_tool_is_listed_logs_source_and_confirms(
    make_registry,
    caplog,
) -> None:
    registry = make_registry(
        servers=[
            {
                "name": "insee",
                "url": "http://127.0.0.1:8001/mcp",
                "domain": "Insee",
                "enabled": False,
            }
        ],
    )
    gateway, _pool, _metrics = build_gateway(registry)
    caplog.set_level(logging.INFO, logger="mcpgateway")

    async with Client(gateway) as client:
        tools = await client.list_tools()
        search_result = await client.call_tool(
            "search_tools",
            {"query": "send feedback report to gateway operators"},
        )
        result = await client.call_tool(
            "call_tool",
            {
                "name": "send_feedback",
                "arguments": {
                    "server": "insee",
                    "author": "ada",
                    "feedback": "Tool returned no results.\nPlease investigate.",
                },
            },
        )

    assert {tool.name for tool in tools} == {"search_tools", "call_tool"}
    assert "send_feedback" in search_result.content[0].text
    assert result.is_error is False
    assert "Feedback recorded successfully." in result.content[0].text

    feedback_records = [
        record
        for record in caplog.records
        if getattr(record, "mcpgateway_event", None) == "feedback_received"
    ]
    assert len(feedback_records) == 1
    log_line = JsonLogFormatter().format(feedback_records[0])
    event = json.loads(log_line)
    assert event["source"] == "insee"
    assert json.loads(event["feedback_author"]) == "ada"
    assert json.loads(event["feedback_body"]) == "Tool returned no results.\nPlease investigate."
    assert len(log_line.splitlines()) == 1
