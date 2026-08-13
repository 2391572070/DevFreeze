from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from devfreeze.config import ConfigError, find_project_root, load_config


class ConfigTests(unittest.TestCase):
    def write(self, root: Path, contents: str) -> None:
        (root / ".devfreeze.toml").write_text(contents, encoding="utf-8")

    def test_missing_config_returns_default(self):
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(directory)
        self.assertEqual(config.version, 1)
        self.assertEqual(config.services, ())
        self.assertIsNone(config.workspace_file)

    def test_loads_strict_service_array_and_finds_parent_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonical_root = root.resolve()
            nested = root / "src" / "package"
            nested.mkdir(parents=True)
            self.write(
                root,
                """
version = 1
workspace_file = "dev.code-workspace"

[[services]]
name = "web"
command = ["python", "-m", "http.server", "8000"]
cwd = "src"
ports = [8000]
ready_url = "http://localhost:8000/health"

[[services]]
name = "worker"
command = ["python", "worker.py"]
""",
            )
            config = load_config(root)
            self.assertEqual(find_project_root(nested), canonical_root)
        self.assertEqual(config.workspace_file, "dev.code-workspace")
        self.assertEqual([service.name for service in config.services], ["web", "worker"])
        self.assertEqual(config.services[0].command, ("python", "-m", "http.server", "8000"))
        self.assertEqual(config.services[0].ports, (8000,))

    def test_project_root_is_canonical_across_directory_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            container = Path(directory)
            root = container / "project"
            nested = root / "src"
            nested.mkdir(parents=True)
            self.write(root, "version = 1\n")
            alias = container / "project-alias"
            try:
                alias.symlink_to(root, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symbolic links are unavailable")

            self.assertEqual(find_project_root(alias / "src"), root.resolve())
            self.assertEqual(load_config(alias).version, 1)

    def test_command_must_be_nonempty_string_array(self):
        invalid_values = ('"npm run dev"', "[]", '["npm", 1]')
        for command in invalid_values:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.write(
                    root,
                    f'version = 1\n[[services]]\nname = "web"\ncommand = {command}\n',
                )
                with self.assertRaisesRegex(ConfigError, r"array of strings|string array"):
                    load_config(root)

    def test_rejects_unknown_fields_and_unsupported_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, "version = 1\nsurprise = true\n")
            with self.assertRaisesRegex(ConfigError, r"unknown (fields|keys)"):
                load_config(root)
            self.write(root, "version = 2\n")
            with self.assertRaisesRegex(ConfigError, r"version"):
                load_config(root)

    def test_rejects_path_traversal_credentials_bad_ports_and_duplicates(self):
        invalid_documents = (
            'version=1\n[[services]]\nname="web"\ncommand=["run"]\ncwd="../outside"\n',
            'version=1\n[[services]]\nname="web"\ncommand=["run"]\nports=[0]\n',
            'version=1\n[[services]]\nname="web"\ncommand=["run"]\n[[services]]\nname="web"\ncommand=["other"]\n',
        )
        for document in invalid_documents:
            with self.subTest(document=document), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.write(root, document)
                with self.assertRaises(ConfigError):
                    load_config(root)

    def test_rejects_symlink_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "config-source"
            target.write_text("version = 1\n", encoding="utf-8")
            (root / ".devfreeze.toml").symlink_to(target)
            with self.assertRaisesRegex(ConfigError, "symbolic-link"):
                load_config(root)


if __name__ == "__main__":
    unittest.main()
