from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import signal
import socket
import sys
import tempfile
import stat
import time
import unittest
from unittest import mock

from devfreeze.services import (
    ServiceRecord,
    ServiceRegistry,
    default_registry_path,
    detect_listening_ports,
    is_process_alive,
    process_identity_matches,
    process_start_time,
    run_detached,
    run_foreground,
)


def make_record(name: str = "web", *, pid: int = 12345) -> ServiceRecord:
    return ServiceRecord(
        name=name,
        argv=["python", "-m", "http.server"],
        cwd="/tmp/example",
        pid=pid,
        process_start_time="9876",
        started_at="2026-08-13T00:00:00+00:00",
        log_path="/tmp/web.log",
        declared_ports=[8000, 8000],
        detected_ports=[8001],
        ready_url="http://127.0.0.1:8000/health",
        status="running",
        detached=True,
    )


class ServiceRecordTests(unittest.TestCase):
    def test_serialization_round_trip(self):
        original = make_record()

        encoded = original.to_dict()
        restored = ServiceRecord.from_dict(json.loads(json.dumps(encoded)))

        self.assertEqual(restored.name, original.name)
        self.assertEqual(restored.argv, original.argv)
        self.assertEqual(restored.process_start_time, "9876")
        self.assertEqual(restored.declared_ports, [8000])
        self.assertEqual(restored.detected_ports, [8001])
        self.assertTrue(restored.detached)

    def test_rejects_reserved_name_and_empty_argument(self):
        reserved = make_record("CON")
        with self.assertRaisesRegex(Exception, "reserved"):
            ServiceRecord.from_dict(reserved.to_dict())
        empty = make_record()
        empty.argv.append("")
        with self.assertRaisesRegex(Exception, "must not be empty"):
            ServiceRecord.from_dict(empty.to_dict())

    def test_from_dict_accepts_missing_optional_fields(self):
        restored = ServiceRecord.from_dict(
            {
                "name": "worker",
                "argv": ["python", "worker.py"],
                "cwd": "/tmp/project",
                "pid": 7,
                "process_start_time": None,
                "started_at": "2026-08-13T00:00:00+00:00",
            }
        )

        self.assertEqual(restored.declared_ports, [])
        self.assertEqual(restored.detected_ports, [])
        self.assertIsNone(restored.log_path)
        self.assertEqual(restored.status, "running")


