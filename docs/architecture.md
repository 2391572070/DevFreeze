# DevFreeze architecture

## Goals

DevFreeze records enough local development metadata to help a developer return
to a task, while keeping recovery narrow, inspectable, and reversible. Its core
design constraints are:

1. local-first storage;
2. metadata-only snapshots;
3. strict parsing at persistence boundaries;
4. preview before execution;
5. no Git or source-tree mutation during recovery;
6. no shell evaluation of managed commands;
7. Python 3.11+ with no third-party runtime dependencies.

DevFreeze is not a VM checkpoint, a container image, a secret manager, or a
backup system. It does not promise byte-for-byte reproduction of a machine.

## Components

```text
CLI
 ├─ capture/recovery ── Git, platform, tooling, project config
 │        │
 │        └──────────── strict snapshot model ── atomic JSON store
 │
 └─ managed services ─ process runner, registry, port discovery, stop safety
```

### CLI

`devfreeze.cli` owns argument parsing, human/JSON presentation, confirmation,
and exit status. It keeps policy visible: `thaw` creates a plan by default and
requires `--execute` before any service is started.

### Capture and recovery

Capture selects the nearest `.devfreeze.toml` directory as the workspace root.
If there is no configuration, it uses the enclosing Git root, then the current
directory as a fallback. This lets packages inside a monorepo have independent
snapshots and services. Capture gathers metadata from Git, the platform,
installed tool executables, project configuration, and the managed-service
registry.

Recovery is deliberately split into two stages:

1. `build_recovery_plan` compares the saved snapshot with current state and
   produces human-readable steps and drift records without changing anything.
2. `execute_recovery_plan` may start only missing services after blockers,
   drift acknowledgement, and user confirmation have been handled.

The executor cannot checkout or reset Git, install dependencies, or write into
the workspace. `--force` only acknowledges Git drift.

### Validated snapshot model

The persisted format is JSON with `schema_version = 1`. The model rejects
missing or unknown fields, duplicate object keys, unsupported versions, unsafe
names, invalid paths, duplicate ports, and malformed values. Snapshot and
service names are portable filenames:

```text
^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$
```

Windows device names are also rejected on every platform. The normative field
shape is documented in [`snapshot.schema.json`](snapshot.schema.json); runtime
validation additionally enforces cross-field containment, such as service
directories remaining under the workspace root.

Writes use a private temporary file followed by an atomic publish in the
destination directory, so readers do not observe a partial document. Creating
a snapshot without `--force` uses a no-clobber publish, including when multiple
CLI processes race to create the same name.

### Project configuration

`.devfreeze.toml` is parsed with Python's standard-library TOML parser. The
current configuration version is `1` and the accepted top-level shape is:

```toml
version = 1

[[services]]
name = "web"
command = ["python", "-m", "app"]
cwd = "."
ports = [8000]
ready_url = "http://localhost:8000/health"
```

Only `name`, `command`, `cwd`, `ports`, and `ready_url` are accepted for a
service. `cwd` is resolved under the project root. Unknown keys are errors.

### Managed services

Managed commands remain argument arrays from capture to execution and are
passed to the operating system with `shell=False`. Foreground mode propagates
the process exit status. Detached mode creates a new session and combines
standard output and error in a local log file.

Windows batch shims (`.cmd` and `.bat`) are rejected because Windows can only
run them through `cmd.exe` string evaluation. Users can invoke the underlying
runtime or executable directly without weakening argument-boundary guarantees.

The runtime registry keys services by workspace and name, uses an operating-
system file lock for cross-process updates, and records a PID plus process-start
identity. Before stopping a service, DevFreeze checks that identity again to
reduce the risk of signalling an unrelated process after PID reuse. If an
identity cannot be established, detached management fails closed.
The normal stop path sends termination and waits; a still-running process is
left registered unless the caller explicitly uses `stop --force`, which allows
a kill signal after the timeout.

Service scope is persisted rather than inferred only from the current checkout.
If a project configuration later moves or disappears, `stop` can fall back to a
globally unique record or accept `--workspace PATH` for an explicit selection.

On Linux, port discovery walks the managed process tree, maps socket file
descriptors to `/proc/net/tcp*`, and reports listening TCP ports. On macOS and
Windows, discovery uses `lsof` when it is available and otherwise returns no
automatically detected ports. Declared ports do not depend on discovery.

## On-disk layout

The data root is selected in this order:

1. the CLI's global `--data-dir` override;
2. `$DEVFREEZE_HOME`;
3. `$XDG_DATA_HOME/devfreeze`;
4. `~/.local/share/devfreeze`.

```text
<data-root>/
├── <snapshot-name>.json
└── runtime/
    ├── services.json
    ├── .services.json.lock
    └── logs/
        └── <workspace-id>/...
```

Snapshot, registry, lock, and managed-log files are created as user-private
files; DevFreeze-owned runtime and log directories are restricted to the user.

The runtime namespace prevents service bookkeeping from colliding with a valid
snapshot name. Data stays local unless the user deliberately copies or shares
it. Files are not encrypted by DevFreeze.

## Snapshot contents

A version 1 snapshot contains:

- `workspace`: absolute root, capture directory, and optional workspace file;
- `git`: sanitized remote, branch, object ID, dirty flag, and changed-file names;
- `platform`: operating system, release, machine, and Python metadata;
- `tooling`: detected tool names and version strings;
- `services`: name, exact argument array, directory, ports, readiness URL, state;
- `note`: user-authored hand-off text.

Git remote capture strips URL user information, query, and fragment data. The
snapshot still contains potentially sensitive metadata such as local paths,
repository locations, filenames, commands, URLs, and notes. It never contains
environment-variable values, uncommitted file contents, or patches by design.

## Trust boundaries

Both snapshot JSON and `.devfreeze.toml` are local inputs, not trusted code. A
modified file can change a service command that will run after explicit
recovery approval. Files should therefore be reviewed before `--execute`, and
snapshots from other people should be treated like scripts.

DevFreeze does not cryptographically sign snapshots or attest to command
provenance. It also does not sandbox the programs it starts: those programs run
with the invoking user's permissions and inherit the runtime environment
provided by the caller.

See [`SECURITY.md`](../SECURITY.md) for the public security policy.

## Compatibility strategy

`schema_version` is independent of the CLI release number. Readers reject
unsupported schema versions instead of silently guessing. A future format
change that alters persisted meaning must introduce an explicit migration path
or a new schema version.

Cross-platform capture is conservative. Features with no reliable platform
implementation return less metadata rather than broadening permissions or
guessing state. Linux currently has the strongest automatic port discovery;
macOS and Windows preserve declared configuration and safely degrade.
