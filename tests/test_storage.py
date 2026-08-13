from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
import tempfile
import stat
import unittest
from unittest import mock

from devfreeze.errors import (
    SnapshotExistsError,
    SnapshotNotFoundError,
    StorageError,
    ValidationError,
)
from devfreeze.models import PlatformState, Snapshot, WorkspaceState
from devfreeze.storage import SnapshotStore, get_data_home


def make_snapshot(root: Path, name: str = "focus") -> Snapshot:
    return Snapshot.create(
        name=name,
        workspace=WorkspaceState(str(root), str(root), None),
        git=None,
        platform=PlatformState("Linux", "test", "x86_64", "3.11"),
    )


def save_after_gate(data: str, workspace: str, note: str, ready, gate, results) -> None:
    """Compete for one snapshot name from an independently spawned process."""

    snapshot = Snapshot.create(
        name="shared",
        workspace=WorkspaceState(workspace, workspace, None),
        git=None,
        platform=PlatformState("Linux", "test", "x86_64", "3.11"),
        note=note,
    )
    ready.put(note)
    gate.wait()
    try:
        SnapshotStore(data).save(snapshot)
    except SnapshotExistsError:
        results.put(("exists", note))
    except Exception as exc:  # pragma: no cover - surfaced in the parent assertion
        results.put(("error", f"{type(exc).__name__}: {exc}"))
    else:
        results.put(("saved", note))


class StorageTests(unittest.TestCase):
    def test_home_precedence(self):
        base = Path(tempfile.gettempdir()).resolve()
        custom = base / "devfreeze-custom"
        xdg = base / "devfreeze-xdg"
        fake_home = base / "devfreeze-user"
        self.assertEqual(
            get_data_home(
                {
                    "DEVFREEZE_HOME": str(custom),
                    "XDG_DATA_HOME": str(xdg),
                }
            ),
            custom,
        )
        self.assertEqual(
            get_data_home({"XDG_DATA_HOME": str(xdg)}),
            xdg / "devfreeze",
        )
        with mock.patch("pathlib.Path.home", return_value=fake_home):
            self.assertEqual(
                get_data_home({}),
                fake_home / ".local" / "share" / "devfreeze",
            )
        with self.assertRaisesRegex(StorageError, "absolute"):
            get_data_home({"DEVFREEZE_HOME": "relative"})

    def test_save_load_list_delete_round_trip(self):
        with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as workspace:
            store = SnapshotStore(data)
            second = make_snapshot(Path(workspace), "z-last")
            first = make_snapshot(Path(workspace), "a-first")
            path = store.save(second)
            store.save(first)
            self.assertEqual(path.name, "z-last.json")
            self.assertEqual(store.list_names(), ["a-first", "z-last"])
            self.assertEqual([item.name for item in store.list()], ["a-first", "z-last"])
            self.assertEqual(store.load("z-last"), second)
            self.assertTrue(store.delete("a-first"))
            self.assertFalse(store.delete("a-first", missing_ok=True))

    def test_save_is_private_atomic_and_does_not_overwrite_by_default(self):
        with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as workspace:
            store = SnapshotStore(data)
            snapshot = make_snapshot(Path(workspace))
            path = store.save(snapshot)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(Path(data).stat().st_mode), 0o700)
            self.assertFalse(any(item.suffix == ".tmp" for item in Path(data).iterdir()))
            with self.assertRaises(SnapshotExistsError):
                store.save(snapshot)
            replacement = Snapshot.create(
                name="focus",
                workspace=snapshot.workspace,
                git=None,
                platform=snapshot.platform,
                note="replacement",
            )
            store.save(replacement, overwrite=True)
            self.assertEqual(store.load("focus").note, "replacement")

    def test_concurrent_creation_cannot_clobber_a_racing_winner(self):
        with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as workspace:
            store = SnapshotStore(data)
            root = Path(workspace)
            contender = make_snapshot(root)
            winner = Snapshot.create(
                name="focus",
                workspace=contender.workspace,
                git=None,
                platform=contender.platform,
                note="racing winner",
            )
            real_link = os.link

            def publish_winner_then_link(source, destination):
                # Simulate another process publishing after save()'s fast
                # exists() check but before this writer's atomic publication.
                destination = Path(destination)
                destination.write_text(winner.to_json() + "\n", encoding="utf-8")
                os.chmod(destination, 0o600)
                return real_link(source, destination)

            with mock.patch("devfreeze.storage.os.link", side_effect=publish_winner_then_link):
                with self.assertRaises(SnapshotExistsError):
                    store.save(contender)

            self.assertEqual(store.load("focus"), winner)
            self.assertFalse(any(item.suffix == ".tmp" for item in Path(data).iterdir()))

    def test_only_one_spawned_process_can_publish_a_new_name(self):
        with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as workspace:
            context = multiprocessing.get_context("spawn")
            ready = context.Queue()
            gate = context.Event()
            results = context.Queue()
            processes = [
                context.Process(
                    target=save_after_gate,
                    args=(data, workspace, f"writer-{index}", ready, gate, results),
                )
                for index in range(4)
            ]
            try:
                for process in processes:
                    process.start()
                for _ in processes:
                    ready.get(timeout=15)
                gate.set()
                outcomes = [results.get(timeout=15) for _ in processes]
                for process in processes:
                    process.join(timeout=15)
                    self.assertEqual(process.exitcode, 0)
            finally:
                gate.set()
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                    process.join(timeout=5)
                ready.close()
                results.close()

            saved = [note for status, note in outcomes if status == "saved"]
            self.assertFalse(any(status == "error" for status, _ in outcomes), outcomes)
            self.assertEqual(len(saved), 1, outcomes)
            self.assertEqual(sum(status == "exists" for status, _ in outcomes), 3, outcomes)
            self.assertEqual(SnapshotStore(data).load("shared").note, saved[0])

    def test_path_traversal_is_rejected_for_every_operation(self):
        with tempfile.TemporaryDirectory() as data:
            store = SnapshotStore(data)
            for operation in (store.load, store.delete):
                with self.subTest(operation=operation.__name__), self.assertRaises(ValidationError):
                    operation("../escape")

    def test_load_rejects_symlink_invalid_data_and_name_mismatch(self):
        with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as workspace:
            store = SnapshotStore(data)
            data_path = Path(data)
            (data_path / "bad.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(StorageError, "invalid snapshot"):
                store.load("bad")
            original = make_snapshot(Path(workspace), "actual")
            (data_path / "claimed.json").write_text(original.to_json(), encoding="utf-8")
            with self.assertRaisesRegex(StorageError, "mismatch"):
                store.load("claimed")
            target = data_path / "target"
            target.write_text(original.to_json(), encoding="utf-8")
            (data_path / "link.json").symlink_to(target)
            with self.assertRaisesRegex(StorageError, "symbolic-link"):
                store.load("link")

    def test_missing_snapshot_has_specific_error(self):
        with tempfile.TemporaryDirectory() as data:
            store = SnapshotStore(data)
            with self.assertRaises(SnapshotNotFoundError):
                store.load("missing")
            with self.assertRaises(SnapshotNotFoundError):
                store.delete("missing")


if __name__ == "__main__":
    unittest.main()
