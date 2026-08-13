"""Read-only Git worktree metadata capture."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
from urllib.parse import SplitResult, urlsplit, urlunsplit

from .errors import GitError
from .executables import trusted_which
from .models import GitState


_TIMEOUT_SECONDS = 10


def _run_git(path: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    executable = trusted_which("git")
    if executable is None:
        raise GitError("git executable was not found on an absolute PATH entry")
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["LC_ALL"] = "C"
    try:
        completed = subprocess.run(
            [
                executable,
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-C",
                os.fspath(path),
                *arguments,
            ],
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_TIMEOUT_SECONDS,
            check=False,
            env=environment,
        )
    except FileNotFoundError as exc:
        raise GitError("git executable was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError("git command timed out") from exc
    except OSError as exc:
        raise GitError(f"cannot run git: {exc}") from exc
    if check and completed.returncode != 0:
        message = completed.stderr.decode("utf-8", "replace").strip()
        raise GitError(message or f"git exited with status {completed.returncode}")
    return completed


def _text(output: bytes) -> str:
    return output.decode("utf-8", "replace").strip()


def find_git_root(path: str | os.PathLike[str]) -> Path | None:
    """Return the enclosing worktree root, or ``None`` outside a repository."""

    candidate = Path(path).expanduser().resolve()
    if candidate.is_file():
        candidate = candidate.parent
    if trusted_which("git") is None:
        return None
    completed = _run_git(candidate, "rev-parse", "--show-toplevel", check=False)
    if completed.returncode != 0:
        return None
    root = _text(completed.stdout)
    if not root:
        return None
    return Path(root).resolve()


def sanitise_remote(remote: str) -> str:
    """Remove common credential-bearing components from a Git remote URL."""

    remote = remote.strip()
    if not remote:
        return remote
    if "://" in remote:
        try:
            parsed = urlsplit(remote)
            hostname = parsed.hostname
            if hostname is None:
                return "<redacted-invalid-remote>"
            host = f"[{hostname}]" if ":" in hostname else hostname
            if parsed.port is not None:
                host += f":{parsed.port}"
            cleaned = SplitResult(parsed.scheme, host, parsed.path, "", "")
            return urlunsplit(cleaned)
        except ValueError:
            return "<redacted-invalid-remote>"
    # scp-like remotes are commonly user@host:path.  Strip the user portion;
    # local filesystem paths containing @ are left untouched unless the suffix
    # has the characteristic host:path shape.
    match = re.fullmatch(r"[^/@:\s]+@([^/:\s]+:.+)", remote)
    if match:
        return match.group(1)
    # Query/fragment components on nonstandard remote syntaxes are not needed
    # for identity comparison and can contain access tokens.
    return remote.split("?", 1)[0].split("#", 1)[0]


def _portable_path(raw: bytes) -> str:
    text = raw.decode("utf-8", "backslashreplace")
    pieces: list[str] = []
    for character in text:
        codepoint = ord(character)
        if codepoint < 32 or codepoint == 127:
            escaped = {"\n": r"\n", "\r": r"\r", "\t": r"\t"}.get(character)
            pieces.append(escaped if escaped is not None else f"\\x{codepoint:02x}")
        else:
            pieces.append(character)
    return "".join(pieces)


def _changed_files(output: bytes) -> tuple[str, ...]:
    records = output.split(b"\0")
    if records and records[-1] == b"":
        records.pop()
    result: list[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        if len(record) < 4 or record[2:3] != b" ":
            raise GitError("git produced an invalid porcelain status record")
        status = record[:2]
        path = _portable_path(record[3:])
        if path not in result:
            result.append(path)
        if b"R" in status or b"C" in status:
            index += 1
            if index >= len(records):
                raise GitError("git produced an incomplete rename status record")
            original = _portable_path(records[index])
            if original not in result:
                result.append(original)
        index += 1
    return tuple(sorted(result))


def capture_git_state(path: str | os.PathLike[str]) -> GitState | None:
    """Capture branch, HEAD and filename-only worktree status.

    This function never reads changed file contents.  All subprocess calls use
    explicit argument vectors with ``shell=False``.  An unborn repository is a
    valid state and is represented with ``head='unborn'``.
    """

    root = find_git_root(path)
    if root is None:
        return None

    branch_result = _run_git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    branch = _text(branch_result.stdout) if branch_result.returncode == 0 else None

    head_result = _run_git(root, "rev-parse", "--verify", "HEAD", check=False)
    if head_result.returncode == 0:
        head = _text(head_result.stdout)
    else:
        # symbolic-ref succeeds for the initial branch before the first commit.
        # A failure in a valid worktree is therefore stable, explicit metadata,
        # not a reason to make `freeze` unusable.
        head = "unborn"

    remote_result = _run_git(root, "remote", "get-url", "origin", check=False)
    remote = sanitise_remote(_text(remote_result.stdout)) if remote_result.returncode == 0 else None
    if remote == "":
        remote = None

    status_result = _run_git(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    changed_files = _changed_files(status_result.stdout)
    return GitState(
        remote=remote,
        branch=branch,
        head=head,
        dirty=bool(changed_files),
        changed_files=changed_files,
    )
