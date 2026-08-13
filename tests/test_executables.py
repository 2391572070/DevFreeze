from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from devfreeze.executables import trusted_which


class TrustedWhichTests(unittest.TestCase):
    def test_ignores_empty_and_relative_path_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / ("safe-tool.exe" if os.name == "nt" else "safe-tool")
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o700)
            with mock.patch.dict(
                os.environ,
                {"PATH": os.pathsep.join(("", ".", str(root)))},
                clear=False,
            ):
                resolved = trusted_which("safe-tool")
            self.assertEqual(resolved, str(executable.resolve()))

    def test_rejects_path_like_names(self):
        self.assertIsNone(trusted_which("./git"))
        self.assertIsNone(trusted_which("tools\\git"))

    @unittest.skipUnless(os.name == "nt", "requires Windows PATHEXT semantics")
    def test_windows_ignores_batch_shims_during_trusted_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "safe-tool.cmd").write_text("@echo off\n", encoding="utf-8")
            (root / "safe-tool.exe").write_bytes(b"MZ")
            with mock.patch.dict(
                os.environ,
                {
                    "PATH": str(root),
                    "PATHEXT": os.pathsep.join((".CMD", ".EXE")),
                },
                clear=False,
            ):
                resolved = trusted_which("safe-tool")
            self.assertEqual(resolved, str((root / "safe-tool.exe").resolve()))


if __name__ == "__main__":
    unittest.main()
