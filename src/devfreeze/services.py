"""Managed development services and listening-port discovery.

This module deliberately uses only the Python standard library.  Commands are
always passed to :class:`subprocess.Popen` as argument vectors; a shell is never
involved.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Callable

from .errors import ValidationError
from .executables import trusted_which
from .models import validate_name


REGISTRY_VERSION = 1


class ServiceError(RuntimeError):
    """Base exception for managed-service failures."""


class RegistryError(ServiceError):
    """Raised when the on-disk service registry is invalid or inaccessible."""


class ServiceAlreadyRunning(ServiceError):
    """Raised when a live service already owns a requested name."""


class UnsafeProcessError(ServiceError):
    """Raised when a process no longer has the identity that was registered."""


@dataclass(slots=True)
class ServiceRecord:
    """Serializable identity and restore information for a managed process."""

    name: str
    argv: list[str]
    cwd: str
    pid: int
    process_start_time: str | None
    started_at: str
    workspace_root: str | None = field(default=None, kw_only=True)
    log_path: str | None = None
    declared_ports: list[int] = field(default_factory=list)
    detected_ports: list[int] = field(default_factory=list)
    ready_url: str | None = None
    status: str = "running"
    detached: bool = False
    exit_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation."""

        result = asdict(self)
        result["argv"] = list(self.argv)
        result["declared_ports"] = sorted(set(self.declared_ports))
        result["detected_ports"] = sorted(set(self.detected_ports))
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ServiceRecord":
        """Load a record, accepting missing optional fields from older files."""

        try:
            name = value["name"]
            raw_argv = value["argv"]
            cwd = value["cwd"]
            pid = value["pid"]
            started_at = value["started_at"]
        except (KeyError, TypeError, ValueError) as exc:
            raise RegistryError("service record is missing a required field") from exc

        try:
            _validate_name(name)
            command = _validate_argv(raw_argv)
        except (TypeError, ValueError) as exc:
            raise RegistryError(f"invalid service record: {exc}") from exc
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            raise RegistryError("service record cwd must be an absolute path")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise RegistryError("service record pid must be a positive integer")
        if not isinstance(started_at, str) or not started_at:
            raise RegistryError("service record started_at must be a non-empty string")

        workspace_value = value.get("workspace_root") or cwd
        if not isinstance(workspace_value, str) or not Path(workspace_value).is_absolute():
            raise RegistryError("service record workspace_root must be an absolute path")

        start_time_value = value.get("process_start_time")
        log_path_value = value.get("log_path")
        ready_url_value = value.get("ready_url")
        try:
            declared_ports = _normalise_ports(value.get("declared_ports", []))
            detected_ports = _normalise_ports(value.get("detected_ports", []))
        except (TypeError, ValueError) as exc:
            raise RegistryError("service record contains an invalid TCP port") from exc

        return cls(
            name=name,
            argv=command,
            cwd=cwd,
            pid=pid,
            process_start_time=(
                None if start_time_value is None else str(start_time_value)
            ),
            started_at=started_at,
            workspace_root=workspace_value,
            log_path=None if log_path_value is None else str(log_path_value),
            declared_ports=declared_ports,
            detected_ports=detected_ports,
            ready_url=None if ready_url_value is None else str(ready_url_value),
            status=str(value.get("status", "running")),
            detached=bool(value.get("detached", False)),
            exit_code=(
                None if value.get("exit_code") is None else int(value["exit_code"])
            ),
        )


def default_registry_path() -> Path:
    """Return the registry path, honoring ``DEVFREEZE_HOME`` first."""

    explicit_home = os.environ.get("DEVFREEZE_HOME")
    if explicit_home:
        return Path(explicit_home).expanduser() / "runtime" / "services.json"
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local/share"
    return base / "devfreeze/runtime/services.json"


