"""Shared exceptions for DevFreeze's public API."""

from __future__ import annotations


class DevFreezeError(Exception):
    """Base class for expected, user-actionable DevFreeze failures."""


class ValidationError(DevFreezeError, ValueError):
    """Raised when a snapshot or one of its fields is invalid."""


class StorageError(DevFreezeError):
    """Raised when snapshot storage cannot be read or updated safely."""


class SnapshotNotFoundError(StorageError, FileNotFoundError):
    """Raised when a requested snapshot does not exist."""


class SnapshotExistsError(StorageError, FileExistsError):
    """Raised when saving would replace a snapshot without permission."""


class GitError(DevFreezeError):
    """Raised when Git state cannot be captured reliably."""


class ConfigError(DevFreezeError, ValueError):
    """Raised when ``.devfreeze.toml`` is malformed or unsafe."""
