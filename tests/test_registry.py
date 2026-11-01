"""Registry loading and validation (spec 1)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from mcpgateway.registry import (
    REGISTRY_ENV_VAR,
    RegistryError,
    load_registry,
    resolve_registry_path,
)

MINIMAL = """
gateway:
  name: Test Gateway
servers:
  - name: alpha
    url: https://alpha.example.org/mcp
    domain: " Insee "
"""


def write(tmp_path: Path, body: str, name: str = "registry.yaml") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_minimal_registry_applies_defaults(tmp_path: Path) -> None:
    loaded = load_registry(write(tmp_path, MINIMAL))
    entry = loaded.registry.servers[0]

    assert entry.name == "alpha"
    assert entry.domain == "Insee"  # trimmed, while preserving the configured label
    assert entry.transport == "http"
    assert entry.enabled is True
    assert entry.tags == frozenset()
    # Operational defaults (spec 4).
    assert entry.timeout == 30.0
    assert entry.max_concurrency == 4
    assert entry.cache_ttl == 300.0
    assert loaded.registry.gateway.concurrency.global_limit == 16


def test_tags_accept_a_bare_string_or_a_list(tmp_path: Path) -> None:
    """The old config.py mixed both shapes; both must keep working."""
    loaded = load_registry(
        write(
            tmp_path,
            """
            servers:
              - name: alpha
                url: https://alpha.example.org/mcp
                domain: stats
                tags: carte
              - name: beta
                url: https://beta.example.org/mcp
                domain: stats
                tags: [dataset, publications]
            """,
        )
    )
    assert loaded.registry.by_name("alpha").tags == frozenset({"carte"})
    assert loaded.registry.by_name("beta").tags == frozenset({"dataset", "publications"})


def test_duplicate_names_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(RegistryError) as excinfo:
        load_registry(
            write(
                tmp_path,
                """
                servers:
                  - {name: alpha, url: https://a.example.org/mcp, domain: stats}
                  - {name: alpha, url: https://b.example.org/mcp, domain: stats}
                """,
            )
        )
    assert "duplicate server name" in str(excinfo.value)


@pytest.mark.parametrize(
    ("url", "fragment"),
    [
        ("ftp://alpha.example.org/mcp", "scheme must be one of"),
        ("alpha.example.org/mcp", "scheme must be one of"),
        ("https:///mcp", "missing host"),
    ],
)
def test_invalid_urls_are_rejected(tmp_path: Path, url: str, fragment: str) -> None:
    with pytest.raises(RegistryError) as excinfo:
        load_registry(write(tmp_path, f"servers:\n  - {{name: alpha, url: '{url}', domain: stats}}\n"))
    assert fragment in str(excinfo.value)


@pytest.mark.parametrize("name", ["1alpha", "with-dash", "", "a" * 70, "alpha beta"])
def test_invalid_names_are_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(RegistryError) as excinfo:
        load_registry(write(tmp_path, f"servers:\n  - {{name: '{name}', url: https://a.example.org/mcp, domain: stats}}\n"))
    assert "invalid server name" in str(excinfo.value)


@pytest.mark.parametrize(("written", "expected"), [("Alpha", "alpha"), ("GeoData", "geodata"), (" alpha ", "alpha")])
def test_names_are_normalised_to_lowercase(tmp_path: Path, written: str, expected: str) -> None:
    """Names are namespace prefixes, so the registry normalises rather than rejecting case."""
    loaded = load_registry(
        write(tmp_path, f"servers:\n  - {{name: '{written}', url: https://a.example.org/mcp, domain: stats}}\n")
    )
    assert loaded.registry.servers[0].name == expected


def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    """extra='forbid' turns a typo into a startup failure instead of a silently ignored field."""
    with pytest.raises(RegistryError) as excinfo:
        load_registry(
            write(
                tmp_path,
                """
                servers:
                  - name: alpha
                    url: https://alpha.example.org/mcp
                    domain: stats
                    transports: http
                """,
            )
        )
    assert "transports" in str(excinfo.value)


@pytest.mark.parametrize("field", ["timeout: -1", "max_concurrency: 0", "cache_ttl: -5"])
def test_out_of_range_limits_are_rejected(tmp_path: Path, field: str) -> None:
    with pytest.raises(RegistryError):
        load_registry(
            write(
                tmp_path,
                f"servers:\n  - {{name: alpha, url: https://a.example.org/mcp, domain: stats, {field}}}\n",
            )
        )


def test_enabled_flag_controls_exposure(tmp_path: Path) -> None:
    loaded = load_registry(
        write(
            tmp_path,
            """
            servers:
              - {name: alpha, url: https://a.example.org/mcp, domain: stats, enabled: true}
              - {name: beta, url: https://b.example.org/mcp, domain: stats, enabled: false}
            """,
        )
    )
    registry = loaded.registry
    assert [entry.name for entry in registry.enabled_servers()] == ["alpha"]
    assert [entry.name for entry in registry.disabled_servers()] == ["beta"]
    # A disabled server is still known, but owns no tool and no resource.
    assert registry.by_name("beta").enabled is False
    assert registry.upstream_of_tool("beta_tool") is None
    assert registry.upstream_of_tool("alpha_tool") is not None


def test_domains_group_servers(tmp_path: Path) -> None:
    loaded = load_registry(
        write(
            tmp_path,
            """
            servers:
              - {name: alpha, url: https://a.example.org/mcp, domain: geographie}
              - {name: beta, url: https://b.example.org/mcp, domain: geographie}
              - {name: gamma, url: https://c.example.org/mcp, domain: diffusion}
            """,
        )
    )
    assert {domain: [entry.name for entry in entries] for domain, entries in loaded.registry.domains().items()} == {
        "diffusion": ["gamma"],
        "geographie": ["alpha", "beta"],
    }


def test_tool_and_resource_attribution(tmp_path: Path) -> None:
    loaded = load_registry(write(tmp_path, MINIMAL))
    registry = loaded.registry

    assert registry.upstream_of_tool("alpha_search") is not None
    assert registry.upstream_of_tool("alphabet_search") is None
    assert registry.upstream_of_tool("search") is None
    assert registry.upstream_of_resource("data://alpha/config") is not None
    assert registry.upstream_of_resource("data://beta/config") is None
    # Gateway-owned resources are not attributed to an upstream.
    assert registry.upstream_of_resource("catalog://servers") is None


def test_full_document_is_accepted(tmp_path: Path) -> None:
    loaded = load_registry(
        write(
            tmp_path,
            """
            gateway:
              name: SSP Gateway
              instructions: "Une passerelle."
              tool_search: {enabled: false, max_results: 10}
              catalog: {enabled: false, include_tool_inventory: false}
              health: {enabled: false, path: /healthz, probe_interval_seconds: 0}
              metrics: {log_level: DEBUG, json_logs: false}
              concurrency: {global_limit: 4, acquire_timeout_seconds: 1.5}
            servers:
              - name: alpha
                url: https://alpha.example.org/mcp
                transport: sse
                domain: stats
                description: Un serveur
                tags: [x]
                enabled: true
                timeout: 12.5
                max_concurrency: 2
                cache_ttl: 60
            """,
        )
    )
    gateway = loaded.registry.gateway
    assert gateway.tool_search.enabled is False
    assert gateway.catalog.enabled is False
    assert gateway.health.path == "/healthz"
    assert gateway.concurrency.acquire_timeout_seconds == 1.5
    assert gateway.metrics.json_logs is False


def test_health_path_must_be_absolute(tmp_path: Path) -> None:
    with pytest.raises(RegistryError) as excinfo:
        load_registry(write(tmp_path, "gateway:\n  health: {path: health}\nservers: []\n"))
    assert "must start with '/'" in str(excinfo.value)


def test_missing_file_raises_registry_error(tmp_path: Path) -> None:
    with pytest.raises(RegistryError) as excinfo:
        load_registry(tmp_path / "nope.yaml")
    assert "no registry file found" in str(excinfo.value)


def test_invalid_yaml_raises_registry_error(tmp_path: Path) -> None:
    with pytest.raises(RegistryError) as excinfo:
        load_registry(write(tmp_path, "servers: [ {name: alpha,\n"))
    assert "invalid YAML" in str(excinfo.value)


def test_document_without_servers_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RegistryError) as excinfo:
        load_registry(write(tmp_path, "unrelated: true\n"))
    assert "expected a 'servers' list" in str(excinfo.value)


def test_environment_variable_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write(tmp_path, MINIMAL, name="from-env.yaml")
    monkeypatch.setenv(REGISTRY_ENV_VAR, str(path))
    # search_cwd=False keeps the test independent of whatever registry.yaml
    # happens to exist in the working directory.
    assert resolve_registry_path(search_cwd=False) == path.resolve()

    monkeypatch.setenv(REGISTRY_ENV_VAR, str(tmp_path / "missing.yaml"))
    with pytest.raises(RegistryError):
        resolve_registry_path(search_cwd=False)


def test_explicit_path_beats_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = write(tmp_path, MINIMAL, name="explicit.yaml")
    other = write(tmp_path, MINIMAL, name="other.yaml")
    monkeypatch.setenv(REGISTRY_ENV_VAR, str(other))

    assert resolve_registry_path(explicit) == explicit.resolve()
