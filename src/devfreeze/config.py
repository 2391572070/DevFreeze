"""Strict project configuration loading for ``.devfreeze.toml``."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tomllib
from typing import Any, Mapping
from urllib.parse import urlsplit

from .errors import ConfigError
from .models import validate_name


CONFIG_FILENAME = ".devfreeze.toml"


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    name: str
    command: tuple[str, ...]
    cwd: str = "."
    ports: tuple[int, ...] = ()
    ready_url: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    version: int = 1
    services: tuple[ServiceConfig, ...] = ()
    workspace_file: str | None = None


def _ensure_keys(data: Mapping[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"{context} contains unknown fields: {', '.join(unknown)}")


def _contained_service_cwd(root: Path, value: object, context: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ConfigError(f"{context}.cwd must be a non-empty string")
    path = Path(value).expanduser()
    resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    try:
        relative = resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ConfigError(f"{context}.cwd must stay inside the project root") from exc
    return os.fspath(relative) if os.fspath(relative) else "."


def _parse_service(raw: object, root: Path, index: int) -> ServiceConfig:
    context = f"services[{index}]"
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise ConfigError(f"{context} must be a TOML table")
    _ensure_keys(raw, {"name", "command", "cwd", "ports", "ready_url"}, context)
    try:
        name = validate_name(raw.get("name"), field=f"{context}.name")
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    command = raw.get("command")
    if not isinstance(command, list) or not command or not all(
        isinstance(argument, str) and argument and "\x00" not in argument for argument in command
    ):
        raise ConfigError(f"{context}.command must be a non-empty string array")
    cwd = _contained_service_cwd(root, raw.get("cwd", "."), context)

    raw_ports = raw.get("ports", [])
    if not isinstance(raw_ports, list) or any(
        isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
        for port in raw_ports
    ):
        raise ConfigError(f"{context}.ports must be an array of TCP ports")
    if len(set(raw_ports)) != len(raw_ports):
        raise ConfigError(f"{context}.ports contains duplicates")
    ready_url = raw.get("ready_url")
    if ready_url is not None:
        ready_url = validate_ready_url(ready_url, context=f"{context}.ready_url")
    return ServiceConfig(
        name=name,
        command=tuple(command),
        cwd=cwd,
        ports=tuple(raw_ports),
        ready_url=ready_url,
    )


def validate_ready_url(value: object, *, context: str = "ready_url") -> str:
    """Validate a non-secret local HTTP readiness URL."""

    if not isinstance(value, str) or not value or "\x00" in value:
        raise ConfigError(f"{context} must be a non-empty string")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ConfigError(f"{context} is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(
            f"{context} must be an http(s) URL without credentials, query, or fragment"
        )
    return value


def find_project_root(start: str | os.PathLike[str]) -> Path:
    """Find the nearest config or Git root, falling back to the start directory."""

    candidate = Path(start).expanduser().resolve()
    if candidate.is_file():
        candidate = candidate.parent
    for directory in (candidate, *candidate.parents):
        if (directory / CONFIG_FILENAME).is_file():
            return directory
        if (directory / ".git").exists():
            return directory
    return candidate


def load_config(root: str | os.PathLike[str]) -> ProjectConfig:
    """Load and strictly validate a project configuration if present."""

    project_root = Path(root).expanduser().resolve()
    path = project_root / CONFIG_FILENAME
    if not path.exists():
        return ProjectConfig()
    if path.is_symlink():
        raise ConfigError(f"refusing symbolic-link configuration: {path}")
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("project configuration must be a TOML table")
    _ensure_keys(raw, {"version", "services", "workspace_file"}, "configuration")
    version = raw.get("version", 1)
    if isinstance(version, bool) or version != 1:
        raise ConfigError(f"configuration version must be 1, got {version!r}")
    raw_services = raw.get("services", [])
    if not isinstance(raw_services, list):
        raise ConfigError("services must be an array of tables")
    services = tuple(_parse_service(item, project_root, index) for index, item in enumerate(raw_services))
    if len({service.name for service in services}) != len(services):
        raise ConfigError("service names must be unique")
    workspace_file = raw.get("workspace_file")
    if workspace_file is not None:
        if not isinstance(workspace_file, str) or not workspace_file or "\x00" in workspace_file:
            raise ConfigError("workspace_file must be a non-empty string")
        workspace_path = Path(workspace_file)
        resolved = (
            (project_root / workspace_path).resolve()
            if not workspace_path.is_absolute()
            else workspace_path.resolve()
        )
        try:
            resolved.relative_to(project_root)
        except ValueError as exc:
            raise ConfigError("workspace_file must stay inside the project root") from exc
        workspace_file = os.fspath(resolved.relative_to(project_root))
    return ProjectConfig(version=1, services=services, workspace_file=workspace_file)


__all__ = [
    "CONFIG_FILENAME",
    "ProjectConfig",
    "ServiceConfig",
    "find_project_root",
    "load_config",
    "validate_ready_url",
]