class ServiceRegistry:
    """Private JSON registry with atomic writes and cross-process locking."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path).expanduser() if path is not None else default_registry_path()
        self._lock = threading.RLock()

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(f".{self.path.name}.lock")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Serialize registry mutations across threads and CLI processes."""

        with self._lock:
            if self.path.parent.name == "runtime":
                _ensure_private_directory(self.path.parent.parent)
            _ensure_private_directory(self.path.parent)
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(self.lock_path, flags, 0o600)
                os.chmod(self.lock_path, 0o600)
            except OSError as exc:
                raise RegistryError(f"cannot lock service registry: {self.path}") from exc
            try:
                _lock_descriptor(descriptor)
                yield
            finally:
                try:
                    _unlock_descriptor(descriptor)
                finally:
                    os.close(descriptor)

    def _read_unlocked(self) -> list[ServiceRecord]:
        if not self.path.exists():
            return []
        if self.path.is_symlink():
            raise RegistryError(f"refusing symbolic-link service registry: {self.path}")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RegistryError(f"cannot read service registry: {self.path}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("services"), list):
            raise RegistryError(f"invalid service registry: {self.path}")
        version = payload.get("version", REGISTRY_VERSION)
        if version != REGISTRY_VERSION:
            raise RegistryError(f"unsupported service registry version: {version!r}")
        return [ServiceRecord.from_dict(item) for item in payload["services"]]

    def _write_unlocked(self, records: Sequence[ServiceRecord]) -> None:
        _ensure_private_directory(self.path.parent)
        payload = {
            "version": REGISTRY_VERSION,
            "services": [record.to_dict() for record in records],
        }
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                os.chmod(temporary.name, 0o600)
                json.dump(payload, temporary, ensure_ascii=False, indent=2)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, self.path)
            temporary_name = None
            os.chmod(self.path, 0o600)
            _fsync_directory(self.path.parent)
        except OSError as exc:
            raise RegistryError(f"cannot write service registry: {self.path}") from exc
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass

    def upsert(self, record: ServiceRecord) -> None:
        """Replace the record with the same workspace/name key."""

        with self._transaction():
            records = self._read_unlocked()
            replacement = [item for item in records if _record_key(item) != _record_key(record)]
            replacement.append(record)
            replacement.sort(key=_record_key)
            self._write_unlocked(replacement)

    def register(self, record: ServiceRecord) -> None:
        """Atomically claim a workspace/name key for a newly started process."""

        with self._transaction():
            records = self._read_unlocked()
            existing = next(
                (item for item in records if _record_key(item) == _record_key(record)),
                None,
            )
            if existing is not None and _record_or_group_alive(existing):
                raise ServiceAlreadyRunning(
                    f"service {record.name!r} is already running as PID {existing.pid} "
                    f"in {existing.workspace_root}"
                )
            replacement = [item for item in records if _record_key(item) != _record_key(record)]
            replacement.append(record)
            replacement.sort(key=_record_key)
            self._write_unlocked(replacement)

    @contextmanager
    def launch_slot(
        self,
        name: str,
        workspace_root: str | os.PathLike[str],
    ) -> Iterator[Callable[[ServiceRecord], None]]:
        """Reserve a workspace/name while a child is spawned and registered."""

        scope = _normalise_scope(workspace_root)
        with self._transaction():
            records = self._read_unlocked()
            existing = next(
                (
                    item
                    for item in records
                    if item.name == name and _record_scope(item) == scope
                ),
                None,
            )
            if existing is not None and _record_or_group_alive(existing):
                raise ServiceAlreadyRunning(
                    f"service {name!r} is already running as PID {existing.pid} "
                    f"in {scope}"
                )
            base = [
                item
                for item in records
                if not (item.name == name and _record_scope(item) == scope)
            ]
            committed = False

            def commit(record: ServiceRecord) -> None:
                nonlocal committed
                if committed:
                    raise RegistryError("service launch slot was already committed")
                if _record_key(record) != (scope, name):
                    raise RegistryError("service record does not match its launch slot")
                replacement = [*base, record]
                replacement.sort(key=_record_key)
                self._write_unlocked(replacement)
                committed = True

            yield commit

    def get(
        self,
        name: str,
        *,
        workspace_root: str | os.PathLike[str] | None = None,
        refresh: bool = True,
    ) -> ServiceRecord | None:
        matches = [record for record in self.list(refresh=refresh) if record.name == name]
        if workspace_root is not None:
            scope = _normalise_scope(workspace_root)
            matches = [record for record in matches if _record_scope(record) == scope]
        if not matches:
            return None
        if len(matches) > 1:
            raise RegistryError(
                f"service name {name!r} is ambiguous; run the command from its workspace"
            )
        return matches[0]

    def list(self, *, refresh: bool = True) -> list[ServiceRecord]:
        with self._transaction():
            records = self._read_unlocked()
        if refresh:
            for record in records:
                if _record_or_group_alive(record):
                    record.status = "running"
                    record.detected_ports = detect_listening_ports(record.pid)
                else:
                    record.status = "stale"
                    record.detected_ports = []
        return records

    def remove(
        self,
        name: str,
        *,
        workspace_root: str | os.PathLike[str] | None = None,
        expected_pid: int | None = None,
    ) -> bool:
        scope = None if workspace_root is None else _normalise_scope(workspace_root)
        with self._transaction():
            records = self._read_unlocked()
            candidates = [
                record
                for record in records
                if record.name == name and (scope is None or _record_scope(record) == scope)
            ]
            if scope is None and expected_pid is None and len(candidates) > 1:
                raise RegistryError(
                    f"service name {name!r} is ambiguous; specify its workspace"
                )
            retained = [
                record
                for record in records
                if not (
                    record.name == name
                    and (scope is None or _record_scope(record) == scope)
                    and (expected_pid is None or record.pid == expected_pid)
                )
            ]
            if len(retained) == len(records):
                return False
            self._write_unlocked(retained)
            return True

    def cleanup_stale(self) -> list[ServiceRecord]:
        """Remove and return records whose registered process no longer exists."""

        with self._transaction():
            records = self._read_unlocked()
            stale = [
                record
                for record in records
                if not _record_or_group_alive(record)
            ]
            if stale:
                stale_keys = {(*_record_key(record), record.pid) for record in stale}
                self._write_unlocked(
                    [
                        record
                        for record in records
                        if (*_record_key(record), record.pid) not in stale_keys
                    ]
                )
            for record in stale:
                record.status = "stale"
                record.detected_ports = []
            return stale

    def stop(
        self,
        name: str,
        *,
        workspace_root: str | os.PathLike[str] | None = None,
        timeout: float = 5.0,
        force: bool = False,
    ) -> bool:
        """Stop a managed service after verifying its process identity.

        ``False`` means the record was already stale (and has been removed).
        A PID whose recorded creation identity differs is never signalled.
        """

        record = self.get(name, workspace_root=workspace_root, refresh=False)
        if record is None:
            return False
        leader_matches = process_identity_matches(
            record.pid,
            record.process_start_time,
        )
        if not leader_matches:
            if _managed_group_alive(record):
                if not sys.platform.startswith("linux"):
                    raise UnsafeProcessError(
                        f"cannot prove the identity of service {name!r} after "
                        "its original leader exited; the record was retained"
                    )
                # Linux procfs proves that this is still the process group
                # created with pid == pgid by run_detached.  It can be safely
                # terminated even after its original leader exited.
                _signal_managed_process(
                    record,
                    signal.SIGTERM,
                    trusted_group=True,
                )
                deadline = time.monotonic() + max(timeout, 0.0)
                while time.monotonic() < deadline and _termination_target_alive(record):
                    time.sleep(0.05)
                if _termination_target_alive(record) and not force:
                    raise ServiceError(
                        f"service {name!r} did not stop within {timeout:g}s; "
                        "use force=True to send SIGKILL"
                    )
                if _termination_target_alive(record):
                    _signal_managed_process(
                        record,
                        getattr(signal, "SIGKILL", signal.SIGTERM),
                        trusted_group=True,
                    )
                    kill_deadline = time.monotonic() + 1.0
                    while time.monotonic() < kill_deadline and _termination_target_alive(record):
                        time.sleep(0.02)
                if _termination_target_alive(record):
                    raise ServiceError(
                        f"service {name!r} is still running after forced termination"
                    )
                self.remove(
                    name,
                    workspace_root=record.workspace_root,
                    expected_pid=record.pid,
                )
                return True
            self.remove(
                name,
                workspace_root=record.workspace_root,
                expected_pid=record.pid,
            )
            return False

        _signal_managed_process(record, signal.SIGTERM)
        deadline = time.monotonic() + max(timeout, 0.0)
        while time.monotonic() < deadline:
            if not _termination_target_alive(record):
                self.remove(
                    name,
                    workspace_root=record.workspace_root,
                    expected_pid=record.pid,
                )
                return True
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

        if _termination_target_alive(record) and not force:
            raise ServiceError(
                f"service {name!r} did not stop within {timeout:g}s; "
                "use force=True to send SIGKILL"
            )
        if _termination_target_alive(record):
            kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
            _signal_managed_process(record, kill_signal, trusted_group=True)
            kill_deadline = time.monotonic() + 1.0
            while time.monotonic() < kill_deadline:
                if not _termination_target_alive(record):
                    break
                time.sleep(0.02)

        if _termination_target_alive(record):
            raise ServiceError(f"service {name!r} is still running after forced termination")
        self.remove(
            name,
            workspace_root=record.workspace_root,
            expected_pid=record.pid,
        )
        return True


