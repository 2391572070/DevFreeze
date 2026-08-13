"""Safe, bounded platform and development-tool version capture."""

from __future__ import annotations

from collections.abc import Sequence
import platform as platform_module
import subprocess
import sys

from .errors import ValidationError
from .executables import trusted_which
from .models import PlatformState, ToolInfo, validate_name


_TOOL_COMMANDS: dict[str, tuple[str, ...]] = {
    "git": ("git", "--version"),
    "go": ("go", "version"),
    "node": ("node", "--version"),
    "npm": ("npm", "--version"),
    "python": (sys.executable, "--version"),
    "rustc": ("rustc", "--version"),
}


def capture_platform() -> PlatformState:
    """Capture non-secret operating-system and Python metadata."""

    return PlatformState(
        system=platform_module.system() or "unknown",
        release=platform_module.release() or "unknown",
        machine=platform_module.machine() or "unknown",
        python=platform_module.python_version(),
    )


def _version(command: tuple[str, ...]) -> str | None:
    executable = command[0]
    resolved = executable if executable == sys.executable else trusted_which(executable)
    if resolved is None:
        return None
    invocation = [resolved, *command[1:]]
    if sys.platform.startswith("win") and str(resolved).lower().endswith((".cmd", ".bat")):
        # Tool capture remains shell-free; batch shims are omitted rather than
        # reconstructed as an injectable cmd.exe command line.
        return None
    try:
        completed = subprocess.run(
            invocation,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = completed.stdout[:4096].strip()
    if completed.returncode != 0 or not output:
        return None
    return output.splitlines()[0].strip() or None


def capture_tooling(names: Sequence[str] | None = None) -> tuple[ToolInfo, ...]:
    """Capture versions of a small allowlist of tools.

    Unknown tool names are rejected instead of being executed.  This preserves
    the invariant that snapshot capture never evaluates user-provided commands.
    Missing tools are omitted from the returned tuple.
    """

    selected = tuple(_TOOL_COMMANDS) if names is None else tuple(names)
    if len(set(selected)) != len(selected):
        raise ValidationError("tool names must not contain duplicates")
    result: list[ToolInfo] = []
    for name in selected:
        validated = validate_name(name, field="tool name")
        command = _TOOL_COMMANDS.get(validated)
        if command is None:
            raise ValidationError(f"unsupported tool name: {validated}")
        version = _version(command)
        if version is not None:
            result.append(ToolInfo(validated, version))
    return tuple(result)
