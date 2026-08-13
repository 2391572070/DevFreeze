# DevFreeze

[![CI](https://github.com/2391572070/DevFreeze/actions/workflows/ci.yml/badge.svg)](https://github.com/2391572070/DevFreeze/actions/workflows/ci.yml)

**Save the context of a local development task, then resume it through a plan you review.**

DevFreeze is a local-first, zero-runtime-dependency CLI for taking metadata-only
snapshots of a development workspace. A snapshot records where you were, the
Git state you saw, relevant tool versions, an optional hand-off note, and
services explicitly configured or started through DevFreeze.

It does **not** save environment-variable values or the contents of uncommitted
files. Resuming never checks out a branch, resets Git, installs dependencies, or
overwrites source code.

[简体中文说明](docs/README.zh-CN.md) · [Roadmap](docs/ROADMAP.md) ·
[Architecture](docs/architecture.md) · [Security](SECURITY.md) ·
[Contributing](CONTRIBUTING.md)

> DevFreeze is alpha software. Review snapshots and recovery plans before using
> them around sensitive projects.

## Why

Returning to a task after a few days often means reconstructing several small
facts: the directory and branch, which files were dirty, which runtime was in
use, what local services mattered, and what you intended to do next. DevFreeze
keeps that metadata together without copying your worktree.

## Requirements and installation

- Python 3.11 or newer
- Git is recommended for repository snapshots

Install the current checkout in an isolated environment:

```console
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
devfreeze doctor
```

DevFreeze has no third-party runtime dependencies. The build step uses
setuptools.

## Quick start

Capture the current workspace:

```console
devfreeze freeze payment-bug -m "Reproduced the timeout; inspect retry.py next"
devfreeze list
devfreeze show payment-bug
```

Compare the snapshot with the current machine and preview recovery:

```console
devfreeze diff payment-bug
devfreeze thaw payment-bug
```

`thaw` is preview-only by default. After reviewing the plan, explicitly allow
DevFreeze to start missing captured services:

```console
devfreeze thaw payment-bug --execute
```

If the branch, HEAD, or dirty-file list has drifted, execution requires an
additional acknowledgement:

```console
devfreeze thaw payment-bug --execute --force
```

`--force` acknowledges the current Git state. It still does not modify Git or
files. Interactive execution asks for confirmation; `-y` skips that prompt.

## Track local services

Run a service in the foreground and track the exact argument vector:

```console
devfreeze run --name web --port 3000 --ready-url http://localhost:3000/health -- npm run dev
```

Or detach it:

```console
devfreeze run --name worker --detach -- python -m app.worker
devfreeze services
devfreeze stop worker
```

The separator `--` makes the managed command unambiguous. Commands are stored
and executed as argument arrays (`shell=False`); shell expressions such as
`cmd1 && cmd2` are not interpreted.

Service names are scoped to the nearest project root, so separate projects may
both run a service named `web`. Run `devfreeze stop NAME` from the corresponding
project. If its configuration moved or disappeared, use
`devfreeze stop NAME --workspace /original/root`. Managed logs and registry
files are created with user-private permissions.

Port flags are repeatable. On Linux, DevFreeze can inspect `/proc` for listening
ports owned by a managed process and its descendants. On macOS and Windows it
safely degrades: declared ports remain available, but automatic discovery may
return no ports.

On Windows, invoke an executable or script runtime directly (for example,
`node path\\to\\script.js`). DevFreeze deliberately refuses `.cmd`/`.bat` shims
because they require `cmd.exe` string evaluation and would weaken the exact-
argument safety boundary.

## Project configuration

Commit a `.devfreeze.toml` at the project root to declare services that should
be included in snapshots:

```toml
version = 1

[[services]]
name = "web"
command = ["python", "-m", "app"]
cwd = "."
ports = [8000]
ready_url = "http://localhost:8000/health"

[[services]]
name = "worker"
command = ["python", "-m", "app.worker"]
```

Commands must be string arrays. A service `cwd` is relative to and contained by
the project root. Configuration is strict: unknown keys and invalid values are
rejected instead of guessed.

## Command reference

```text
devfreeze freeze NAME [-m NOTE] [--force] [--json]
devfreeze list [--json]
devfreeze show NAME [--json]
devfreeze diff NAME [--json]
devfreeze thaw NAME [--execute] [-y] [--force] [--json]
devfreeze run --name NAME [--cwd PATH] [--port N] [--ready-url URL]
              [--detach] -- COMMAND...
devfreeze services [--all] [--json]
devfreeze stop NAME [--workspace PATH] [--force]
devfreeze delete NAME [-y]
devfreeze doctor
devfreeze version
devfreeze --version
```

Use `devfreeze COMMAND --help` for option details. The global
`--data-dir PATH` option must precede the command.

## Storage and privacy

By default, snapshot JSON is stored under:

1. `$DEVFREEZE_HOME`, when set;
2. `$XDG_DATA_HOME/devfreeze`, when set;
3. `~/.local/share/devfreeze` otherwise.

`--data-dir PATH` overrides that location for one invocation. Snapshot files
are named `<name>.json`; runtime service state lives under `runtime/` so it
cannot collide with a snapshot named `services`.

Snapshots contain metadata and are intentionally human-readable. They can
still reveal paths, remote repository locations, branch names, changed-file
names, commands, URLs, and notes. Inspect JSON before sharing it. See
[SECURITY.md](SECURITY.md) for the threat model and reporting process, and
[the snapshot schema](docs/snapshot.schema.json) for the on-disk contract.

## Safety model

- No environment-variable values are captured in snapshots.
- No uncommitted file contents or patch data are captured.
- Managed commands are argument arrays and are never evaluated by a shell.
- `thaw` only previews unless `--execute` is supplied.
- Execution starts missing, previously captured services only.
- Git drift requires `--force`, which is acknowledgement—not mutation.
- DevFreeze never performs `checkout`, `reset`, dependency installation, or
  source-file replacement during recovery.

## Development

```console
make test
make check
```

Tests use Python's standard-library `unittest`. Contributions are welcome; read
[the roadmap](docs/ROADMAP.md) and [CONTRIBUTING.md](CONTRIBUTING.md) before
opening a change. Submit public bugs and feature proposals through the
[issue chooser](https://github.com/2391572070/DevFreeze/issues/new/choose), but
report suspected vulnerabilities privately through [SECURITY.md](SECURITY.md).

## License

DevFreeze is available under the [MIT License](LICENSE).
