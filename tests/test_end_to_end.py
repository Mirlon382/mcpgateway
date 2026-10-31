"""End-to-end test: real HTTP gateway, real HTTP upstream, all five specs.

Marked ``integration`` (deselected by default, run with ``-m integration``)
because it binds local ports and starts two processes. Everything else in the
suite is hermetic.

What it proves, against processes that actually talk HTTP:

1. the gateway starts from a YAML registry and proxies a mounted upstream (spec 1);
2. ``GET /health`` reports reachability, latency and metrics (spec 2);
3. the JSON logs carry tool calls, upstream latency and errors (spec 3);
4. a saturated upstream returns a bounded-error instead of queueing forever (spec 4);
5. ``catalog://`` resources expose server summaries and tool inventories (spec 5).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastmcp import Client

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_PORT = 8899
GATEWAY_PORT = 8123

REGISTRY = """
gateway:
  name: E2E Gateway
  tool_search:
    enabled: false
  catalog:
    enabled: true
    include_tool_inventory: true
  health:
    enabled: true
    path: /health
    probe_interval_seconds: 1
    probe_timeout_seconds: 5
    failure_threshold: 2
  metrics:
    log_level: INFO
    json_logs: true
  concurrency:
    global_limit: 4
    acquire_timeout_seconds: 0.2

servers:
  - name: demo
    url: http://127.0.0.1:{upstream_port}/mcp
    transport: http
    domain: demo
    description: Serveur de démonstration
    tags: [test]
    enabled: true
    timeout: 10.0
    max_concurrency: 1
    cache_ttl: 5
  - name: offline
    url: http://127.0.0.1:9/mcp
    transport: http
    domain: demo
    description: Port fermé, pour vérifier la remontée d'indisponibilité
    enabled: true
    timeout: 2.0
    max_concurrency: 1
    cache_ttl: 5
  - name: disabled_server
    url: http://127.0.0.1:9/mcp
    transport: http
    domain: demo
    enabled: false
