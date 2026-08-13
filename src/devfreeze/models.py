"""Validated, versioned models for portable DevFreeze JSON snapshots.

The models intentionally contain metadata only.  In particular, snapshots do
not contain file contents, environment-variable values, or shell source.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import PurePosixPath, PureWindowsPath
import re
from typing import Any, ClassVar
from urllib.parse import urlsplit

from .errors import ValidationError


SNAPSHOT_SCHEMA_VERSION = 1
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_SERVICE_STATUSES = {"configured", "running", "stopped", "exited", "unknown"}


def validate_name(value: object, *, field: str = "name") -> str:
    """Validate a filesystem-safe snapshot or service name."""

    if not isinstance(value, str) or not NAME_PATTERN.fullmatch(value):
        raise ValidationError(
            f"{field} must match {NAME_PATTERN.pattern!r} and be at most 64 characters"
        )
    # Windows treats the part before the first dot as a device name.  Rejecting
    # these everywhere keeps snapshots safe to copy between operating systems.
    if value.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise ValidationError(f"{field} is a reserved filename: {value!r}")
    return value


def _reject_controls(value: str, field: str, *, allow_lines: bool = False) -> str:
    if "\x00" in value:
        raise ValidationError(f"{field} contains a NUL byte")
    for character in value:
        codepoint = ord(character)
        if (codepoint < 32 and not (allow_lines and character in "\n\r\t")) or codepoint == 127:
            raise ValidationError(f"{field} contains a control character")
        # Lone surrogate code points cannot be encoded as portable UTF-8 JSON.
        if 0xD800 <= codepoint <= 0xDFFF:
            raise ValidationError(f"{field} contains invalid Unicode")
    return value


def _string(
    value: object,
    field: str,
    *,
    optional: bool = False,
    allow_lines: bool = False,
    max_length: int = 4096,
) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string" + (" or null" if optional else ""))
    if not value:
        raise ValidationError(f"{field} must not be empty")
    if len(value) > max_length:
        raise ValidationError(f"{field} is longer than {max_length} characters")
    return _reject_controls(value, field, allow_lines=allow_lines)


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValidationError(f"{field} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        raise ValidationError(f"{field} is missing required fields: {', '.join(missing)}")
    if unknown:
        raise ValidationError(f"{field} contains unknown fields: {', '.join(unknown)}")


def _sequence(value: object, field: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ValidationError(f"{field} must be a JSON array")
    return value


def _path_flavour(value: str) -> PurePosixPath | PureWindowsPath:
    windows = PureWindowsPath(value)
    if windows.is_absolute() and not PurePosixPath(value).is_absolute():
        return windows
    return PurePosixPath(value)


def _absolute_path(value: object, field: str) -> str:
    result = _string(value, field, max_length=32768)
    assert isinstance(result, str)
    path = _path_flavour(result)
    if not path.is_absolute():
        raise ValidationError(f"{field} must be an absolute path")
    if ".." in path.parts:
        raise ValidationError(f"{field} must not contain '..'")
    return result


def _is_within(child: str, parent: str) -> bool:
    child_path = _path_flavour(child)
    parent_path = _path_flavour(parent)
    if type(child_path) is not type(parent_path):
        return False
    try:
        child_path.relative_to(parent_path)
    except ValueError:
        return False
    return True


def _relative_repo_path(value: object, field: str) -> str:
    result = _string(value, field, max_length=32768)
    assert isinstance(result, str)
    posix = PurePosixPath(result)
    windows = PureWindowsPath(result)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise ValidationError(f"{field} must be relative to the repository")
    if ".." in posix.parts or ".." in windows.parts:
        raise ValidationError(f"{field} must not traverse outside the repository")
    if result in {".", ".."}:
        raise ValidationError(f"{field} is not a file path")
    return result


def _normalise_tuple(value: object, field: str) -> tuple[Any, ...]:
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    raise ValidationError(f"{field} must be a sequence")


def _port(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer")
    if not 1 <= value <= 65535:
        raise ValidationError(f"{field} must be between 1 and 65535")
    return value


@dataclass(frozen=True, slots=True)
class WorkspaceState:
    root: str
    cwd: str
    workspace_file: str | None = None

    def __post_init__(self) -> None:
        root = _absolute_path(self.root, "workspace.root")
        cwd = _absolute_path(self.cwd, "workspace.cwd")
        if not _is_within(cwd, root):
            raise ValidationError("workspace.cwd must be inside workspace.root")
        workspace_file = self.workspace_file
        if workspace_file is not None:
            workspace_file = _absolute_path(workspace_file, "workspace.workspace_file")
            if not _is_within(workspace_file, root):
                raise ValidationError("workspace.workspace_file must be inside workspace.root")
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "workspace_file", workspace_file)

    def to_dict(self) -> dict[str, Any]:
        return {"root": self.root, "cwd": self.cwd, "workspace_file": self.workspace_file}

    @classmethod
    def from_dict(cls, value: object) -> "WorkspaceState":
        data = _mapping(value, "workspace")
        _exact_keys(data, {"root", "cwd", "workspace_file"}, "workspace")
        return cls(data["root"], data["cwd"], data["workspace_file"])


@dataclass(frozen=True, slots=True)
class GitState:
    remote: str | None
    branch: str | None
    head: str
    dirty: bool
    changed_files: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        remote = _string(self.remote, "git.remote", optional=True, max_length=32768)
        branch = _string(self.branch, "git.branch", optional=True, max_length=1024)
        head = _string(self.head, "git.head", max_length=128)
        assert isinstance(head, str)
        if head != "unborn" and not re.fullmatch(r"[0-9a-fA-F]{4,128}", head):
            raise ValidationError("git.head must be a hexadecimal object ID or 'unborn'")
        if not isinstance(self.dirty, bool):
            raise ValidationError("git.dirty must be a boolean")
        raw_files = _normalise_tuple(self.changed_files, "git.changed_files")
        files = tuple(
            _relative_repo_path(item, f"git.changed_files[{index}]")
            for index, item in enumerate(raw_files)
        )
        if len(set(files)) != len(files):
            raise ValidationError("git.changed_files must not contain duplicates")
        object.__setattr__(self, "remote", remote)
        object.__setattr__(self, "branch", branch)
        object.__setattr__(self, "head", head.lower() if head != "unborn" else head)
        object.__setattr__(self, "changed_files", files)

    def to_dict(self) -> dict[str, Any]:
        return {
            "remote": self.remote,
            "branch": self.branch,
            "head": self.head,
            "dirty": self.dirty,
            "changed_files": list(self.changed_files),
        }

    @classmethod
    def from_dict(cls, value: object) -> "GitState":
        data = _mapping(value, "git")
        _exact_keys(data, {"remote", "branch", "head", "dirty", "changed_files"}, "git")
        return cls(
            data["remote"],
            data["branch"],
            data["head"],
            data["dirty"],
            tuple(_sequence(data["changed_files"], "git.changed_files")),
        )


@dataclass(frozen=True, slots=True)
class PlatformState:
    system: str
    release: str
    machine: str
    python: str

    def __post_init__(self) -> None:
        for field_name in ("system", "release", "machine", "python"):
            value = _string(getattr(self, field_name), f"platform.{field_name}", max_length=1024)
            object.__setattr__(self, field_name, value)

    def to_dict(self) -> dict[str, str]:
        return {
            "system": self.system,
            "release": self.release,
            "machine": self.machine,
            "python": self.python,
        }

    @classmethod
    def from_dict(cls, value: object) -> "PlatformState":
        data = _mapping(value, "platform")
        _exact_keys(data, {"system", "release", "machine", "python"}, "platform")
        return cls(data["system"], data["release"], data["machine"], data["python"])


@dataclass(frozen=True, slots=True)
class ToolInfo:
    name: str
    version: str | None

    def __post_init__(self) -> None:
        name = validate_name(self.name, field="tooling.name")
        version = _string(self.version, "tooling.version", optional=True, max_length=4096)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "version", version)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version}

    @classmethod
    def from_dict(cls, value: object) -> "ToolInfo":
        data = _mapping(value, "tooling item")
        _exact_keys(data, {"name", "version"}, "tooling item")
        return cls(data["name"], data["version"])


@dataclass(frozen=True, slots=True)
class ServiceState:
    name: str
    command: tuple[str, ...]
    cwd: str
    ports: tuple[int, ...] = ()
    ready_url: str | None = None
    status: str = "unknown"

    def __post_init__(self) -> None:
        name = validate_name(self.name, field="services.name")
        raw_command = _normalise_tuple(self.command, "services.command")
        if not raw_command:
            raise ValidationError("services.command must not be empty")
        command: list[str] = []
        for index, argument in enumerate(raw_command):
            parsed = _string(argument, f"services.command[{index}]", max_length=32768)
            assert isinstance(parsed, str)
            command.append(parsed)
        cwd = _absolute_path(self.cwd, "services.cwd")
        raw_ports = _normalise_tuple(self.ports, "services.ports")
        ports = tuple(_port(item, f"services.ports[{index}]") for index, item in enumerate(raw_ports))
        if len(set(ports)) != len(ports):
            raise ValidationError("services.ports must not contain duplicates")
        ready_url = _string(self.ready_url, "services.ready_url", optional=True, max_length=8192)
        if ready_url is not None:
            try:
                parsed_ready_url = urlsplit(ready_url)
            except ValueError as exc:
                raise ValidationError("services.ready_url is invalid") from exc
            if (
                parsed_ready_url.scheme not in {"http", "https"}
                or not parsed_ready_url.hostname
                or parsed_ready_url.username is not None
                or parsed_ready_url.password is not None
                or parsed_ready_url.query
                or parsed_ready_url.fragment
            ):
                raise ValidationError(
                    "services.ready_url must be an http(s) URL without credentials, "
                    "query, or fragment"
                )
        status = _string(self.status, "services.status", max_length=32)
        assert isinstance(status, str)
        if status not in _SERVICE_STATUSES:
            allowed = ", ".join(sorted(_SERVICE_STATUSES))
            raise ValidationError(f"services.status must be one of: {allowed}")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "command", tuple(command))
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "ports", ports)
        object.__setattr__(self, "ready_url", ready_url)
        object.__setattr__(self, "status", status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": list(self.command),
            "cwd": self.cwd,
            "ports": list(self.ports),
            "ready_url": self.ready_url,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ServiceState":
        data = _mapping(value, "service")
        _exact_keys(data, {"name", "command", "cwd", "ports", "ready_url", "status"}, "service")
        return cls(
            name=data["name"],
            command=tuple(_sequence(data["command"], "service.command")),
            cwd=data["cwd"],
            ports=tuple(_sequence(data["ports"], "service.ports")),
            ready_url=data["ready_url"],
            status=data["status"],
        )


@dataclass(frozen=True, slots=True)
class Snapshot:
    name: str
    created_at: str
    workspace: WorkspaceState
    git: GitState | None
    platform: PlatformState
    tooling: tuple[ToolInfo, ...] = ()
    services: tuple[ServiceState, ...] = ()
    note: str | None = None
    schema_version: int = SNAPSHOT_SCHEMA_VERSION

    CURRENT_SCHEMA_VERSION: ClassVar[int] = SNAPSHOT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        name = validate_name(self.name, field="snapshot.name")
        created_at = _string(self.created_at, "snapshot.created_at", max_length=128)
        assert isinstance(created_at, str)
        try:
            parsed_time = datetime.fromisoformat(created_at)
        except ValueError as exc:
            raise ValidationError("snapshot.created_at must be an ISO 8601 datetime") from exc
        if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
            raise ValidationError("snapshot.created_at must include a UTC offset")
        if not isinstance(self.workspace, WorkspaceState):
            raise ValidationError("snapshot.workspace must be a WorkspaceState")
        if self.git is not None and not isinstance(self.git, GitState):
            raise ValidationError("snapshot.git must be a GitState or null")
        if not isinstance(self.platform, PlatformState):
            raise ValidationError("snapshot.platform must be a PlatformState")
        raw_tooling = _normalise_tuple(self.tooling, "snapshot.tooling")
        if not all(isinstance(tool, ToolInfo) for tool in raw_tooling):
            raise ValidationError("snapshot.tooling must contain ToolInfo values")
        if len({tool.name for tool in raw_tooling}) != len(raw_tooling):
            raise ValidationError("snapshot.tooling contains duplicate names")
        raw_services = _normalise_tuple(self.services, "snapshot.services")
        if not all(isinstance(service, ServiceState) for service in raw_services):
            raise ValidationError("snapshot.services must contain ServiceState values")
        if len({service.name for service in raw_services}) != len(raw_services):
            raise ValidationError("snapshot.services contains duplicate names")
        # A snapshot can be inspected even if a captured/hand-edited service
        # points elsewhere.  The recovery layer deliberately rejects such a
        # command before execution; keeping it representable here lets users
        # see the unsafe drift instead of making the entire snapshot unreadable.
        note = self.note
        if note is not None:
            note = _string(note, "snapshot.note", optional=True, allow_lines=True, max_length=100_000)
        if isinstance(self.schema_version, bool) or self.schema_version != SNAPSHOT_SCHEMA_VERSION:
            raise ValidationError(f"unsupported snapshot schema_version: {self.schema_version!r}")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "tooling", tuple(raw_tooling))
        object.__setattr__(self, "services", tuple(raw_services))
        object.__setattr__(self, "note", note)

    @classmethod
    def create(
        cls,
        *,
        name: str,
        workspace: WorkspaceState,
        git: GitState | None,
        platform: PlatformState,
        tooling: Sequence[ToolInfo] = (),
        services: Sequence[ServiceState] = (),
        note: str | None = None,
        created_at: str | None = None,
    ) -> "Snapshot":
        if created_at is None:
            created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        return cls(
            name=name,
            created_at=created_at,
            workspace=workspace,
            git=git,
            platform=platform,
            tooling=tuple(tooling),
            services=tuple(services),
            note=note,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "created_at": self.created_at,
            "workspace": self.workspace.to_dict(),
            "git": None if self.git is None else self.git.to_dict(),
            "platform": self.platform.to_dict(),
            "tooling": [tool.to_dict() for tool in self.tooling],
            "services": [service.to_dict() for service in self.services],
            "note": self.note,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, allow_nan=False)

    @classmethod
    def from_dict(cls, value: object) -> "Snapshot":
        data = _mapping(value, "snapshot")
        expected = {
            "schema_version",
            "name",
            "created_at",
            "workspace",
            "git",
            "platform",
            "tooling",
            "services",
            "note",
        }
        _exact_keys(data, expected, "snapshot")
        version = data["schema_version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValidationError("snapshot.schema_version must be an integer")
        git = None if data["git"] is None else GitState.from_dict(data["git"])
        return cls(
            name=data["name"],
            created_at=data["created_at"],
            workspace=WorkspaceState.from_dict(data["workspace"]),
            git=git,
            platform=PlatformState.from_dict(data["platform"]),
            tooling=tuple(
                ToolInfo.from_dict(item)
                for item in _sequence(data["tooling"], "snapshot.tooling")
            ),
            services=tuple(
                ServiceState.from_dict(item)
                for item in _sequence(data["services"], "snapshot.services")
            ),
            note=data["note"],
            schema_version=version,
        )

    @classmethod
    def from_json(cls, value: str | bytes | bytearray) -> "Snapshot":
        if not isinstance(value, (str, bytes, bytearray)):
            raise ValidationError("snapshot JSON must be text or UTF-8 bytes")

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, item in pairs:
                if key in result:
                    raise ValidationError(f"snapshot JSON contains duplicate field: {key}")
                result[key] = item
            return result

        def reject_constant(constant: str) -> None:
            raise ValidationError(f"snapshot JSON contains invalid number: {constant}")

        try:
            decoded = json.loads(
                value,
                object_pairs_hook=unique_object,
                parse_constant=reject_constant,
            )
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError("snapshot is not valid UTF-8 JSON") from exc
        return cls.from_dict(decoded)