class ServiceRegistryTests(unittest.TestCase):
    def test_upsert_get_remove_uses_atomic_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime" / "services.json"
            registry = ServiceRegistry(path)
            original_replace = os.replace

            with mock.patch(
                "devfreeze.services.os.replace", wraps=original_replace
            ) as replace:
                registry.upsert(make_record())

            replace.assert_called_once()
            temporary, destination = map(Path, replace.call_args.args)
            self.assertEqual(temporary.parent, path.parent)
            self.assertEqual(destination, path)
            self.assertFalse(temporary.exists())
            self.assertEqual(registry.get("web", refresh=False).pid, 12345)
            self.assertEqual(json.loads(path.read_text())["version"], 1)
            self.assertFalse(list(path.parent.glob("*.tmp")))

            registry.upsert(make_record(pid=54321))
            self.assertEqual(len(registry.list(refresh=False)), 1)
            self.assertEqual(registry.get("web", refresh=False).pid, 54321)
            self.assertFalse(registry.remove("web", expected_pid=12345))
            self.assertTrue(registry.remove("web", expected_pid=54321))
            self.assertEqual(registry.list(refresh=False), [])

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits")
    def test_registry_and_runtime_directory_are_private(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            runtime.mkdir(mode=0o777)
            os.chmod(runtime, 0o777)
            path = runtime / "services.json"
            registry = ServiceRegistry(path)

            registry.upsert(make_record())

            self.assertEqual(stat.S_IMODE(runtime.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(registry.lock_path.stat().st_mode), 0o600)

    def test_same_name_can_be_scoped_to_different_workspaces(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ServiceRegistry(Path(directory) / "runtime/services.json")
            first = make_record(pid=111)
            first.workspace_root = "/tmp/project-a"
            second = make_record(pid=222)
            second.workspace_root = "/tmp/project-b"
            registry.upsert(first)
            registry.upsert(second)

            self.assertEqual(len(registry.list(refresh=False)), 2)
            self.assertEqual(
                registry.get(
                    "web",
                    workspace_root="/tmp/project-b",
                    refresh=False,
                ).pid,
                222,
            )
            with self.assertRaisesRegex(Exception, "ambiguous"):
                registry.get("web", refresh=False)

    def test_cleanup_stale_removes_only_stale_records(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ServiceRegistry(Path(directory) / "services.json")
            stale = make_record("old", pid=111)
            live = make_record("live", pid=222)
            registry.upsert(stale)
            registry.upsert(live)

            with mock.patch(
                "devfreeze.services.process_identity_matches",
                side_effect=lambda pid, _start: pid == 222,
            ), mock.patch(
                "devfreeze.services._managed_group_alive",
                return_value=False,
            ):
                removed = registry.cleanup_stale()

            self.assertEqual([record.name for record in removed], ["old"])
            self.assertEqual(removed[0].status, "stale")
            self.assertEqual(
                [record.name for record in registry.list(refresh=False)], ["live"]
            )

    def test_cleanup_keeps_detached_group_when_leader_has_exited(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ServiceRegistry(Path(directory) / "runtime/services.json")
            record = make_record("daemon", pid=333)
            registry.upsert(record)
            with mock.patch(
                "devfreeze.services.process_identity_matches",
                return_value=False,
            ), mock.patch(
                "devfreeze.services._managed_group_alive",
                return_value=True,
            ):
                removed = registry.cleanup_stale()

            self.assertEqual(removed, [])
            self.assertIsNotNone(
                registry.get(
                    "daemon",
                    workspace_root=record.cwd,
                    refresh=False,
                )
            )

    def test_default_path_honors_devfreeze_home(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"DEVFREEZE_HOME": directory}, clear=False
        ):
            self.assertEqual(
                default_registry_path(),
                Path(directory) / "runtime" / "services.json",
            )


class ProcessIdentityTests(unittest.TestCase):
    def test_linux_identity_requires_matching_start_time(self):
        with mock.patch("devfreeze.services.sys.platform", "linux"), mock.patch(
            "devfreeze.services.is_pid_alive", return_value=True
        ), mock.patch(
            "devfreeze.services.process_start_time", return_value="42"
        ):
            self.assertTrue(process_identity_matches(12, "42"))
            self.assertFalse(process_identity_matches(12, "43"))
            self.assertFalse(process_identity_matches(12, None))

    def test_current_process_identity(self):
        start_time = process_start_time(os.getpid())
        self.assertTrue(is_process_alive(os.getpid(), start_time))
        if sys.platform.startswith("linux"):
            self.assertIsNotNone(start_time)
            self.assertFalse(is_process_alive(os.getpid(), f"{start_time}-different"))

    def test_windows_start_time_uses_filetime_ticks_and_closes_handle(self):
        kernel32 = mock.Mock()
        handle = 0x1_0000_1234
        kernel32.OpenProcess = mock.Mock(return_value=handle)

        def get_process_times(actual_handle, creation, _exit, _kernel, _user):
            self.assertEqual(actual_handle, handle)
            creation._obj.dwLowDateTime = 0x89ABCDEF
            creation._obj.dwHighDateTime = 0x01234567
            return 1

        kernel32.GetProcessTimes = mock.Mock(side_effect=get_process_times)
        kernel32.CloseHandle = mock.Mock(return_value=1)

        with mock.patch("devfreeze.services.sys.platform", "win32"), mock.patch(
            "devfreeze.services.os.name",
            "nt",
        ), mock.patch(
            "devfreeze.services.ctypes.WinDLL",
            return_value=kernel32,
            create=True,
        ) as win_dll:
            actual = process_start_time(4567)

        filetime_ticks = (0x01234567 << 32) | 0x89ABCDEF
        dotnet_epoch_offset = 504_911_232_000_000_000
        self.assertEqual(actual, f"windows:{filetime_ticks + dotnet_epoch_offset}")
        win_dll.assert_called_once_with("kernel32", use_last_error=True)
        kernel32.OpenProcess.assert_called_once_with(0x1000, False, 4567)
        kernel32.CloseHandle.assert_called_once_with(handle)
        self.assertIs(kernel32.OpenProcess.restype, ctypes.c_void_p)
        self.assertEqual(kernel32.OpenProcess.argtypes[0], ctypes.c_uint32)
        self.assertIs(kernel32.GetProcessTimes.argtypes[0], ctypes.c_void_p)
        self.assertIs(kernel32.CloseHandle.argtypes[0], ctypes.c_void_p)

    def test_windows_start_time_open_process_failure_fails_closed(self):
        kernel32 = mock.Mock()
        kernel32.OpenProcess = mock.Mock(return_value=0)
        kernel32.GetProcessTimes = mock.Mock(return_value=1)
        kernel32.CloseHandle = mock.Mock(return_value=1)

        with mock.patch("devfreeze.services.sys.platform", "win32"), mock.patch(
            "devfreeze.services.os.name",
            "nt",
        ), mock.patch(
            "devfreeze.services.ctypes.WinDLL",
            return_value=kernel32,
            create=True,
        ):
            self.assertIsNone(process_start_time(4567))

        kernel32.GetProcessTimes.assert_not_called()
        kernel32.CloseHandle.assert_not_called()

    def test_windows_start_time_query_failure_closes_handle(self):
        kernel32 = mock.Mock()
        handle = 0x1_0000_5678
        kernel32.OpenProcess = mock.Mock(return_value=handle)
        kernel32.GetProcessTimes = mock.Mock(return_value=0)
        kernel32.CloseHandle = mock.Mock(return_value=1)

        with mock.patch("devfreeze.services.sys.platform", "win32"), mock.patch(
            "devfreeze.services.os.name",
            "nt",
        ), mock.patch(
            "devfreeze.services.ctypes.WinDLL",
            return_value=kernel32,
            create=True,
        ):
            self.assertIsNone(process_start_time(4567))

        kernel32.GetProcessTimes.assert_called_once()
        kernel32.CloseHandle.assert_called_once_with(handle)

    def test_windows_is_pid_alive_never_calls_os_kill(self):
        with mock.patch("devfreeze.services.sys.platform", "win32"), mock.patch(
            "devfreeze.services.os.name",
            "nt",
        ), mock.patch(
            "devfreeze.services.process_start_time",
            side_effect=["windows:123", None],
        ) as start_time, mock.patch(
            "devfreeze.services.os.kill",
            side_effect=AssertionError("Windows liveness must not call os.kill"),
        ) as kill:
            self.assertTrue(is_process_alive(4567))
            self.assertFalse(is_process_alive(4567))

        self.assertEqual(start_time.call_count, 2)
        kill.assert_not_called()

    def test_windows_identity_match_is_exact(self):
        with mock.patch("devfreeze.services.sys.platform", "win32"), mock.patch(
            "devfreeze.services.os.name",
            "nt",
        ), mock.patch(
            "devfreeze.services.process_start_time",
            return_value="windows:123",
        ):
            self.assertTrue(process_identity_matches(4567, "windows:123"))
            self.assertFalse(process_identity_matches(4567, "windows:124"))
            self.assertFalse(process_identity_matches(4567, None))

    def test_non_linux_orphan_group_is_retained_but_never_signalled(self):
        record = make_record("orphan")
        with mock.patch("devfreeze.services.sys.platform", "darwin"), mock.patch(
            "devfreeze.services.os.name",
            "posix",
        ), mock.patch(
            "devfreeze.services.process_identity_matches",
            return_value=False,
        ), mock.patch("devfreeze.services.os.killpg") as killpg:
            from devfreeze.services import UnsafeProcessError

            registry = mock.Mock()
            registry.get.return_value = record
            with self.assertRaises(UnsafeProcessError):
                ServiceRegistry.stop(registry, "orphan")
        killpg.assert_not_called()

    def test_non_linux_verified_detached_stop_probes_group_until_gone(self):
        record = make_record("verified", pid=43210)
        registry = mock.Mock()
        registry.get.return_value = record

        def kill_group(pid, sent_signal):
            self.assertEqual(pid, record.pid)
            if sent_signal == 0:
                raise ProcessLookupError

        with mock.patch("devfreeze.services.sys.platform", "darwin"), mock.patch(
            "devfreeze.services.os.name",
            "posix",
        ), mock.patch(
            "devfreeze.services.process_identity_matches",
            return_value=True,
        ), mock.patch(
            "devfreeze.services.os.killpg",
            side_effect=kill_group,
        ) as killpg:
            self.assertTrue(ServiceRegistry.stop(registry, "verified", timeout=0.1))

        self.assertEqual(
            [call.args[1] for call in killpg.call_args_list],
            [signal.SIGTERM, 0],
        )
        registry.remove.assert_called_once_with(
            "verified",
            workspace_root=record.workspace_root,
            expected_pid=record.pid,
        )


class ManagedProcessTests(unittest.TestCase):
    def test_foreground_process_is_removed_after_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ServiceRegistry(Path(directory) / "services.json")
            exit_code = run_foreground(
                "short",
                [sys.executable, "-c", "raise SystemExit(7)"],
                cwd=directory,
                registry=registry,
            )

            self.assertEqual(exit_code, 7)
            self.assertEqual(registry.list(refresh=False), [])

    def test_detached_process_logs_and_can_be_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ServiceRegistry(Path(directory) / "runtime/services.json")
            record = run_detached(
                "sleeper",
                [
                    sys.executable,
                    "-c",
                    "import time; print('service-ready', flush=True); time.sleep(60)",
                ],
                cwd=directory,
                registry=registry,
            )
            try:
                self.assertTrue(
                    is_process_alive(record.pid, record.process_start_time)
                )
                self.assertEqual(registry.get("sleeper", refresh=False).pid, record.pid)
                self.assertIsNotNone(record.log_path)
                if os.name == "posix":
                    self.assertEqual(
                        stat.S_IMODE(Path(record.log_path).stat().st_mode),
                        0o600,
                    )

                deadline = time.monotonic() + 2.0
                log_text = ""
                while time.monotonic() < deadline:
                    log_text = Path(record.log_path).read_text(
                        encoding="utf-8", errors="replace"
                    )
                    if "service-ready" in log_text:
                        break
                    time.sleep(0.02)
                self.assertIn("service-ready", log_text)

                self.assertTrue(registry.stop("sleeper", timeout=2.0))
                self.assertIsNone(registry.get("sleeper", refresh=False))
                self.assertFalse(
                    is_process_alive(record.pid, record.process_start_time)
                )
            finally:
                # Only the exact child identity created by this test is eligible.
                if is_process_alive(record.pid, record.process_start_time):
                    try:
                        if os.name == "posix":
                            os.killpg(record.pid, signal.SIGKILL)
                        else:  # pragma: no cover - Windows cleanup
                            os.kill(record.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass


@unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux /proc")
class LinuxPortDetectionTests(unittest.TestCase):
    def test_detects_listening_socket_owned_by_process(self):
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except PermissionError:
            self.skipTest("sandbox does not permit creating sockets")
        with listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]

            ports = detect_listening_ports(
                os.getpid(), include_descendants=False
            )

            self.assertIn(port, ports)


if __name__ == "__main__":
    unittest.main()