""".format(upstream_port=UPSTREAM_PORT)


def wait_for_port(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise TimeoutError(f"nothing listening on port {port} after {timeout}s")


def http_json(path: str, timeout: float = 30.0) -> tuple[int, dict]:
    request = urllib.request.Request(f"http://127.0.0.1:{GATEWAY_PORT}{path}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - localhost only
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:  # non-2xx carries a JSON body too
        return exc.code, json.loads(exc.read().decode())


@pytest.fixture(scope="module")
def running_gateway(tmp_path_factory: pytest.TempPathFactory):
    """Start the demo upstream and the gateway as subprocesses."""
    workdir = tmp_path_factory.mktemp("e2e")
    registry_path = workdir / "registry.yaml"
    registry_path.write_text(REGISTRY, encoding="utf-8")
    log_path = workdir / "gateway.log"

    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    upstream = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "tests" / "fixtures" / "demo_upstream.py"), str(UPSTREAM_PORT)],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        wait_for_port(UPSTREAM_PORT)

        log_file = log_path.open("w", encoding="utf-8")
        gateway = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "mcpgateway.server",
                "--registry",
                str(registry_path),
                "--host",
                "127.0.0.1",
                "--port",
                str(GATEWAY_PORT),
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            wait_for_port(GATEWAY_PORT)
            # First /health call triggers the initial probe cycle.
            body: dict = {}
            status = 0
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                status, body = http_json("/health?details=1")
                upstreams = body.get("upstreams", {})
                # Wait until the background prober has both proved the live upstream
                # reachable and crossed the failure threshold for the dead one.
                if upstreams.get("demo", {}).get("state") == "ok" and upstreams.get("offline", {}).get("state") == "down":
                    break
                time.sleep(0.5)
            yield {"log_path": log_path, "body": body, "status": status}
        finally:
            os.killpg(os.getpgid(gateway.pid), signal.SIGTERM)
            gateway.wait(timeout=15)
            log_file.close()
    finally:
        os.killpg(os.getpgid(upstream.pid), signal.SIGTERM)
        upstream.wait(timeout=15)


def test_health_reports_upstream_reachability(running_gateway: dict) -> None:
    body = running_gateway["body"]

    assert body["status"] == "degraded"  # demo is up, the closed port is down
    assert body["servers"]["enabled"] == 2
    assert body["servers"]["disabled"] == 1
    assert body["servers"]["down"] == 1
    assert body["upstreams"]["demo"]["state"] == "ok"
    assert body["upstreams"]["demo"]["latency_ms"] is not None
    assert body["upstreams"]["demo"]["tool_count"] == 3
    assert body["upstreams"]["offline"]["state"] == "down"  # crossed the failure threshold
    assert body["upstreams"]["offline"]["consecutive_failures"] >= 2
    assert body["upstreams"]["offline"]["last_error"]
    assert body["upstreams"]["disabled_server"]["state"] == "disabled"
    assert body["probe"]["failure_threshold"] == 2


def test_health_status_code_follows_the_contract(running_gateway: dict) -> None:
    status, body = http_json("/health")
    # The summary payload stays small and does not list upstream URLs.
    assert "url" not in json.dumps(body)
    # One upstream is down, so a monitor sees 503 and a machine-readable reason.
    assert status == 503
    assert body["status"] == "degraded"
    assert body["unreachable"] == ["offline"]
    assert body["servers"]["ok"] == 1
    assert body["servers"]["down"] == 1


async def test_tools_are_proxied_and_named_with_their_namespace(running_gateway: dict) -> None:
    async with Client(f"http://127.0.0.1:{GATEWAY_PORT}/mcp") as client:
        tools = {tool.name for tool in await client.list_tools()}

    assert tools == {"demo_echo", "demo_add", "demo_slow"}
    assert all(name.startswith("demo_") for name in tools)


async def test_proxied_tool_call_works_end_to_end(running_gateway: dict) -> None:
    async with Client(f"http://127.0.0.1:{GATEWAY_PORT}/mcp") as client:
        result = await client.call_tool("demo_echo", {"message": "bonjour"})
        assert result.data == "echo: bonjour"

        total = await client.call_tool("demo_add", {"a": 2, "b": 3})
        assert total.data == 5


async def test_catalog_resources_expose_summaries_and_inventories(running_gateway: dict) -> None:
    async with Client(f"http://127.0.0.1:{GATEWAY_PORT}/mcp") as client:
        servers = json.loads((await client.read_resource("catalog://servers"))[0].text)  # type: ignore[union-attr]
        domains = json.loads((await client.read_resource("catalog://domains"))[0].text)  # type: ignore[union-attr]
        inventory = json.loads((await client.read_resource("catalog://servers/demo/tools"))[0].text)  # type: ignore[union-attr]
        guide = (await client.read_resource("catalog://guide"))[0].text  # type: ignore[union-attr]

    assert {entry["name"] for entry in servers["servers"]} == {"demo", "offline", "disabled_server"}
    demo = next(entry for entry in servers["servers"] if entry["name"] == "demo")
    assert demo["domain"] == "demo"
    assert demo["tags"] == ["test"]
    assert demo["status"]["state"] == "ok"
    assert domains["domains"][0]["domain"] == "demo"
    assert sorted(inventory["tools"][0]) == ["description", "gateway_name", "name", "parameters"]
    assert {entry["gateway_name"] for entry in inventory["tools"]} == {"demo_echo", "demo_add", "demo_slow"}
    # Regression guard: the parameter names must come from the upstream schema, not
    # from an empty fallback (FastMCP exposes them as Tool.parameters).
    echo = next(entry for entry in inventory["tools"] if entry["name"] == "echo")
    assert echo["parameters"] == ["message"]
    slow = next(entry for entry in inventory["tools"] if entry["name"] == "slow")
    assert slow["parameters"] == ["seconds"]
    assert next(entry for entry in inventory["tools"] if entry["name"] == "add")["parameters"] == ["a", "b"]

    # The guide is served as markdown, with its mechanics intact (this deployment
    # configures no deployment guide, so only the packaged part is present).
    assert guide.startswith("---")
    assert "{serveur}_{outil}" in guide
    assert "catalog://servers" in guide
    assert "Démarches types" not in guide


async def test_a_slow_upstream_cannot_stampede_past_its_concurrency_limit(running_gateway: dict) -> None:
    """'demo' has max_concurrency=1 and acquire_timeout_seconds=0.2.

    Three concurrent calls to a tool that sleeps for a second must not all reach
    the upstream: one runs, the others get a bounded ToolError instead of waiting
    indefinitely (spec 4).
    """
    from fastmcp.exceptions import ToolError

    async with Client(f"http://127.0.0.1:{GATEWAY_PORT}/mcp") as client:

        async def call() -> str:
            try:
                result = await client.call_tool("demo_slow", {"seconds": 1.0}, timeout=15.0)
                return "ok"
            except ToolError as exc:
                assert "busy" in str(exc) or "refused" in str(exc)
                return "rejected"

        outcomes = await asyncio.gather(call(), call(), call())

    assert outcomes.count("ok") >= 1, f"no call reached the upstream: {outcomes}"
    assert outcomes.count("rejected") >= 1, f"the concurrency ceiling never engaged: {outcomes}"


def test_structured_logs_carry_tool_calls_and_upstream_latency(running_gateway: dict) -> None:
    """The gateway log must be machine-readable JSON with the metric fields (spec 3)."""
    # Give the subprocess a moment to flush the lines produced by the calls above.
    time.sleep(0.5)
    events = []
    for line in running_gateway["log_path"].read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:  # pragma: no cover - uvicorn noise
            continue

    by_event = {event.get("event") for event in events}
    assert "gateway_started" in by_event
    assert "registry_loaded" in by_event
    assert "upstream_call" in by_event
    assert "probe_cycle" in by_event

    probes = [event for event in events if event.get("event") == "upstream_call" and event.get("operation") == "probe"]
    assert probes, "no probe events were logged"
    demo_probe = next(event for event in probes if event.get("upstream") == "demo")
    assert demo_probe["status"] == "ok"
    assert demo_probe["duration_ms"] > 0

    failures = [event for event in probes if event.get("status") == "error"]
    assert failures, "the unreachable upstream should have been logged as an error"

    tool_events = [event for event in events if event.get("event") == "tool_call"]
    assert {event["tool"] for event in tool_events} >= {"demo_echo", "demo_add"}
    echo_event = next(event for event in tool_events if event["tool"] == "demo_echo")
    assert echo_event["upstream"] == "demo"
    assert echo_event["status"] == "ok"
    assert echo_event["duration_ms"] > 0

    health_events = [event for event in events if event.get("event") == "health_check"]
    assert health_events and health_events[0]["status"] in {"ok", "degraded", "down"}
