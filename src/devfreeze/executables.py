"""Conservative executable lookup for read-only metadata capture."""

from __future__ import annotations

import os
from pathlib import Path


def trusted_which(name: str) -> str | None:
    """Resolve a bare executable from explicit absolute PATH entries only.

    In particular, this never searches the current working directory.  That
    matters on Windows, where normal executable lookup can otherwise run a
    repository-local ``git.exe`` merely while capturing metadata.
    """

    if not name or Path(name).name != name or any(separator in name for separator in ("/", "\\")):
        return None
    suffixes = ("",)
    current = Path.cwd().resolve()
    project_root = current
    for candidate_root in (current, *current.parents):
        if (candidate_root / ".git").exists() or (
            candidate_root / ".devfreeze.toml"
        ).exists():
            project_root = candidate_root.resolve()
            break
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        configured = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
        suffixes = tuple(
            suffix.lower()
            for suffix in configured.split(os.pathsep)
            if suffix and suffix.lower() not in {".bat", ".cmd"}
        )
        if Path(name).suffix:
            suffixes = ("",)
    for raw_directory in os.environ.get("PATH", os.defpath).split(os.pathsep):
        if not raw_directory:
            continue
        directory = Path(raw_directory).expanduser()
        if not directory.is_absolute():
            continue
        try:
            resolved_directory = directory.resolve()
            resolved_directory.relative_to(project_root)
        except (OSError, ValueError):
            pass
        else:
            # Do not execute repository-controlled binaries during metadata
            # capture, even if a project prepended an absolute local bin path.
            continue
        for suffix in suffixes:
            candidate = resolved_directory / f"{name}{suffix}"
            try:
                if not candidate.is_file():
                    continue
                if os.name == "nt" or os.access(candidate, os.X_OK):
                    return os.fspath(candidate.resolve())
            except OSError:
                continue
    return None


__all__ = ["trusted_which"]
