from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from devfreeze.models import GitState, PlatformState, ServiceState, Snapshot, ToolInfo, WorkspaceState
from devfreeze.recovery import build_recovery_plan, capture_snapshot, compare_snapshot, execute_recovery_plan
from devfreeze.services import ServiceRecord


class FakeRegistry:
    def __init__(self, records=None):
        self.records = list(records or [])

    def list(self):
        return list(self.records)


def make_snapshot(root: Path, *, git: GitState | None = None, services=()) -> Snapshot:
    return Snapshot.create(
        name="focus",
        workspace=WorkspaceState(root=str(root), cwd=str(root), workspace_file=None),
        git=git,
        platform=PlatformState(system="Linux", release="test", machine="x86_64", python="3.11"),
        tooling=(ToolInfo(name="git", version="git version test"),),
        services=tuple(services),
        note="continue here",
    )


class RecoveryTests(unittest.TestCase):
    def test_nested_config_is_the_workspace_root_inside_a_monorepo(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            subprocess.run(["git", "init", "-q", repository], check=True)
            app = repository / "packages" / "app"
            app.mkdir(parents=True)
            (app / ".devfreeze.toml").write_text(
                'version = 1\n[[services]]\nname = "web"\n'
                'command = ["python", "-m", "http.server"]\n',
                encoding="utf-8",
            )

            with mock.patch("devfreeze.recovery.capture_tooling", return_value=()):
                snapshot = capture_snapshot(
                    "nested",
                    cwd=app,
                    registry=FakeRegistry(),
                )

            self.assertEqual(snapshot.workspace.root, str(app.resolve()))
            self.assertEqual([service.name for service in snapshot.services], ["web"])
            self.assertIsNotNone(snapshot.git)

    def test_plan_does_not_treat_other_workspace_service_as_running(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = ServiceState(
                name="web",
                command=("python", "server.py"),
                cwd=str(root),
                ports=(),
                ready_url=None,
                status="configured",
            )
            record = ServiceRecord(
                name="web",
                argv=["python", "server.py"],
                cwd="/tmp/another-project",
                pid=12345,
                process_start_time="linux:1",
                started_at="2026-08-13T00:00:00+00:00",
                workspace_root="/tmp/another-project",
            )
            snapshot = make_snapshot(root, services=(service,))
            with mock.patch("devfreeze.recovery.capture_tooling", return_value=(ToolInfo("git", "git version test"),)):
                plan = build_recovery_plan(snapshot, FakeRegistry([record]))

            self.assertTrue(any("启动服务 web" in step for step in plan.steps))

    def test_plan_uses_registry_tree_liveness_for_orphaned_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = ServiceState(
                name="worker",
                command=("python", "worker.py"),
                cwd=str(root),
                status="running",
            )
            record = ServiceRecord(
                name="worker",
                argv=["python", "worker.py"],
                cwd=str(root),
                pid=12345,
                process_start_time="linux:1",
                started_at="2026-08-13T00:00:00+00:00",
                workspace_root=str(root),
                detached=True,
            )
            snapshot = make_snapshot(root, services=(service,))
            with mock.patch(
                "devfreeze.recovery.service_record_is_running",
                return_value=True,
            ), mock.patch(
                "devfreeze.recovery.capture_tooling",
                return_value=(ToolInfo("git", "git version test"),),
            ):
                plan = build_recovery_plan(snapshot, FakeRegistry([record]))

            self.assertTrue(any("保留已运行服务 worker" in step for step in plan.steps))

    def test_execute_revalidates_after_plan_was_displayed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            saved = GitState(None, "main", "a" * 40, False, ())
            changed = GitState(None, "other", "b" * 40, False, ())
            snapshot = make_snapshot(root, git=saved)
            with mock.patch(
                "devfreeze.recovery.capture_git_state",
                side_effect=[saved, changed],
            ), mock.patch(
                "devfreeze.recovery.capture_tooling",
                return_value=(ToolInfo("git", "git version test"),),
            ):
                plan = build_recovery_plan(snapshot, FakeRegistry())
                with self.assertRaisesRegex(RuntimeError, "--force"):
                    execute_recovery_plan(plan, registry=FakeRegistry())

    def test_missing_workspace_is_blocker(self):
        snapshot = make_snapshot(Path("/definitely/missing/devfreeze-test"))
        plan = build_recovery_plan(snapshot, FakeRegistry())
        self.assertEqual(len(plan.blockers), 1)
        self.assertEqual(plan.blockers[0].field, "workspace.root")

    def test_git_drift_is_warning_and_never_mutates_git(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            saved = GitState(
                remote=None,
                branch="saved-branch",
                head="a" * 40,
                dirty=False,
                changed_files=(),
            )
            current = GitState(
                remote=None,
                branch="current-branch",
                head="b" * 40,
                dirty=True,
                changed_files=("changed.txt",),
            )
            snapshot = make_snapshot(root, git=saved)
            with mock.patch("devfreeze.recovery.capture_git_state", return_value=current), mock.patch(
                "devfreeze.recovery.capture_tooling", return_value=(ToolInfo("git", "git version test"),)
            ):
                drifts = compare_snapshot(snapshot, FakeRegistry())
            self.assertTrue(any(d.field == "git.branch" for d in drifts))
            self.assertTrue(any(d.field == "git.head" for d in drifts))
            self.assertTrue(any(d.field == "git.changed_files" for d in drifts))

    def test_execute_requires_force_for_git_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            saved = GitState(None, "main", "a" * 40, False, ())
            snapshot = make_snapshot(root, git=saved)
            with mock.patch(
                "devfreeze.recovery.capture_git_state",
                return_value=GitState(None, "other", "b" * 40, False, ()),
            ), mock.patch("devfreeze.recovery.capture_tooling", return_value=(ToolInfo("git", "git version test"),)):
                plan = build_recovery_plan(snapshot, FakeRegistry())
                with self.assertRaisesRegex(RuntimeError, "--force"):
                    execute_recovery_plan(plan, registry=FakeRegistry())

    def test_execute_rejects_service_outside_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = ServiceState(
                name="web",
                command=("python", "-m", "http.server"),
                cwd="/tmp/outside-devfreeze-workspace",
                ports=(8000,),
                ready_url=None,
                status="configured",
            )
            snapshot = make_snapshot(root, services=(service,))
            with mock.patch("devfreeze.recovery.capture_tooling", return_value=(ToolInfo("git", "git version test"),)):
                plan = build_recovery_plan(snapshot, FakeRegistry())
            with self.assertRaisesRegex(RuntimeError, "工作区之外"):
                execute_recovery_plan(plan, registry=FakeRegistry())


if __name__ == "__main__":
    unittest.main()