def run_foreground(
    name: str,
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    workspace_root: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    registry: ServiceRegistry | None = None,
    declared_ports: Sequence[int] = (),
    ready_url: str | None = None,
) -> int:
    """Run a registered foreground command and forward termination signals."""

    _validate_name(name)
    command = _validate_argv(argv)
    service_registry = registry or ServiceRegistry()
    working_directory = str(Path(cwd or os.getcwd()).expanduser().resolve())
    workspace = _workspace_for(working_directory, workspace_root)
    command = _resolve_windows_command(command)
    with service_registry.launch_slot(name, workspace) as commit:
        process = subprocess.Popen(
            command,
            cwd=working_directory,
            env=None if env is None else dict(env),
            shell=False,
            **(
                {
                    "creationflags": getattr(
                        subprocess,
                        "CREATE_NEW_PROCESS_GROUP",
                        0,
                    )
                }
                if os.name == "nt"
                else {}
            ),
        )
        record = _new_record(
            name,
            command,
            working_directory,
            process.pid,
            workspace_root=workspace,
            declared_ports=declared_ports,
            ready_url=ready_url,
            detached=False,
        )
        try:
            commit(record)
        except Exception:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
    try:
        with _forward_signals(process):
            return process.wait()
    finally:
        service_registry.remove(
            name,
            workspace_root=workspace,
            expected_pid=process.pid,
        )


