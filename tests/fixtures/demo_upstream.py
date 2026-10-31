"""A tiny MCP server used as the upstream in the end-to-end test."""

from __future__ import annotations

import sys

from fastmcp import FastMCP

mcp = FastMCP("Demo Upstream", instructions="Serveur de démonstration pour les tests.")


@mcp.tool
def echo(message: str) -> str:
    """Echo a message back."""
    return f"echo: {message}"


@mcp.tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@mcp.tool
async def slow(seconds: float = 1.0) -> str:
    """Sleep for the given number of seconds, to exercise the concurrency ceiling."""
    import asyncio

    await asyncio.sleep(seconds)
    return f"slept {seconds}s"


@mcp.resource("data://demo/config")
def demo_config() -> str:
    """Static demo configuration."""
    return '{"env": "test"}'


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    mcp.run(transport="http", host="127.0.0.1", port=port)
