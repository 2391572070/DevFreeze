# Changelog

All notable changes to DevFreeze will be documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project intends to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-08-13

### Added

- Initial project packaging, documentation, CI, and community files.
- Metadata-only JSON snapshots and strict `.devfreeze.toml` configuration.
- Safe recovery planning with explicit service-start approval.
- Managed foreground and detached services with conservative process identity
  checks and Linux-first port discovery.
- Workspace-scoped, cross-process-safe service registration and orphan-group
  cleanup, with private snapshots, registries, locks, and logs.
- Atomic no-clobber snapshot publication and execution-time drift revalidation.

### Fixed

- Canonicalized project and service paths across filesystem aliases such as
  macOS `/var` and `/private/var`.
- Hardened managed-process shutdown so detached POSIX groups are tracked
  without signalling an identity that can no longer be proved.
- Replaced Windows shell-based process inspection with 64-bit-safe Win32
  creation-time and liveness checks that preserve existing registry identities.
- Made the complete test suite portable across Linux, macOS, and Windows.

[Unreleased]: https://github.com/2391572070/DevFreeze/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/2391572070/DevFreeze/releases/tag/v0.1.0
