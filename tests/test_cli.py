from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from devfreeze.cli import main
from devfreeze.services import ServiceRecord, ServiceRegistry


class CLITests(unittest.TestCase):
    def run_cli(self, argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_version_subcommand(self):
        code, stdout, _ = self.run_cli(["version"])
        self.assertEqual(code, 0)
        self.assertIn("devfreeze 0.1.0", stdout)

    def test_run_requires_command(self):
        code, _, stderr = self.run_cli(["run", "--name", "web"])
        self.assertEqual(code, 1)
        self.assertIn("缺少要运行的命令", stderr)

    def test_run_rejects_secret_bearing_ready_url(self):
        code, _, stderr = self.run_cli(
            [
                "run",
                "--name",
                "web",
                "--ready-url",
                "http://token@localhost/health",
                "--",
                "python",
                "server.py",
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("without credentials", stderr)

    def test_freeze_list_show_round_trip_outside_git(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as data:
            previous = Path.cwd()
            os.chdir(workspace)
            try:
                code, _, stderr = self.run_cli(["--data-dir", data, "freeze", "focus", "-m", "next step"])
                self.assertEqual((code, stderr), (0, ""))
                code, stdout, _ = self.run_cli(["--data-dir", data, "list"])
                self.assertEqual(code, 0)
                self.assertIn("focus", stdout)
                code, stdout, _ = self.run_cli(["--data-dir", data, "show", "focus"])
                self.assertEqual(code, 0)
                self.assertIn("next step", stdout)
            finally:
                os.chdir(previous)

    def test_thaw_is_preview_by_default(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as data:
            previous = Path.cwd()
            os.chdir(workspace)
            try:
                self.run_cli(["--data-dir", data, "freeze", "focus"])
                with mock.patch("devfreeze.cli.execute_recovery_plan") as execute:
                    code, stdout, _ = self.run_cli(["--data-dir", data, "thaw", "focus"])
                self.assertEqual(code, 0)
                self.assertIn("这里只是预览", stdout)
                execute.assert_not_called()
            finally:
                os.chdir(previous)

    def test_stop_falls_back_to_unique_record_after_config_scope_changes(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as data:
            old_scope = Path(workspace) / "app"
            old_scope.mkdir()
            record = ServiceRecord(
                name="worker",
                argv=["python", "worker.py"],
                cwd=str(old_scope),
                pid=12345,
                process_start_time="linux:1",
                started_at="2026-08-13T00:00:00+00:00",
                workspace_root=str(old_scope),
                detached=True,
            )
            registry = ServiceRegistry(Path(data) / "runtime/services.json")
            registry.upsert(record)
            previous = Path.cwd()
            os.chdir(workspace)
            try:
                with mock.patch.object(
                    ServiceRegistry,
                    "stop",
                    return_value=True,
                ) as stop:
                    code, _, stderr = self.run_cli(
                        ["--data-dir", data, "stop", "worker"]
                    )
            finally:
                os.chdir(previous)
            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(
                Path(stop.call_args.kwargs["workspace_root"]).resolve(),
                old_scope.resolve(),
            )


if __name__ == "__main__":
    unittest.main()
