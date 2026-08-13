"""Snapshot capture, drift detection, and safe recovery planning.

The recovery layer is deliberately conservative.  It never mutates Git state and
only starts commands that were explicitly captured by DevFreeze.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from .config import ProjectConfig, find_project_root, load_config
from .gitstate import capture_git_state, find_git_root
from .models import ServiceState, Snapshot, WorkspaceState
from .services import (
    ServiceRecord,
    ServiceRegistry,
    run_detached,
    service_record_is_running,
)
from .tooling import capture_platform, capture_tooling


@dataclass(frozen=True)
class Drift:
    """One difference between a saved snapshot and the current machine."""

    level: str
    field: str
    saved: str
    current: str
    message: str


@dataclass
class RecoveryPlan:
    """A human-reviewable plan.  Creating a plan has no side effects."""

    snapshot: Snapshot
    drifts: list[Drift] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)

    @property
    def blockers(self) -> list[Drift]:
        return [drift for drift in self.drifts if drift.level == "blocker"]

    @property
    def warnings(self) -> list[Drift]:
        return [drift for drift in self.drifts if drift.level == "warning"]


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _quoted(value: str) -> str:
    """Return terminal-safe, unambiguous text for a recovery approval."""

    return json.dumps(value, ensure_ascii=True)


def _argv_text(arguments: tuple[str, ...] | list[str]) -> str:
    """Render argument boundaries and invisible Unicode explicitly."""

    return json.dumps(list(arguments), ensure_ascii=True)


def _state_from_record(record: ServiceRecord) -> ServiceState:
    """Translate the runtime registry representation into snapshot state."""

    return ServiceState(
        name=record.name,
        command=list(record.argv),
        cwd=record.cwd,
        ports=sorted(set(record.declared_ports + record.detected_ports)),
        ready_url=record.ready_url,
        status="running" if service_record_is_running(record) else "stopped",
    )


def _state_from_config(service: object, root: Path) -> ServiceState:
    # ServiceConfig is intentionally kept small; getattr keeps this adapter
    # forwards-compatible with optional config fields.
    cwd_value = getattr(service, "cwd", None)
    cwd = Path(cwd_value) if cwd_value and cwd_value != "." else root
    if not cwd.is_absolute():
        cwd = root / cwd
    ports = list(getattr(service, "ports", []) or [])
    return ServiceState(
        name=str(getattr(service, "name")),
        command=list(getattr(service, "command")),
        cwd=str(cwd.resolve()),
        ports=sorted(set(ports)),
        ready_url=getattr(service, "ready_url", None),
        status="configured",
    )


def collect_services(root: Path, config: ProjectConfig, registry: ServiceRegistry) -> list[ServiceState]:
    """Collect configured services and overlay live DevFreeze-managed services."""

    collected: dict[str, ServiceState] = {}
    for service in config.services:
        state = _state_from_config(service, root)
        collected[state.name] = state

    for record in registry.list():
        cwd = Path(record.cwd)
        record_root = Path(record.workspace_root or record.cwd).resolve()
        if record_root == root.resolve() and _is_within(cwd, root):
            collected[record.name] = _state_from_record(record)

    return [collected[name] for name in sorted(collected)]


def _same_service(record: ServiceRecord, service: ServiceState, root: Path) -> bool:
    """Match a live record to the exact captured project service."""

    record_root = Path(record.workspace_root or record.cwd).resolve()
    return (
        record_root == root.resolve()
        and Path(record.cwd).resolve() == Path(service.cwd).resolve()
        and tuple(record.argv) == tuple(service.command)
        and service_record_is_running(record)
    )


def capture_snapshot(
    name: str,
    *,
    cwd: Path,
    note: str | None = None,
    registry: ServiceRegistry | None = None,
) -> Snapshot:
    """Capture non-secret, metadata-only development context."""

    cwd = cwd.resolve()
    git_root = find_git_root(cwd)
    root = find_project_root(cwd)
    config = load_config(root)
    registry = registry or ServiceRegistry()

    workspace_file = getattr(config, "workspace_file", None)
    if workspace_file:
        workspace_path = Path(workspace_file)
        if not workspace_path.is_absolute():
            workspace_path = root / workspace_path
        workspace_file = str(workspace_path.resolve())

    workspace = WorkspaceState(
        root=str(root),
        cwd=str(cwd),
        workspace_file=workspace_file,
    )
    return Snapshot.create(
        name=name,
        workspace=workspace,
        git=capture_git_state(root) if git_root else None,
        platform=capture_platform(),
        tooling=capture_tooling(),
        services=collect_services(root, config, registry),
        note=note,
    )


def compare_snapshot(snapshot: Snapshot, registry: ServiceRegistry | None = None) -> list[Drift]:
    """Compare a snapshot with the current machine without changing anything."""

    drifts: list[Drift] = []
    root = Path(snapshot.workspace.root)
    if not root.is_dir():
        drifts.append(
            Drift("blocker", "workspace.root", snapshot.workspace.root, "missing", "工作区目录不存在")
        )
        return drifts

    current_git = capture_git_state(root)
    saved_git = snapshot.git
    if saved_git is not None and current_git is None:
        drifts.append(Drift("blocker", "git", "repository", "not a repository", "Git 仓库已不存在"))
    elif saved_git is not None and current_git is not None:
        if saved_git.remote != current_git.remote and (saved_git.remote or current_git.remote):
            drifts.append(
                Drift("blocker", "git.remote", saved_git.remote, current_git.remote, "仓库远程地址不同")
            )
        if saved_git.branch != current_git.branch:
            drifts.append(
                Drift(
                    "warning",
                    "git.branch",
                    saved_git.branch or "(detached)",
                    current_git.branch or "(detached)",
                    "当前分支与快照不同；DevFreeze 不会自动切换",
                )
            )
        if saved_git.head != current_git.head:
            drifts.append(
                Drift("warning", "git.head", saved_git.head, current_git.head, "HEAD 已发生变化")
            )
        saved_changed = set(saved_git.changed_files)
        current_changed = set(current_git.changed_files)
        if saved_changed != current_changed:
            drifts.append(
                Drift(
                    "warning",
                    "git.changed_files",
                    f"{len(saved_changed)} files",
                    f"{len(current_changed)} files",
                    "未提交文件列表已发生变化；DevFreeze 不会覆盖文件",
                )
            )

    current_tools = {tool.name: tool.version for tool in capture_tooling()}
    for saved_tool in snapshot.tooling:
        current_version = current_tools.get(saved_tool.name)
        if current_version != saved_tool.version:
            drifts.append(
                Drift(
                    "warning",
                    f"tooling.{saved_tool.name}",
                    saved_tool.version,
                    current_version or "missing",
                    f"{saved_tool.name} 版本与快照不同",
                )
            )

    registry = registry or ServiceRegistry()
    live = {
        record.name: record
        for record in registry.list()
        if Path(record.workspace_root or record.cwd).resolve() == root.resolve()
    }
    for service in snapshot.services:
        record = live.get(service.name)
        if record and _same_service(record, service, root):
            continue
        drifts.append(
            Drift("info", f"service.{service.name}", service.status, "stopped", "服务当前未运行")
        )
    return drifts


def build_recovery_plan(snapshot: Snapshot, registry: ServiceRegistry | None = None) -> RecoveryPlan:
    """Build a recovery plan whose steps are descriptive, never executable strings."""

    registry = registry or ServiceRegistry()
    drifts = compare_snapshot(snapshot, registry)
    steps = [f"进入工作区 {_quoted(snapshot.workspace.root)}"]
    if snapshot.git is not None:
        branch = snapshot.git.branch or "detached HEAD"
        head = snapshot.git.head[:12] if snapshot.git.head else "unborn"
        steps.append(f"核对 Git 状态（快照分支 {_quoted(branch)}，提交 {head}）")
    root = Path(snapshot.workspace.root).resolve()
    live = {
        record.name: record
        for record in registry.list()
        if Path(record.workspace_root or record.cwd).resolve() == root
    }
    for service in snapshot.services:
        record = live.get(service.name)
        if record and _same_service(record, service, root):
            steps.append(f"保留已运行服务 {service.name}（PID {record.pid}）")
        else:
            steps.append(f"启动服务 {service.name}，参数: {_argv_text(service.command)}")
    if snapshot.workspace.workspace_file:
        steps.append(f"可手动打开工作区文件 {_quoted(snapshot.workspace.workspace_file)}")
    return RecoveryPlan(snapshot=snapshot, drifts=drifts, steps=steps)


def execute_recovery_plan(
    plan: RecoveryPlan,
    *,
    registry: ServiceRegistry | None = None,
    force: bool = False,
) -> list[ServiceRecord]:
    """Start missing services in a reviewed plan.

    Git state is never modified.  ``force`` only acknowledges drift; it cannot
    turn on checkout, reset, dependency installation, or shell evaluation.
    """

    # Recompute immediately before starting anything.  The user may have kept
    # an interactive approval prompt open while Git or the workspace changed.
    # This cannot eliminate every same-user filesystem race, but it closes the
    # human-review window and fails closed on newly introduced drift.
    fresh_drifts = compare_snapshot(plan.snapshot, registry)
    fresh_blockers = [drift for drift in fresh_drifts if drift.level == "blocker"]
    if fresh_blockers:
        reasons = "; ".join(drift.message for drift in fresh_blockers)
        raise RuntimeError(f"恢复被阻止: {reasons}")
    actionable_warnings = [
        drift
        for drift in fresh_drifts
        if drift.level == "warning" and drift.field.startswith("git.")
    ]
    if actionable_warnings and not force:
        raise RuntimeError("Git 状态与快照不同；检查后使用 --force 明确认可当前状态")

    root = Path(plan.snapshot.workspace.root).resolve()
    registry = registry or ServiceRegistry()
    live = {
        record.name: record
        for record in registry.list()
        if Path(record.workspace_root or record.cwd).resolve() == root
    }
    started: list[ServiceRecord] = []
    for service in plan.snapshot.services:
        current = live.get(service.name)
        if current and _same_service(current, service, root):
            continue
        service_cwd = Path(service.cwd).resolve()
        if not _is_within(service_cwd, root):
            raise RuntimeError(f"拒绝启动 {service.name}: 服务目录位于工作区之外")
        if not service.command:
            raise RuntimeError(f"拒绝启动 {service.name}: 命令为空")
        started.append(
            run_detached(
                name=service.name,
                argv=list(service.command),
                cwd=service_cwd,
                workspace_root=root,
                declared_ports=list(service.ports),
                ready_url=service.ready_url,
                registry=registry,
            )
        )
    return started
