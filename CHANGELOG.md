# Changelog

All notable changes to DevFreeze will be documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project intends to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Initial project packaging, documentation, CI, and community files.
- Metadata-only JSON snapshots and strict `.devfreeze.toml` configuration.
- Safe recovery planning with explicit service-start approval.
- Managed foreground and detached services with conservative process identity
  checks and Linux-first port discovery.
- Workspace-scoped, cross-process-safe service registration and orphan-group
  cleanup, with private snapshots, registries, locks, and logs.
- Atomic no-clobber snapshot publication and execution-time drift revalidation.
