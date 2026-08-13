from __future__ import annotations

import unittest
from unittest import mock

from devfreeze.errors import ValidationError
from devfreeze.models import PlatformState
from devfreeze.tooling import capture_platform, capture_tooling


class ToolingTests(unittest.TestCase):
    def test_platform_capture_returns_valid_model(self):
        state = capture_platform()
        self.assertIsInstance(state, PlatformState)
        self.assertTrue(state.system)
        self.assertTrue(state.release)
        self.assertTrue(state.machine)
        self.assertTrue(state.python)

    def test_capture_runs_allowlisted_command_without_shell(self):
        completed = mock.Mock(returncode=0, stdout="git version test\n")
        with mock.patch("devfreeze.tooling.trusted_which", return_value="/usr/bin/git"), mock.patch(
            "devfreeze.tooling.subprocess.run", return_value=completed
        ) as run:
            tools = capture_tooling(("git",))
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].name, "git")
        self.assertEqual(tools[0].version, "git version test")
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["/usr/bin/git", "--version"])
        self.assertFalse(kwargs["shell"])

    def test_unknown_tool_is_rejected_without_execution(self):
        with mock.patch("devfreeze.tooling.subprocess.run") as run:
            with self.assertRaisesRegex(ValidationError, "unsupported tool"):
                capture_tooling(("definitely-not-allowed",))
        run.assert_not_called()

    def test_missing_allowlisted_tool_is_omitted(self):
        with mock.patch("devfreeze.tooling.trusted_which", return_value=None), mock.patch(
            "devfreeze.tooling.subprocess.run"
        ) as run:
            tools = capture_tooling(("git",))
        self.assertEqual(tools, ())
        run.assert_not_called()

    def test_duplicate_tool_names_are_rejected(self):
        with self.assertRaisesRegex(ValidationError, "duplicates"):
            capture_tooling(("git", "git"))


if __name__ == "__main__":
    unittest.main()
