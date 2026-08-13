"""Command-line interface for DevFreeze."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Sequence

from . import __version__
from .config import load_config, validate_ready_url
from .errors import DevFreezeError
from .executables import trusted_which
from .recovery import build_recovery_plan, capture_snapshot, execute_recovery_plan
from .services import (
    ServiceRegistry,
    run_detached,
    run_foreground,
    service_record_is_running,
)
from .storage import SnapshotStore, get_data_home


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devfreeze",
        description="Save and safely resume local development context.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--data-dir", type=Path, help="override DevFreeze data directory")
    sub = parser.add_subparsers(dest="command", required=True)

    freeze = sub.add_parser("freeze", help="capture the current development context")
    freeze.add_argument("name")
    freeze.add_argument("-m", "--message", dest="note", help="short hand-off note")
    freeze.add_argument("--force", action="store_true", help="replace an existing snapshot")
    freeze.add_argument("--json", action="store_true", help="print the saved snapshot as JSON")

    list_cmd = sub.add_parser("list", help="list snapshots")
    list_cmd.add_argument("--json", action="store_true")

    show = sub.add_parser("show", help="show one snapshot")
    show.add_argument("name")
    show.add_argument("--json", action="store_true")

    diff = sub.add_parser("diff", help="compare a snapshot with the current machine")
    diff.add_argument("name")
    diff.add_argument("--json", action="store_true")

    thaw = sub.add_parser("thaw", help="preview or execute a safe recovery plan")
    thaw.add_argument("name")
    thaw.add_argument("--execute", action="store_true", help="start missing captured services")
    thaw.add_argument("--force", action="store_true", help="acknowledge Git drift without changing Git")
    thaw.add_argument("-y", "--yes", action="store_true", help="skip the interactive confirmation")
    thaw.add_argument("--json", action="store_true")

    run = sub.add_parser("run", help="run and track a project service")
    run.add_argument("--name", required=True, help="stable service name")
    run.add_argument("--cwd", type=Path, default=Path.cwd())
    run.add_argument("--port", type=int, action="append", default=[], dest="ports")
    run.add_argument("--ready-url")
    run.add_argument("--detach", action="store_true")
    run.add_argument("argv", nargs=argparse.REMAINDER, metavar="-- COMMAND")

    services = sub.add_parser("services", help="list tracked services")
    services.add_argument("--json", action="store_true")
    services.add_argument("--all", action="store_true", help="include stale records")

    stop = sub.add_parser("stop", help="stop a tracked service")
    stop.add_argument("name")
    stop.add_argument(
        "--workspace",
        type=Path,
        help="workspace root recorded when the service was started",
    )
    stop.add_argument("--force", action="store_true", help="send SIGKILL after a short timeout")

    delete = sub.add_parser("delete", help="delete a snapshot")
    delete.add_argument("name")
    delete.add_argument("-y", "--yes", action="store_true")

    sub.add_parser("doctor", help="check the local installation and project")
    sub.add_parser("version", help="print the DevFreeze version")
    return parser


def _store(args: argparse.Namespace) -> SnapshotStore:
    return SnapshotStore(args.data_dir) if args.data_dir else SnapshotStore()


def _registry(args: argparse.Namespace) -> ServiceRegistry:
    if args.data_dir:
        return ServiceRegistry(args.data_dir / "runtime" / "services.json")
    return ServiceRegistry()


def _display_snapshot(snapshot: object) -> None:
    print(f"{snapshot.name}  {snapshot.created_at}")
    print(f"  工作区  {snapshot.workspace.root}")
    if snapshot.git:
        branch = snapshot.git.branch or "(detached)"
        dirty = "，有未提交修改" if snapshot.git.dirty else "，工作区干净"
        head = snapshot.git.head[:12] if snapshot.git.head else "(unborn)"
        print(f"  Git     {branch} @ {head}{dirty}")
    if snapshot.services:
        names = ", ".join(service.name for service in snapshot.services)
        print(f"  服务    {names}")
    if snapshot.note:
        print(f"  笔记    {snapshot.note}")


def _plan_dict(plan: object) -> dict[str, object]:
    return {
        "snapshot": plan.snapshot.name,
        "steps": plan.steps,
        "drifts": [drift.__dict__ for drift in plan.drifts],
        "blocked": bool(plan.blockers),
    }


def _display_plan(plan: object) -> None:
    print(f"恢复计划：{plan.snapshot.name}")
    for index, step in enumerate(plan.steps, 1):
        print(f"  {index}. {step}")
    if plan.drifts:
        print("\n状态差异：")
        symbols = {"blocker": "✗", "warning": "!", "info": "·"}
        for drift in plan.drifts:
            print(f"  {symbols.get(drift.level, '·')} [{drift.level}] {drift.message}")
            print(f"    {drift.field}: {drift.saved} → {drift.current}")
    print("\nDevFreeze 不会 checkout、reset、安装依赖或覆盖文件。")


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _clean_argv(argv: Sequence[str]) -> list[str]:
    command = list(argv)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise DevFreezeError("缺少要运行的命令；示例: devfreeze run --name web -- npm run dev")
    if "\x00" in "".join(command):
        raise DevFreezeError("命令参数包含 NUL 字节")
    return command


def _cmd_freeze(args: argparse.Namespace) -> int:
    registry = _registry(args)
    snapshot = capture_snapshot(args.name, cwd=Path.cwd(), note=args.note, registry=registry)
    _store(args).save(snapshot, overwrite=args.force)
    if args.json:
        print(snapshot.to_json())
    else:
        print(f"✓ 已保存快照 {snapshot.name}")
        _display_snapshot(snapshot)
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    snapshots = _store(args).list()
    if args.json:
        print(json.dumps([snapshot.to_dict() for snapshot in snapshots], ensure_ascii=False, indent=2))
    elif not snapshots:
        print("还没有快照。使用 devfreeze freeze <name> 创建一个。")
    else:
        for snapshot in snapshots:
            _display_snapshot(snapshot)
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    snapshot = _store(args).load(args.name)
    if args.json:
        print(snapshot.to_json())
    else:
        _display_snapshot(snapshot)
        if snapshot.git and snapshot.git.changed_files:
            print("  修改文件")
            for path in snapshot.git.changed_files:
                print(f"    - {path}")
        if snapshot.tooling:
            print("  工具链")
            for tool in snapshot.tooling:
                print(f"    - {tool.name}: {tool.version}")
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    plan = build_recovery_plan(_store(args).load(args.name), _registry(args))
    if args.json:
        print(json.dumps(_plan_dict(plan), ensure_ascii=False, indent=2))
    elif not plan.drifts:
        print("✓ 当前开发现场与快照一致")
    else:
        _display_plan(plan)
    return 2 if plan.blockers else 0


def _cmd_thaw(args: argparse.Namespace) -> int:
    plan = build_recovery_plan(_store(args).load(args.name), _registry(args))
    if args.json:
        print(json.dumps(_plan_dict(plan), ensure_ascii=False, indent=2))
        if not args.execute:
            return 2 if plan.blockers else 0
    else:
        _display_plan(plan)
    if not args.execute:
        if not args.json:
            print("\n这里只是预览。确认后可运行 devfreeze thaw " + args.name + " --execute")
        return 2 if plan.blockers else 0
    if plan.blockers:
        raise DevFreezeError("恢复计划存在阻断项，不能执行")
    if not args.yes and not _confirm("确认启动上述缺失服务？"):
        raise DevFreezeError("未确认，未执行任何操作")
    started = execute_recovery_plan(plan, registry=_registry(args), force=args.force)
    if not args.json:
        if started:
            for record in started:
                print(f"✓ 已启动 {record.name}（PID {record.pid}）")
        else:
            print("✓ 无需启动服务")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    command = _clean_argv(args.argv)
    cwd = args.cwd.resolve()
    if not cwd.is_dir():
        raise DevFreezeError(f"工作目录不存在: {cwd}")
    for port in args.ports:
        if port < 1 or port > 65535:
            raise DevFreezeError(f"端口超出范围: {port}")
    if args.ready_url is not None:
        validate_ready_url(args.ready_url, context="--ready-url")
    workspace_root = _project_scope(cwd)
    kwargs = {
        "name": args.name,
        "argv": command,
        "cwd": cwd,
        "workspace_root": workspace_root,
        "declared_ports": args.ports,
        "ready_url": args.ready_url,
        "registry": _registry(args),
    }
    if args.detach:
        record = run_detached(**kwargs)
        print(f"✓ 已在后台启动 {record.name}（PID {record.pid}）")
        if record.log_path:
            print(f"  日志: {record.log_path}")
        return 0
    return run_foreground(**kwargs)


def _cmd_services(args: argparse.Namespace) -> int:
    registry = _registry(args)
    if not args.all:
        registry.cleanup_stale()
    records = registry.list()
    if args.json:
        print(json.dumps([record.to_dict() for record in records], ensure_ascii=False, indent=2))
    elif not records:
        print("没有托管服务。")
    else:
        for record in records:
            state = "running" if service_record_is_running(record) else "stale"
            ports = sorted(set(record.declared_ports + record.detected_ports))
            port_text = ",".join(str(port) for port in ports) if ports else "-"
            scope = record.workspace_root or record.cwd
            print(
                f"{record.name:<18} {state:<8} pid={record.pid:<7} "
                f"ports={port_text} workspace={scope}"
            )
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    registry = _registry(args)
    workspace = (
        args.workspace.resolve()
        if args.workspace is not None
        else _project_scope(Path.cwd())
    )
    record = registry.get(
        args.name,
        workspace_root=workspace,
        refresh=False,
    )
    if record is None and args.workspace is None:
        # Project configuration may have been removed after launch.  A globally
        # unique name remains safe to resolve; ambiguous names require the new
        # explicit --workspace selector.
        record = registry.get(args.name, refresh=False)
        if record is not None:
            workspace = Path(record.workspace_root or record.cwd)
    stopped = registry.stop(
        args.name,
        workspace_root=workspace,
        force=args.force,
    )
    if stopped:
        print(f"✓ 已停止 {args.name}")
    else:
        print(f"服务 {args.name} 已经停止，已清理记录")
    return 0


def _cmd_delete(args: argparse.Namespace) -> int:
    if not args.yes and not _confirm(f"删除快照 {args.name}？"):
        raise DevFreezeError("未确认，快照未删除")
    _store(args).delete(args.name)
    print(f"✓ 已删除快照 {args.name}")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    checks: list[tuple[bool, str]] = []
    checks.append((sys.version_info >= (3, 11), f"Python {sys.version.split()[0]}（需要 3.11+）"))
    git_available = trusted_which("git") is not None
    checks.append((git_available, "Git 可用（可选；用于仓库快照）"))
    data_home = args.data_dir or get_data_home()
    try:
        SnapshotStore(data_home).ensure_private_root(create=True)
        writable = os.access(data_home, os.W_OK)
    except OSError:
        writable = False
    checks.append((writable, f"数据目录可写: {data_home}"))
    try:
        config = load_config(Path.cwd())
        config_text = f"项目配置有效（{len(config.services)} 个服务）"
        config_ok = True
    except Exception as exc:  # doctor should report all checks, not abort early
        config_text = f"项目配置无效: {exc}"
        config_ok = False
    checks.append((config_ok, config_text))
    proc_available = Path("/proc").exists()
    port_discovery_ok = not sys.platform.startswith("linux") or proc_available
    checks.append(
        (
            port_discovery_ok,
            "Linux /proc 端口检测可用"
            if proc_available
            else "端口自动检测将安全降级（可继续使用声明端口）",
        )
    )
    for ok, message in checks:
        print(f"{'✓' if ok else '✗'} {message}")
    print("✓ 安全策略：不采集环境变量值，不保存未提交文件内容，不执行 shell 字符串")
    required = (checks[0], checks[2], checks[3])
    return 0 if all(ok for ok, _ in required) else 1


def _cmd_version(args: argparse.Namespace) -> int:
    print(f"devfreeze {__version__}")
    return 0


def _project_scope(path: Path) -> Path:
    """Use the same nearest-config/Git rule as snapshots."""

    from .config import find_project_root

    return find_project_root(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    handlers = {
        "freeze": _cmd_freeze,
        "list": _cmd_list,
        "show": _cmd_show,
        "diff": _cmd_diff,
        "thaw": _cmd_thaw,
        "run": _cmd_run,
        "services": _cmd_services,
        "stop": _cmd_stop,
        "delete": _cmd_delete,
        "doctor": _cmd_doctor,
        "version": _cmd_version,
    }
    try:
        return handlers[args.command](args)
    except (DevFreezeError, OSError, ValueError, RuntimeError) as exc:
        print(f"devfreeze: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ndevfreeze: 已取消", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