def run_detached(
    name: str,
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    workspace_root: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    registry: ServiceRegistry | None = None,
    log_path: str | os.PathLike[str] | None = None,
    declared_ports: Sequence[int] = (),
    ready_url: str | None = None,
) -> ServiceRecord:
    """Start a detached command, logging stdout/stderr, and register it."""

    _validate_name(name)
    command = _validate_argv(argv)
    service_registry = registry or ServiceRegistry()
    working_directory = str(Path(cwd or os.getcwd()).expanduser().resolve())
    workspace = _workspace_for(working_directory, workspace_root)
    command = _resolve_windows_command(command)
    output_path = (
        Path(log_path).expanduser()
        if log_path is not None
        else _default_log_path(service_registry, name, workspace)
    )
    if log_path is None:
        _ensure_private_directory(output_path.parent)
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)

    popen_options: dict[str, Any] = {}
    if os.name == "posix":
        popen_options["start_new_session"] = True
    elif os.name == "nt":  # pragma: no cover - exercised on Windows
        popen_options["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )

    with service_registry.launch_slot(name, workspace) as commit:
        with _open_private_append(output_path) as output:
            process = subprocess.Popen(
                command,
                cwd=working_directory,
                env=None if env is None else dict(env),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                shell=False,
                **popen_options,
            )

        record = _new_record(
            name,
            command,
            working_directory,
            process.pid,
            workspace_root=workspace,
            log_path=str(output_path.resolve()),
            declared_ports=declared_ports,
            ready_url=ready_url,
            detached=True,
        )
        if record.process_start_time is None:
            # A detached process must have a portable creation identity.  A PID-only
            # registry is unsafe because the PID can be reused between CLI runs.
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                _cleanup_failed_launch(process, record)
                raise ServiceError("could not read a safe identity for the new process")
            _cleanup_failed_launch(process, record)
            raise ServiceError(f"managed command exited immediately with {process.returncode}")
        try:
            exit_code = process.wait(timeout=0.05)
        except subprocess.TimeoutExpired:
            exit_code = None
        if exit_code is not None:
            _cleanup_failed_launch(process, record)
            raise ServiceError(f"managed command exited immediately with {exit_code}")
        record.detected_ports = detect_listening_ports(process.pid)
        try:
            commit(record)
        except Exception:
            _cleanup_failed_launch(process, record)
            raise
    _reap_in_background(process)
    return record


def process_start_time(pid: int) -> str | None:
    """Return an OS process-creation identity, or ``None`` if unavailable."""

    if pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        stat = _read_linux_proc_stat(pid)
        return None if stat is None else f"linux:{stat[1]}"
    if os.name == "posix":
        executable = trusted_which("ps")
        if executable is None:
            return None
        try:
            completed = subprocess.run(
                [executable, "-o", "lstart=", "-p", str(pid)],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2.0,
                shell=False,
                env={**os.environ, "LC_ALL": "C"},
            )
        except (OSError, subprocess.SubprocessError):
            return None
        value = completed.stdout.strip()
        return f"ps:{value}" if completed.returncode == 0 and value else None
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        executable = trusted_which("powershell.exe") or trusted_which("powershell")
        if executable is None:
            return None
        expression = (
            f"(Get-Process -Id {pid} -ErrorAction Stop)."
            "StartTime.ToUniversalTime().Ticks"
        )
        try:
            completed = subprocess.run(
                [
                    executable,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    expression,
                ],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3.0,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        value = completed.stdout.strip()
        return f"windows:{value}" if completed.returncode == 0 and value.isdigit() else None
    return None


def is_pid_alive(pid: int) -> bool:
    """Return whether *pid* refers to a live, non-zombie process."""

    if pid <= 0:
        return False
    if sys.platform.startswith("linux"):
        stat = _read_linux_proc_stat(pid)
        return stat is not None and stat[0] != "Z"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def process_identity_matches(pid: int, expected_start_time: str | None) -> bool:
    """Check activity and guard against PID reuse with a creation identity."""

    if not is_pid_alive(pid) or expected_start_time is None:
        return False
    return process_start_time(pid) == str(expected_start_time)


def is_process_alive(pid: int, expected_start_time: str | None = None) -> bool:
    """Compatibility helper used by the snapshot and CLI layers.

    On Linux a supplied start time also proves that the PID has not been reused.
    Without a supplied start time this performs an activity check only.
    """

    if expected_start_time is None:
        return is_pid_alive(pid)
    return process_identity_matches(pid, expected_start_time)


def service_record_is_running(record: ServiceRecord) -> bool:
    """Return the registry's conservative process-tree liveness decision."""

    return _record_or_group_alive(record)


def descendant_pids(pid: int) -> set[int]:
    """Return *pid* and its descendants on Linux; safely degrade elsewhere."""

    result = {pid}
    if not sys.platform.startswith("linux"):
        return result
    children_by_parent: dict[int, set[int]] = {}
    try:
        entries = os.scandir("/proc")
    except OSError:
        return result
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            child_pid = int(entry.name)
            stat = _read_linux_proc_stat(child_pid)
            if stat is None:
                continue
            children_by_parent.setdefault(stat[2], set()).add(child_pid)
    pending = [pid]
    while pending:
        parent = pending.pop()
        for child in children_by_parent.get(parent, ()):
            if child not in result:
                result.add(child)
                pending.append(child)
    return result


def detect_listening_ports(pid: int, *, include_descendants: bool = True) -> list[int]:
    """Find TCP listening ports owned by a process tree.

    Linux uses `/proc/<pid>/fd` socket inodes joined against
    `/proc/net/tcp{,6}`.  Other platforms use ``lsof`` when available, or
    return an empty list without failing the caller.
    """

    if pid <= 0:
        return []
    pids = descendant_pids(pid) if include_descendants else {pid}
    if sys.platform.startswith("linux"):
        inodes: set[str] = set()
        for process_pid in pids:
            fd_directory = Path("/proc") / str(process_pid) / "fd"
            try:
                descriptors = list(fd_directory.iterdir())
            except (FileNotFoundError, PermissionError, OSError):
                continue
            for descriptor in descriptors:
                try:
                    target = os.readlink(descriptor)
                except (FileNotFoundError, PermissionError, OSError):
                    continue
                match = re.fullmatch(r"socket:\[(\d+)\]", target)
                if match:
                    inodes.add(match.group(1))
        if not inodes:
            return []
        ports: set[int] = set()
        for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
            ports.update(_ports_for_inodes(table, inodes))
        return sorted(ports)
    return _detect_ports_with_lsof(pids)


def _read_linux_proc_stat(pid: int) -> tuple[str, str, int, int] | None:
    """Return ``(state, start_ticks, parent_pid, process_group)`` from procfs."""

    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None
    # The comm field is parenthesised and may itself contain spaces or `)`.
    closing_parenthesis = raw.rfind(")")
    if closing_parenthesis < 0:
        return None
    fields = raw[closing_parenthesis + 1 :].split()
    # fields begins at kernel field 3; ppid is 4 and starttime is 22.
    if len(fields) <= 19:
        return None
    try:
        return fields[0], fields[19], int(fields[1]), int(fields[2])
    except (ValueError, IndexError):
        return None


def _ports_for_inodes(table: Path, inodes: set[str]) -> set[int]:
    ports: set[int] = set()
    try:
        lines = table.read_text(encoding="ascii").splitlines()[1:]
    except (FileNotFoundError, PermissionError, OSError, UnicodeError):
        return ports
    for line in lines:
        columns = line.split()
        if len(columns) < 10 or columns[3] != "0A" or columns[9] not in inodes:
            continue
        try:
            port = int(columns[1].rsplit(":", 1)[1], 16)
        except (IndexError, ValueError):
            continue
        if 0 < port <= 65535:
            ports.add(port)
    return ports


def _detect_ports_with_lsof(pids: set[int]) -> list[int]:
    executable = trusted_which("lsof")
    if executable is None or not pids:
        return []
    try:
        completed = subprocess.run(
            [
                executable,
                "-nP",
                "-a",
                "-p",
                ",".join(str(pid) for pid in sorted(pids)),
                "-iTCP",
                "-sTCP:LISTEN",
                "-Fn",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    ports: set[int] = set()
    for line in completed.stdout.splitlines():
        if not line.startswith("n"):
            continue
        match = re.search(r":(\d+)(?:\s|$)", line)
        if match:
            port = int(match.group(1))
            if 0 < port <= 65535:
                ports.add(port)
    return sorted(ports)


def _normalise_ports(values: Any) -> list[int]:
    if values is None:
        return []
    if isinstance(values, (str, bytes)):
        raise TypeError("ports must be an iterable of integers")
    result: set[int] = set()
    for value in values:
        port = int(value)
        if not 1 <= port <= 65535:
            raise ValueError(f"invalid TCP port: {port}")
        result.add(port)
    return sorted(result)


def _validate_argv(argv: Sequence[str]) -> list[str]:
    if isinstance(argv, (str, bytes)) or not argv:
        raise ValueError("argv must be a non-empty sequence of strings")
    command = list(argv)
    if not all(isinstance(argument, str) for argument in command):
        raise TypeError("argv must contain only strings")
    if any(not argument for argument in command):
        raise ValueError("argv arguments must not be empty")
    if any("\x00" in argument for argument in command):
        raise ValueError("argv arguments must not contain NUL bytes")
    return command


def _validate_name(name: str) -> None:
    try:
        validate_name(name, field="service name")
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc


def _new_record(
    name: str,
    argv: list[str],
    cwd: str,
    pid: int,
    *,
    workspace_root: str,
    log_path: str | None = None,
    declared_ports: Sequence[int] = (),
    ready_url: str | None = None,
    detached: bool,
) -> ServiceRecord:
    _validate_name(name)
    return ServiceRecord(
        name=name,
        argv=argv,
        cwd=cwd,
        pid=pid,
        process_start_time=process_start_time(pid),
        started_at=datetime.now(timezone.utc).isoformat(),
        workspace_root=workspace_root,
        log_path=log_path,
        declared_ports=_normalise_ports(declared_ports),
        ready_url=ready_url,
        status="running",
        detached=detached,
    )


def _ensure_name_available(
    registry: ServiceRegistry,
    name: str,
    workspace_root: str,
) -> None:
    existing = registry.get(
        name,
        workspace_root=workspace_root,
        refresh=False,
    )
    if existing is None:
        return
    if _managed_record_alive(existing):
        raise ServiceAlreadyRunning(
            f"service {name!r} is already running as PID {existing.pid} "
            f"in {workspace_root}"
        )
    registry.remove(
        name,
        workspace_root=workspace_root,
        expected_pid=existing.pid,
    )


def _default_log_path(registry: ServiceRegistry, name: str, workspace_root: str) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip(".-") or "service"
    workspace_id = hashlib.sha256(workspace_root.encode("utf-8")).hexdigest()[:12]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return registry.path.parent / "logs" / workspace_id / f"{safe_name}-{timestamp}.log"


def _normalise_scope(value: str | os.PathLike[str]) -> str:
    return os.fspath(Path(value).expanduser().resolve())


def _workspace_for(cwd: str, explicit: str | os.PathLike[str] | None) -> str:
    workspace = _normalise_scope(explicit if explicit is not None else cwd)
    try:
        Path(cwd).resolve().relative_to(Path(workspace))
    except ValueError as exc:
        raise ValueError("service cwd must be inside its workspace root") from exc
    return workspace


def _record_scope(record: ServiceRecord) -> str:
    return _normalise_scope(record.workspace_root or record.cwd)


def _record_key(record: ServiceRecord) -> tuple[str, str]:
    return _record_scope(record), record.name


def _managed_group_alive(record: ServiceRecord) -> bool:
    """Return whether a detached POSIX process group still has live members."""

    if not record.detached:
        return process_identity_matches(record.pid, record.process_start_time)
    if os.name != "posix":
        # Windows has no safe standard-library process-tree identity primitive.
        # Preserve the record after its leader exits so the name cannot be
        # reused and the tree cannot be silently forgotten.
        return record.process_start_time is not None
    if sys.platform.startswith("linux"):
        return _linux_process_group_alive(record.pid)
    # Without procfs we cannot safely prove that a group ID was not reused after
    # its leader exited.  Keep the record as an explicit unsafe/stale item rather
    # than deleting it or signalling a possibly unrelated group.
    return record.process_start_time is not None


def _managed_record_alive(record: ServiceRecord) -> bool:
    return process_identity_matches(record.pid, record.process_start_time)


def _record_or_group_alive(record: ServiceRecord) -> bool:
    """Keep orphaned detached groups registered instead of forgetting them."""

    return _managed_record_alive(record) or _managed_group_alive(record)


def _termination_target_alive(record: ServiceRecord) -> bool:
    """Track the whole group after its leader identity was verified once."""

    if record.detached and os.name == "posix":
        return _managed_group_alive(record)
    return process_identity_matches(record.pid, record.process_start_time)


def _linux_process_group_alive(group_id: int) -> bool:
    try:
        entries = os.scandir("/proc")
    except OSError:
        return False
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            stat = _read_linux_proc_stat(int(entry.name))
            if stat is not None and stat[0] != "Z" and stat[3] == group_id:
                return True
    return False


def _resolve_windows_command(command: list[str]) -> list[str]:
    """Resolve PATHEXT shims without ever passing a command through a shell."""

    if os.name != "nt":
        return command
    resolved = shutil.which(command[0])
    if resolved is None:
        return command
    suffix = Path(resolved).suffix.lower()
    if suffix in {".cmd", ".bat"}:  # pragma: no cover - exercised on Windows
        # Batch files inherently require cmd.exe, whose metacharacter parsing is
        # not equivalent to CreateProcess argv quoting.  Reconstructing a command
        # string would violate DevFreeze's no-shell boundary, so fail closed.
        raise ServiceError(
            f"refusing Windows batch command shim {resolved!r}; "
            "invoke the underlying executable/script directly"
        )
    return [resolved, *command[1:]]


@contextmanager
def _open_private_append(path: Path) -> Iterator[Any]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        os.chmod(path, 0o600)
        with os.fdopen(descriptor, "ab", buffering=0) as stream:
            descriptor = -1
            yield stream
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _ensure_private_directory(path: Path) -> None:
    try:
        if path.is_symlink():
            raise RegistryError(f"refusing symbolic-link DevFreeze directory: {path}")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        details = path.stat()
        if not Path(path).is_dir():
            raise RegistryError(f"DevFreeze path is not a directory: {path}")
        if hasattr(os, "getuid") and details.st_uid != os.getuid():
            raise RegistryError(
                f"DevFreeze directory is not owned by the current user: {path}"
            )
        os.chmod(path, 0o700)
    except RegistryError:
        raise
    except OSError as exc:
        raise RegistryError(f"cannot secure DevFreeze directory: {path}") from exc


@contextmanager
def _forward_signals(process: subprocess.Popen[Any]) -> Iterator[None]:
    watched = [signal.SIGINT, signal.SIGTERM]
    previous: dict[signal.Signals, Any] = {}

    def forward(received: int, _frame: Any) -> None:
        if process.poll() is None:
            try:
                if os.name == "nt" and received == signal.SIGINT:
                    process.send_signal(
                        getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM)
                    )
                else:
                    process.send_signal(received)
            except (ProcessLookupError, ValueError, OSError):
                pass

    # Python only permits signal-handler installation in the main thread.
    if threading.current_thread() is threading.main_thread():
        for watched_signal in watched:
            previous[watched_signal] = signal.getsignal(watched_signal)
            signal.signal(watched_signal, forward)
    try:
        yield
    finally:
        for watched_signal, handler in previous.items():
            signal.signal(watched_signal, handler)


def _signal_managed_process(
    record: ServiceRecord,
    sent_signal: int,
    *,
    trusted_group: bool = False,
) -> None:
    if not trusted_group and not process_identity_matches(
        record.pid,
        record.process_start_time,
    ):
        raise UnsafeProcessError(
            f"refusing to signal PID {record.pid}: process identity changed"
        )
    try:
        if record.detached and os.name == "posix":
            # Detached services are session/group leaders created by this module.
            os.killpg(record.pid, sent_signal)
        else:
            os.kill(record.pid, sent_signal)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        raise UnsafeProcessError(
            f"permission denied while signalling managed PID {record.pid}"
        ) from exc


def _cleanup_failed_launch(
    process: subprocess.Popen[Any],
    record: ServiceRecord,
) -> None:
    """Best-effort TERM/KILL of a group that never reached the registry."""

    def signal_group(sent_signal: int) -> None:
        try:
            if record.detached and os.name == "posix":
                os.killpg(record.pid, sent_signal)
            elif process.poll() is None:
                process.send_signal(sent_signal)
        except (ProcessLookupError, PermissionError):
            pass

    signal_group(signal.SIGTERM)
    deadline = time.monotonic() + 0.25
    while time.monotonic() < deadline and _launch_group_alive(record):
        time.sleep(0.02)
    if _launch_group_alive(record):
        signal_group(getattr(signal, "SIGKILL", signal.SIGTERM))
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and _launch_group_alive(record):
            time.sleep(0.02)
    try:
        process.wait(timeout=0.1)
    except subprocess.TimeoutExpired:
        _reap_in_background(process)


def _launch_group_alive(record: ServiceRecord) -> bool:
    if record.detached and os.name == "posix":
        if sys.platform.startswith("linux"):
            return _linux_process_group_alive(record.pid)
        try:
            os.killpg(record.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    return is_pid_alive(record.pid)


def _lock_descriptor(descriptor: int) -> None:
    if os.name == "posix":
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
            os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)


def _unlock_descriptor(descriptor: int) -> None:
    if os.name == "posix":
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)


def _reap_in_background(process: subprocess.Popen[Any]) -> None:
    """Retain and reap a detached child without blocking the caller."""

    threading.Thread(
        target=process.wait,
        name=f"devfreeze-reap-{process.pid}",
        daemon=True,
    ).start()


def _fsync_directory(directory: Path) -> None:
    if os.name != "posix":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


__all__ = [
    "RegistryError",
    "ServiceAlreadyRunning",
    "ServiceError",
    "ServiceRecord",
    "ServiceRegistry",
    "UnsafeProcessError",
    "default_registry_path",
    "descendant_pids",
    "detect_listening_ports",
    "is_pid_alive",
    "is_process_alive",
    "process_identity_matches",
    "process_start_time",
    "run_detached",
    "run_foreground",
    "service_record_is_running",
]
