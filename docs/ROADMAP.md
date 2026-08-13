# DevFreeze roadmap

This roadmap separates what is implemented from what is merely proposed. It is
not a promise of dates. Priorities describe sequencing: **P0** is required for
the milestone, **P1** is important, and **P2** is opportunistic. Each numbered
item is intended to be small enough to become an independent GitHub issue.

## v0.1: implemented MVP

The v0.1 implementation is complete and ready for its first public release. It
currently provides:

- metadata-only, versioned JSON snapshots with strict validation and atomic,
  user-private storage;
- Git, platform, allowlisted tool-version, note, and configured or managed
  service metadata capture;
- strict `.devfreeze.toml` service configuration;
- `freeze`, `list`, `show`, `diff`, and preview-first `thaw` workflows;
- Git drift detection and execution-time revalidation without checking out,
  resetting, installing dependencies, or changing source files;
- foreground and detached service management using exact argument arrays and
  `shell=False`;
- workspace-scoped service records, conservative process-identity checks,
  private logs, and cross-process registry locking;
- Linux-first automatic port discovery with conservative macOS and Windows
  fallback;
- standard-library tests, a three-OS CI matrix, bilingual user documentation,
  and public contribution and security policies.

The following boundaries are intentional and important when evaluating v0.1:

- `ready_url` is validated and recorded as metadata, but DevFreeze does **not**
  yet poll it or wait for a service to become ready.
- A multi-service `thaw --execute` is not transactional. If a later service
  fails to start, services started earlier by that invocation can remain
  running and registered.
- Snapshot paths are absolute. DevFreeze does not currently relocate a snapshot
  to another clone or machine, nor does it provide an import/export trust flow.
- Declared services have no dependency graph or readiness-gated ordering.

## v0.1 public-release checklist

- [x] Add the MIT license, security policy, contribution guide, code of conduct,
  changelog, CI workflow, issue forms, and pull request template.
- [x] Document the v0.1 safety contract and current product boundaries.
- [x] Publish the repository under the `main` branch and run the full GitHub
  Actions matrix on Linux, macOS, and Windows with Python 3.11–3.13
  ([verified run](https://github.com/2391572070/DevFreeze/actions/runs/31664838053)).
- [x] Build both wheel and source-distribution artifacts, then verify a clean
  environment can install them and run `devfreeze doctor`.
- [x] Tag `v0.1.0` and publish release notes derived from `CHANGELOG.md`.
- [ ] Enable GitHub private vulnerability reporting and verify the public issue
  chooser sends security reports to `SECURITY.md`.
- [ ] Add a short, reproducible demo of `freeze` → drift → `thaw` preview →
  service resume without using private source code or credentials.

## v0.2: reliable recovery and everyday usability

### P0 — milestone requirements

1. **Wait for local service readiness with a bounded timeout.** Poll a recorded
   `ready_url`, expose timeout and cancellation behavior, and produce useful
   diagnostics. The first design should accept loopback targets only; broader
   network access requires an explicit opt-in and an SSRF-focused review.
2. **Roll back services started by a failed thaw.** If service N fails, stop only
   services 1…N-1 that the same invocation started. Never stop a service that
   was already running, and leave the registry consistent after rollback.
3. **Exercise real managed-service lifecycles on every supported OS.** Cover
   detached launch, process identity, normal and forced stop, stale cleanup, and
   log handling in Linux, macOS, and Windows CI.

### P1 — important candidates

4. **Add `devfreeze logs NAME [--follow]`.** Resolve names within a workspace,
   support bounded tailing and follow mode, and preserve private-file handling.
5. **Add `devfreeze init --print` and an explicit `--write`.** Suggest a minimal
   configuration for common Python, Node.js, Go, and Rust projects. Detection
   must be side-effect-free, and source-tree writes must never be implicit.
6. **Add service dependencies and deterministic start ordering.** Introduce a
   reviewed configuration-format change with cycle detection, topological
   ordering, and readiness-gated downstream startup.
7. **Add `doctor --json` and a redacted support bundle.** Define stable machine
   output and make redaction the default for home paths, remotes, command
   arguments, URLs, notes, and child-process logs.

### P2 — opportunistic

8. **Add snapshot retention and pruning controls.** Preview every deletion,
   support name and age filters, require explicit approval, and never alter
   managed-service state.

## v0.3: portable collaboration and integrations

### P0 — milestone requirements

1. **Introduce explicit snapshot-schema migrations.** Keep old snapshots
   readable, make migrations inspectable, test each supported upgrade path, and
   continue to reject unknown future versions.
2. **Export and import redacted, non-executable snapshot bundles.** Report which
   fields were removed, validate the complete bundle, and require a separate,
   explicit trust action before imported commands can ever run.
3. **Rebase a snapshot onto a moved or freshly cloned repository.** Show an
   old-path → new-path mapping, verify repository identity using available Git
   metadata, and never mutate Git or source files as part of relocation.

### P1 — important candidates

4. **Optionally sign and verify snapshots.** Clearly distinguish integrity from
   author identity. Failed verification should block execution while preserving
   safe, read-only inspection.
5. **Add explicit Docker Compose and devcontainer adapters.** Translate adapter
   actions into reviewable plans and exact argument arrays; never evaluate shell
   strings or install dependencies implicitly.

### P2 — opportunistic

6. **Improve distribution and shell integration.** Add generated completions and
   document reproducible installation with `pipx`, `uv`, and Homebrew without
   adding runtime dependencies to the core package.

## Non-goals

Unless this roadmap is explicitly revised, DevFreeze will not:

- become a backup tool or capture uncommitted file contents, patches, secrets,
  or environment-variable values;
- provide byte-for-byte VM, container, or machine reproduction;
- automatically checkout or reset Git, install dependencies, or overwrite
  workspace source during recovery;
- evaluate user commands as shell strings;
- claim to sandbox the programs it starts;
- make snapshots public, synchronize them to a cloud service, or infer trust in
  snapshots received from other people;
- treat a readiness URL, process ID, or matching path as proof that a service or
  snapshot is trustworthy.

Feature proposals are welcome through the repository's
[issue chooser](https://github.com/2391572070/DevFreeze/issues/new/choose).
Suspected vulnerabilities must be reported privately as described in
[`SECURITY.md`](../SECURITY.md), not through a public issue.
