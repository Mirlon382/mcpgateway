"""Declarative registry of upstream MCP servers.

The gateway is driven by a YAML file (``registry.yaml`` by default) instead of a
Python module, so that onboarding a new upstream server is a data change rather
than a code change. Everything in the file is validated at load time: a typo in a
URL, an invalid namespace, or a duplicated server name fails the startup with a
readable message instead of surfacing later as a confusing proxy error.

Server ``name`` doubles as the namespace prefix applied to every component the
upstream contributes (``mount(proxy, namespace=name)``), which is why it is
restricted to a lowercase identifier usable as a tool-name prefix.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

#: Environment variable pointing at an alternative registry file.
REGISTRY_ENV_VAR = "MCPGATEWAY_REGISTRY"

#: Files looked up (in order) when no explicit path is given.
DEFAULT_REGISTRY_FILENAMES: tuple[str, ...] = (
    "registry.yaml",
    "registry.yml",
    "mcpgateway.yaml",
)

#: A server name is used verbatim as a namespace prefix, so it must be a plain
#: lowercase identifier: it ends up inside tool names clients have to call.
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

ALLOWED_SCHEMES = ("http", "https")


class RegistryError(RuntimeError):
    """Raised when the registry file is missing, unreadable, or invalid."""


def _normalise_tags(value: Any) -> frozenset[str]:
    """Accept a list, a set, or a single string; return a frozenset.

    The previous ``config.py`` mixed both shapes (``"tags": "carte"`` next to
    ``{"dataset", "publications"}``); normalising here keeps that history from
    turning into a runtime surprise.
    """
    if value is None:
        return frozenset()
    if isinstance(value, str):
        value = [value]
    if isinstance(value, (set, frozenset, list, tuple)):
        return frozenset(str(item).strip() for item in value if str(item).strip())
    raise ValueError("tags must be a string or a list of strings")


class ServerEntry(BaseModel):
    """One upstream MCP server, validated."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(description="Unique namespace prefix for this upstream's components")
    url: str = Field(description="MCP endpoint URL (Streamable HTTP or legacy SSE)")
    transport: Literal["http", "sse"] = "http"
    domain: str = Field(description="Functional domain used to group servers (e.g. geographie)")
    description: str = ""
    tags: frozenset[str] = Field(default_factory=frozenset)
    enabled: bool = True

    # --- Operational limits (spec 4: timeouts and concurrency) ---
    timeout: float = Field(default=30.0, gt=0, le=600, description="Per-call upstream timeout (s)")
    max_concurrency: int = Field(default=4, ge=1, le=128, description="Concurrent calls allowed to this upstream")
    cache_ttl: float = Field(default=300.0, ge=0, le=86400, description="Component-list cache TTL (s); 0 disables")

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        name = value.strip().lower()
        if not NAME_PATTERN.match(name):
            raise ValueError(
                f"invalid server name {value!r}: must match {NAME_PATTERN.pattern} "
                "(lowercase letter first, then lowercase letters, digits or underscores) — "
                "the name is used as the namespace prefix of every tool"
            )
        return name

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        url = value.strip()
        parsed = urlparse(url)
        if parsed.scheme not in ALLOWED_SCHEMES:
            raise ValueError(f"invalid url {value!r}: scheme must be one of {ALLOWED_SCHEMES}")
        if not parsed.netloc:
            raise ValueError(f"invalid url {value!r}: missing host")
        return url

    @field_validator("domain")
    @classmethod
    def _check_domain(cls, value: str) -> str:
        domain = value.strip().lower()
        if not domain:
            raise ValueError("domain must not be empty")
        return domain

    @field_validator("tags", mode="before")
    @classmethod
    def _check_tags(cls, value: Any) -> frozenset[str]:
        return _normalise_tags(value)

    @property
    def netloc(self) -> str:
        return urlparse(self.url).netloc

    @property
    def tool_prefix(self) -> str:
        """Prefix FastMCP applies to this upstream's tools once mounted."""
        return f"{self.name}_"

    def owns_tool(self, tool_name: str) -> bool:
        return tool_name.startswith(self.tool_prefix)

    def owns_resource(self, uri: str) -> bool:
        """Mounted resources are served as ``scheme://{namespace}/path``."""
        parsed = urlparse(uri)
        return parsed.netloc == self.name or parsed.netloc.startswith(f"{self.name}/")


class ToolSearchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_results: int = Field(default=5, ge=1, le=50)


class CatalogSettings(BaseModel):
    """Catalog MCP resources (spec 5). Both switches are independent so that a
    deployment can expose server summaries without paying for tool inventories."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    include_tool_inventory: bool = True
    inventory_stale_after_seconds: float = Field(default=300.0, ge=0, le=86400)
    include_guide: bool = True
    guide_path: str | None = Field(
        default=None,
        description=(
            "Optional deployment guide, served after the packaged guide in catalog://guide. "
            "Relative paths are resolved against the registry file's directory."
        ),
    )


class HealthSettings(BaseModel):
    """Health endpoint and upstream reachability tracking (spec 2)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    path: str = "/health"
    probe_interval_seconds: float = Field(default=60.0, ge=0, le=3600)
    probe_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    failure_threshold: int = Field(default=2, ge=1, le=10, description="Failures before an upstream is reported down")

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        path = value.strip()
        if not path.startswith("/"):
            raise ValueError(f"health path must start with '/', got {value!r}")
        return path


class MetricsSettings(BaseModel):
    """Structured logging of metrics (spec 3)."""

    model_config = ConfigDict(extra="forbid")

    log_level: str = "INFO"
    json_logs: bool = True
    sample_window: int = Field(default=512, ge=16, le=10000, description="Latency samples kept per series")


