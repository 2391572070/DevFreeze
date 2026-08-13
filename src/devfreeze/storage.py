"""Local JSON snapshot storage with atomic publication and replacement."""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import stat
import tempfile

from .errors import (
    SnapshotExistsError,
    SnapshotNotFoundError,
    StorageError,
    ValidationError,
)
from .models import Snapshot, validate_name


def _environment_path(value: str, variable: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise StorageError(f"{variable} must be an absolute path")
    return path


def get_data_home(env: Mapping[str, str] | None = None) -> Path:
    """Return the directory containing snapshots.

    ``DEVFREEZE_HOME`` names that directory directly.  Otherwise the XDG data
    convention is used: ``$XDG_DATA_HOME/devfreeze`` or, as a fallback,
    ``~/.local/share/devfreeze``.
    """

    environment = os.environ if env is None else env
    explicit = environment.get("DEVFREEZE_HOME")
    if explicit:
        return _environment_path(explicit, "DEVFREEZE_HOME")
    xdg_home = environment.get("XDG_DATA_HOME")
    if xdg_home:
        return _environment_path(xdg_home, "XDG_DATA_HOME") / "devfreeze"
    return Path.home() / ".local" / "share" / "devfreeze"


def _fsync_directory(directory: Path) -> None:
    """Best-effort persistence for an atomic directory-entry update."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # Some platforms/filesystems do not permit fsync on directories.  The
        # file itself has already been flushed and safely replaced.
        pass
    finally:
        os.close(descriptor)


class SnapshotStore:
    """Store one validated JSON document per snapshot name."""

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        self.root = Path(root).expanduser() if root is not None else get_data_home()
        if not self.root.is_absolute():
            self.root = self.root.resolve()

    def _path(self, name: str) -> Path:
        safe_name = validate_name(name, field="snapshot name")
        path = self.root / f"{safe_name}.json"
        # validate_name is the primary defence; this invariant also makes
        # future changes to the filename format fail closed.
        try:
            path.relative_to(self.root)
        except ValueError as exc:  # pragma: no cover - defensive invariant
            raise StorageError("snapshot path escapes the data directory") from exc
        return path

    def ensure_private_root(self, *, create: bool) -> None:
        """Validate ownership and restrict the data root to the current user."""

        try:
            if self.root.is_symlink():
                raise StorageError(f"refusing symbolic-link data directory: {self.root}")
            if create:
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            elif not self.root.exists():
                return
            details = self.root.stat()
            if not stat.S_ISDIR(details.st_mode):
                raise StorageError(f"snapshot data path is not a directory: {self.root}")
            if hasattr(os, "getuid") and details.st_uid != os.getuid():
                raise StorageError(
                    f"snapshot data directory is not owned by the current user: {self.root}"
                )
            os.chmod(self.root, 0o700)
        except StorageError:
            raise
        except OSError as exc:
            raise StorageError(f"cannot secure snapshot data directory {self.root}: {exc}") from exc

    def save(self, snapshot: Snapshot, *, overwrite: bool = False) -> Path:
        """Atomically save ``snapshot`` and return its path.

        A non-overwriting save publishes the completed temporary file with a
        same-directory atomic no-clobber operation: a hard link normally, with
        Windows' non-replacing rename semantics as a fallback for volumes that
        do not support links.  Concurrent writers therefore cannot both claim a
        previously absent snapshot name.  Explicit overwrites continue to use
        atomic replacement.
        """

        if not isinstance(snapshot, Snapshot):
            raise ValidationError("save expects a Snapshot")
        destination = self._path(snapshot.name)
        try:
            self.ensure_private_root(create=True)
            if destination.exists() and not overwrite:
                raise SnapshotExistsError(f"snapshot already exists: {snapshot.name}")

            temporary_path: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.root,
                    prefix=f".{snapshot.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as temporary:
                    temporary_path = temporary.name
                    os.chmod(temporary.name, 0o600)
                    json.dump(
                        snapshot.to_dict(),
                        temporary,
                        ensure_ascii=False,
                        indent=2,
                        allow_nan=False,
                    )
                    temporary.write("\n")
                    temporary.flush()
                    os.fsync(temporary.fileno())

                if overwrite:
                    os.replace(temporary_path, destination)
                    temporary_path = None
                else:
                    # An exists()+replace sequence has an unavoidable race: two
                    # processes can both observe an absent destination and the
                    # later replace silently overwrites the winner.  A hard link
                    # publishes this already-flushed inode only if destination
                    # does not exist, with the filesystem arbitrating the race.
                    try:
                        os.link(temporary_path, destination)
                    except FileExistsError as exc:
                        raise SnapshotExistsError(
                            f"snapshot already exists: {snapshot.name}"
                        ) from exc
                    except OSError:
                        if os.name != "nt":
                            raise
                        # Hard links may be unavailable on FAT-family Windows
                        # volumes.  Windows rename is also atomic within this
                        # directory and, unlike POSIX rename, fails rather than
                        # replacing an existing destination.
                        try:
                            os.rename(temporary_path, destination)
                        except FileExistsError as exc:
                            raise SnapshotExistsError(
                                f"snapshot already exists: {snapshot.name}"
                            ) from exc
                        temporary_path = None
                    else:
                        os.unlink(temporary_path)
                        temporary_path = None
                _fsync_directory(self.root)
            finally:
                if temporary_path is not None:
                    try:
                        os.unlink(temporary_path)
                    except FileNotFoundError:
                        pass
        except (SnapshotExistsError, StorageError):
            raise
        except OSError as exc:
            raise StorageError(f"cannot save snapshot {snapshot.name!r}: {exc}") from exc
        return destination

    def load(self, name: str) -> Snapshot:
        """Load and strictly validate a snapshot by name."""

        path = self._path(name)
        self.ensure_private_root(create=False)
        try:
            # Refuse symlinks: a snapshot name should never redirect reads to
            # an arbitrary file outside the DevFreeze data directory.
            if path.is_symlink():
                raise StorageError(f"refusing symbolic-link snapshot: {name}")
            payload = path.read_bytes()
        except FileNotFoundError as exc:
            raise SnapshotNotFoundError(f"snapshot not found: {name}") from exc
        except StorageError:
            raise
        except OSError as exc:
            raise StorageError(f"cannot read snapshot {name!r}: {exc}") from exc
        try:
            snapshot = Snapshot.from_json(payload)
        except ValidationError as exc:
            raise StorageError(f"invalid snapshot file {path.name}: {exc}") from exc
        if snapshot.name != name:
            raise StorageError(
                f"snapshot filename/name mismatch: requested {name!r}, file contains {snapshot.name!r}"
            )
        return snapshot

    def list_names(self) -> list[str]:
        """Return valid stored names in lexical order without parsing files."""

        if not self.root.exists():
            return []
        self.ensure_private_root(create=False)
        result: list[str] = []
        try:
            candidates = self.root.iterdir()
            for path in candidates:
                if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                    continue
                name = path.stem
                try:
                    validate_name(name, field="snapshot name")
                except ValidationError:
                    continue
                result.append(name)
        except OSError as exc:
            raise StorageError(f"cannot list snapshots: {exc}") from exc
        return sorted(result)

    def list(self) -> list[Snapshot]:
        """Load every snapshot, sorted by name."""

        return [self.load(name) for name in self.list_names()]

    def delete(self, name: str, *, missing_ok: bool = False) -> bool:
        """Delete one snapshot.  Return ``True`` if a file was removed."""

        path = self._path(name)
        self.ensure_private_root(create=False)
        try:
            path.unlink()
        except FileNotFoundError as exc:
            if missing_ok:
                return False
            raise SnapshotNotFoundError(f"snapshot not found: {name}") from exc
        except OSError as exc:
            raise StorageError(f"cannot delete snapshot {name!r}: {exc}") from exc
        _fsync_directory(self.root)
        return True
