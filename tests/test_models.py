from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from devfreeze.errors import ValidationError
from devfreeze.models import (
    GitState,
    PlatformState,
    ServiceState,
    Snapshot,
    ToolInfo,
    WorkspaceState,
    validate_name,
)


def make_snapshot(root: Path) -> Snapshot:
    return Snapshot.create(
        name="payment-bug",
        created_at="2026-08-13T08:00:00+08:00",
        workspace=WorkspaceState(str(root), str(root / "src"), str(root / "project.code-workspace")),
        git=GitState(
            "https://github.com/example/project.git",
            "fix/payment",
            "a" * 40,
            True,
            ("src/payment.py", "new.txt"),
        ),
        platform=PlatformState("Linux", "6.0", "x86_64", "3.11.0"),
        tooling=(ToolInfo("git", "git version 2.40"),),
        services=(
            ServiceState(
                "web",
                ("python", "-m", "http.server"),
                str(root),
                (8000,),
                "http://localhost:8000/",
                "configured",
            ),
        ),
        note="continue here\nthen test",
    )


class ModelTests(unittest.TestCase):
    def test_snapshot_json_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            snapshot = make_snapshot(root)
            restored = Snapshot.from_json(snapshot.to_json())
        self.assertEqual(restored, snapshot)
        self.assertEqual(restored.to_dict()["schema_version"], 1)
        self.assertIsInstance(restored.services[0].command, tuple)

    def test_snapshot_rejects_unknown_and_missing_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = make_snapshot(Path(directory))
            payload = snapshot.to_dict()
            payload["surprise"] = True
            with self.assertRaisesRegex(ValidationError, "unknown fields"):
                Snapshot.from_dict(payload)
            payload = snapshot.to_dict()
            del payload["platform"]
            with self.assertRaisesRegex(ValidationError, "missing required"):
                Snapshot.from_dict(payload)

    def test_snapshot_rejects_duplicate_json_keys_and_nonfinite_numbers(self):
        with self.assertRaisesRegex(ValidationError, "duplicate field"):
            Snapshot.from_json('{"schema_version":1,"schema_version":1}')
        with self.assertRaisesRegex(ValidationError, "invalid number"):
            Snapshot.from_json('{"schema_version":NaN}')

    def test_name_validation_prevents_traversal_and_reserved_names(self):
        for invalid in ("../focus", ".focus", "with space", "CON", "a" * 65, ""):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                validate_name(invalid)
        self.assertEqual(validate_name("fix.payment-1"), "fix.payment-1")

    def test_workspace_paths_are_absolute_and_cwd_is_inside_root(self):
        with self.assertRaisesRegex(ValidationError, "absolute"):
            WorkspaceState("relative", "relative", None)
        with self.assertRaisesRegex(ValidationError, "inside"):
            WorkspaceState("/project", "/elsewhere", None)

    def test_git_state_allows_unborn_and_rejects_path_traversal(self):
        state = GitState(None, "main", "unborn", True, ("README.md",))
        self.assertEqual(state.head, "unborn")
        with self.assertRaisesRegex(ValidationError, "traverse"):
            GitState(None, "main", "abcd", True, ("../secret",))

    def test_service_rejects_empty_command_bad_status_and_bad_ports(self):
        with self.assertRaisesRegex(ValidationError, "must not be empty"):
            ServiceState("web", (), "/project")
        with self.assertRaisesRegex(ValidationError, "one of"):
            ServiceState("web", ("run",), "/project", status="configured-ish")
        with self.assertRaisesRegex(ValidationError, "between"):
            ServiceState("web", ("run",), "/project", ports=(0,))
        with self.assertRaisesRegex(ValidationError, "without credentials"):
            ServiceState(
                "web",
                ("run",),
                "/project",
                ready_url="http://secret@localhost/health",
            )

    def test_snapshot_rejects_naive_timestamp_and_future_version(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = make_snapshot(Path(directory)).to_dict()
            payload["created_at"] = "2026-08-13T08:00:00"
            with self.assertRaisesRegex(ValidationError, "UTC offset"):
                Snapshot.from_dict(payload)
            payload = make_snapshot(Path(directory)).to_dict()
            payload["schema_version"] = 2
            with self.assertRaisesRegex(ValidationError, "unsupported"):
                Snapshot.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
