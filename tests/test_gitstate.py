from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from devfreeze.gitstate import capture_git_state, find_git_root, sanitise_remote


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


class GitStateTests(unittest.TestCase):
    def test_returns_none_outside_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(find_git_root(directory))
            self.assertIsNone(capture_git_state(directory))

    def test_captures_unborn_branch_and_untracked_filenames(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git(root, "init", "-b", "focus")
            (root / "new file.txt").write_text("secret contents must not be read", encoding="utf-8")
            state = capture_git_state(root)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.branch, "focus")
        self.assertEqual(state.head, "unborn")
        self.assertTrue(state.dirty)
        self.assertEqual(state.changed_files, ("new file.txt",))

    def test_captures_head_remote_and_modified_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git(root, "init", "-b", "main")
            git(root, "config", "user.name", "Test")
            git(root, "config", "user.email", "test@example.invalid")
            git(root, "remote", "add", "origin", "https://token@example.com/org/repo.git?access=secret")
            tracked = root / "tracked.txt"
            tracked.write_text("first", encoding="utf-8")
            git(root, "add", "tracked.txt")
            git(root, "commit", "-m", "first")
            tracked.write_text("changed", encoding="utf-8")
            state = capture_git_state(root / "tracked.txt")
        assert state is not None
        self.assertEqual(state.branch, "main")
        self.assertRegex(state.head, r"^[0-9a-f]{40,64}$")
        self.assertEqual(state.remote, "https://example.com/org/repo.git")
        self.assertEqual(state.changed_files, ("tracked.txt",))

    def test_remote_sanitisation(self):
        cases = {
            "ssh://user:pass@example.com:2222/org/repo?token=x#x": "ssh://example.com:2222/org/repo",
            "git@example.com:org/repo.git": "example.com:org/repo.git",
            "https://example.com/org/repo.git": "https://example.com/org/repo.git",
            "/local/repo?ignored": "/local/repo",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(sanitise_remote(source), expected)

    def test_git_calls_are_shell_free_argument_vectors(self):
        completed = subprocess.CompletedProcess([], 1, b"", b"not git")
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "devfreeze.gitstate.subprocess.run", return_value=completed
        ) as run, mock.patch(
            "devfreeze.gitstate.trusted_which", return_value="/usr/bin/git"
        ):
            self.assertIsNone(find_git_root(directory))
        args, kwargs = run.call_args
        self.assertIsInstance(args[0], list)
        self.assertFalse(kwargs["shell"])
        self.assertIn("-C", args[0])
        self.assertEqual(args[0][0], "/usr/bin/git")


if __name__ == "__main__":
    unittest.main()