class ConcurrencySettings(BaseModel):
    """Gateway-wide concurrency ceiling (spec 4)."""

    model_config = ConfigDict(extra="forbid")

    global_limit: int = Field(default=16, ge=1, le=1024)
    acquire_timeout_seconds: float = Field(
        default=5.0, gt=0, le=300, description="How long a call waits for a slot before failing fast"
    )


class GatewaySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "SSP Gateway"
    instructions: str = ""
    tool_search: ToolSearchSettings = Field(default_factory=ToolSearchSettings)
    catalog: CatalogSettings = Field(default_factory=CatalogSettings)
    health: HealthSettings = Field(default_factory=HealthSettings)
    metrics: MetricsSettings = Field(default_factory=MetricsSettings)
    concurrency: ConcurrencySettings = Field(default_factory=ConcurrencySettings)


class Registry(BaseModel):
    """The whole registry file: gateway settings plus upstream servers."""

    model_config = ConfigDict(extra="forbid")

    gateway: GatewaySettings = Field(default_factory=GatewaySettings)
    servers: list[ServerEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_unique_names(self) -> "Registry":
        seen: dict[str, int] = {}
        duplicates: list[str] = []
        for entry in self.servers:
            seen[entry.name] = seen.get(entry.name, 0) + 1
        duplicates = [name for name, count in seen.items() if count > 1]
        if duplicates:
            raise ValueError(
                "duplicate server name(s) in registry: " + ", ".join(sorted(duplicates))
                + " — names are namespace prefixes and must be unique"
            )
        return self

    # -- accessors ---------------------------------------------------------

    def enabled_servers(self) -> list[ServerEntry]:
        return [entry for entry in self.servers if entry.enabled]

    def disabled_servers(self) -> list[ServerEntry]:
        return [entry for entry in self.servers if not entry.enabled]

    def by_name(self, name: str) -> ServerEntry:
        for entry in self.servers:
            if entry.name == name:
                return entry
        raise KeyError(name)

    def domains(self) -> dict[str, list[ServerEntry]]:
        grouped: dict[str, list[ServerEntry]] = {}
        for entry in self.servers:
            grouped.setdefault(entry.domain, []).append(entry)
        return dict(sorted(grouped.items()))

    def upstream_of_tool(self, tool_name: str) -> ServerEntry | None:
        """Attribute a gateway tool name back to its upstream (``{name}_{tool}``)."""
        for entry in self.enabled_servers():
            if entry.owns_tool(tool_name):
                return entry
        return None

    def upstream_of_resource(self, uri: str) -> ServerEntry | None:
        for entry in self.enabled_servers():
            if entry.owns_resource(uri):
                return entry
        return None


@dataclass(frozen=True)
class LoadedRegistry:
    registry: Registry
    path: Path


def resolve_registry_path(path: str | Path | None = None, *, search_cwd: bool = True) -> Path:
    """Locate the registry file: explicit path, then ``$MCPGATEWAY_REGISTRY``, then CWD."""
    candidates: list[Path | None] = []
    if path is not None:
        candidates.append(Path(path).expanduser())
    else:
        env_value = os.environ.get(REGISTRY_ENV_VAR)
        if env_value:
            candidates.append(Path(env_value).expanduser())
        if search_cwd:
            candidates.extend(Path.cwd() / name for name in DEFAULT_REGISTRY_FILENAMES)

    checked: list[str] = []
    for candidate in candidates:
        if candidate is None:
            continue
        checked.append(str(candidate))
        if candidate.is_file():
            return candidate.resolve()

    raise RegistryError(
        "no registry file found. Looked for: "
        + (", ".join(checked) if checked else "<nothing: pass --registry or set " + REGISTRY_ENV_VAR + ">")
    )


def load_registry(path: str | Path | None = None, *, search_cwd: bool = True) -> LoadedRegistry:
    """Load and validate the registry, raising :class:`RegistryError` with a
    human-readable message on any problem."""
    registry_path = resolve_registry_path(path, search_cwd=search_cwd)

    try:
        raw_text = registry_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryError(f"cannot read registry {registry_path}: {exc}") from exc

    try:
        document = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise RegistryError(f"invalid YAML in {registry_path}: {exc}") from exc

    if document is None:
        document = {}
    if not isinstance(document, dict):
        raise RegistryError(f"invalid registry {registry_path}: top level must be a mapping, got {type(document).__name__}")

    # A bare list is accepted as shorthand for {"servers": [...]}.
    if "servers" not in document and "gateway" not in document:
        raise RegistryError(
            f"invalid registry {registry_path}: expected a 'servers' list (and optionally a 'gateway' mapping)"
        )

    try:
        registry = Registry.model_validate(document)
    except ValidationError as exc:
        raise RegistryError(f"invalid registry {registry_path}:\n{_format_validation_error(exc)}") from exc

    _resolve_guide_path(registry, registry_path)

    return LoadedRegistry(registry=registry, path=registry_path)


def _resolve_guide_path(registry: Registry, registry_path: Path) -> None:
    """Validate ``catalog.guide_path`` and store it as an absolute path.

    A deployment guide that does not exist is a configuration error, not something to
    discover when a client happens to read ``catalog://guide``, so it fails here — and
    relative paths resolve against the registry file, not the process CWD.
    """
    configured = registry.gateway.catalog.guide_path
    if not configured:
        return

    guide = Path(configured).expanduser()
    if not guide.is_absolute():
        guide = registry_path.parent / guide
    if not guide.is_file():
        raise RegistryError(
            f"invalid registry {registry_path}: gateway.catalog.guide_path points to {guide}, which is not a file"
        )
    registry.gateway.catalog.guide_path = str(guide.resolve())


def _format_validation_error(exc: ValidationError) -> str:
    lines: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)
