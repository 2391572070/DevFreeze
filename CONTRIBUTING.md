# Contributing to DevFreeze

Thank you for helping make returning to development tasks safer and easier.

## Before opening a change

- Review the [roadmap](docs/ROADMAP.md) for current boundaries and proposed
  milestones.
- Search existing issues and pull requests.
- Use the repository's [issue chooser](https://github.com/2391572070/DevFreeze/issues/new/choose)
  for public bug reports and feature proposals; blank public issues are disabled.
- For behavior or format changes, open an issue first so the safety and
  compatibility impact can be discussed.
- Keep changes focused. Avoid mixing refactors with feature work.
- Never disclose a suspected vulnerability in a public issue or uncoordinated
  pull request. Follow [SECURITY.md](SECURITY.md) for private reporting.

Participation is governed by our [Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

DevFreeze requires Python 3.11 or newer and has no third-party runtime
dependencies.

```console
git clone YOUR-FORK-URL
cd devfreeze
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

On Windows, activate the environment with the command appropriate for your
shell. You may also use:

```console
make check
```

## Design rules

Changes must preserve the project's core security contract unless an explicitly
reviewed design replaces it:

- do not capture environment-variable values;
- do not capture uncommitted file contents or patches;
- represent commands as argument arrays and use `shell=False`;
- keep `thaw` preview-only by default;
- do not let recovery checkout/reset Git, install dependencies, or overwrite
  workspace files;
- require explicit human approval before starting captured commands;
- prefer safe platform degradation to permission expansion or guessing.

Avoid adding a runtime dependency when the standard library can provide a small,
auditable implementation. A proposed dependency needs a clear maintenance and
security rationale.

## Tests

Use standard-library `unittest`. Add regression tests for changes in behavior,
especially around validation, path containment, process identity, command
execution, persistence, or recovery drift.

Tests must not:

- signal an unrelated system process;
- depend on external network access;
- read real credentials or environment-variable values into snapshots;
- leave background services or files outside their temporary directory.

Linux-specific `/proc` tests should be guarded so the suite safely runs on
macOS and Windows. Test declared-port behavior separately from automatic port
discovery.

## Documentation and persisted formats

Update the English README and the Chinese README when user-facing behavior
changes. If snapshot fields change, update:

- `docs/snapshot.schema.json`;
- `docs/architecture.md`;
- compatibility tests;
- `CHANGELOG.md`.

Do not silently reinterpret an existing `schema_version`. A persisted meaning
change needs an explicit migration or a new version.

## Pull requests

The repository's pull request template is required. A pull request should
include:

- a concise explanation of the problem and solution;
- tests that fail before and pass after the change;
- documentation for user-visible behavior;
- platform notes when behavior differs on Linux, macOS, or Windows;
- a statement about security and privacy impact.

Run `make check` before requesting review. Maintainers may ask for a smaller
change if the safety behavior cannot be reviewed independently.
