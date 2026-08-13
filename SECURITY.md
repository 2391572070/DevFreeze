# Security policy

## Supported versions

DevFreeze is currently pre-1.0. Security fixes are provided on the latest
released version only. Upgrade to the newest release before reporting an issue
that may already have been fixed.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use GitHub's
private vulnerability-reporting feature for this repository when available.
If private reporting is not configured, contact the maintainers through a
private channel listed on the repository owner profile and include
"DevFreeze security" in the subject.

Include, where possible:

- the affected DevFreeze version and operating system;
- a minimal reproduction that does not contain real credentials or private
  source code;
- the impact and the trust boundary that is crossed;
- any suggested mitigation;
- whether you believe active exploitation is occurring.

Maintainers should acknowledge a complete report within seven days and keep the
reporter informed while validating and preparing a fix. These are targets, not
a promise of a particular disclosure date.

## Security properties

DevFreeze is designed so that:

- snapshots do not contain environment-variable values;
- snapshots do not contain uncommitted file contents or patch data;
- managed commands are executed as argument arrays with `shell=False`;
- recovery is preview-only until `--execute` is explicitly supplied;
- Git drift must be acknowledged before services are started;
- recovery never checks out or resets Git, installs dependencies, or replaces
  workspace files;
- a detached managed process must have a process-start identity; PID-only
  registry entries are never signalled;
- registry and managed-log files are user-private, and concurrent registry
  changes are serialized across CLI processes.

Violations of these properties are security bugs.

## Threat model and limitations

Snapshot JSON and `.devfreeze.toml` are data until the user chooses to execute a
recovery plan. A maliciously modified command can run with the current user's
permissions after approval. Treat snapshots from other people like scripts:
inspect commands and paths before using `thaw --execute`.

DevFreeze does not:

- sandbox a managed program;
- encrypt snapshot, registry, or log files;
- sign snapshots or prove who created them;
- hide paths, branch names, changed-file names, repository locations, commands,
  readiness URLs, or user-authored notes;
- prevent a child program from reading the environment it normally inherits;
- act as a backup of uncommitted work.

Git remote capture removes URL user information, query, and fragment data, but
other metadata can still be sensitive. Review files before sharing them and
protect the DevFreeze data directory with normal account-level permissions.

Detached service logs contain the child program's own output. That output may
contain secrets even though DevFreeze does not capture environment-variable
values. Handle logs according to the child program's security requirements.

## Safe usage

- Keep the data directory local and out of public repositories.
- Do not put tokens, passwords, or private data in snapshot notes, commands, or
  readiness URLs.
- Review recovery plans after pulling changes to `.devfreeze.toml`.
- Use `stop --force` only after checking the service identity and normal stop
  failure; it allows a kill signal after the timeout.
- Use operating-system isolation when running an untrusted development service.

For the implementation boundaries, see
[`docs/architecture.md`](docs/architecture.md).
