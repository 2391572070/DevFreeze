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
            executable = root / "safe-tool"
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


if __name__ == "__main__":
    unittest.main()
