from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

import parallel_resource_locks as locks


def disk_usage(free: int):
    return type("DiskUsage", (), {"total": free, "used": 0, "free": free})()


class FileMutexTests(unittest.TestCase):
    def test_releases_after_exception(self):
        if locks.msvcrt is None:
            self.skipTest("Windows byte-range locks only")
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "worker.lock"
            with self.assertRaisesRegex(RuntimeError, "sentinel"):
                with locks.file_mutex(lock_path, timeout=0.2, poll=0.01):
                    raise RuntimeError("sentinel")
            with locks.file_mutex(lock_path, timeout=0.2, poll=0.01):
                pass

    @unittest.skipUnless(os.name == "nt", "requires Windows process locking")
    def test_lock_is_exclusive_between_processes_and_released_on_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "workers.lock"
            ready_path = Path(directory) / "ready"
            module_dir = str(Path(locks.__file__).parent)
            env = os.environ.copy()
            env["PYTHONPATH"] = module_dir + os.pathsep + env.get("PYTHONPATH", "")
            code = "\n".join(
                (
                    "import sys, time",
                    "from pathlib import Path",
                    "from parallel_resource_locks import file_mutex",
                    "with file_mutex(sys.argv[1], timeout=5):",
                    "    Path(sys.argv[2]).write_text('ready')",
                    "    time.sleep(10)",
                )
            )
            child = subprocess.Popen(
                [sys.executable, "-c", code, str(lock_path), str(ready_path)],
                cwd=module_dir,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 5
                while not ready_path.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready_path.exists(), "child did not acquire its file lock")
                with self.assertRaises(TimeoutError):
                    with locks.file_mutex(lock_path, timeout=0.1, poll=0.01):
                        pass
            finally:
                child.terminate()
                child.wait(timeout=5)
            with locks.file_mutex(lock_path, timeout=0.5, poll=0.01):
                pass


class DiskReservationsTests(unittest.TestCase):
    def make_reservations(self, directory: str, *, reserve: int = 0, **kwargs):
        root = Path(directory)
        return locks.DiskReservations(
            root / "reservations.json",
            root,
            reserve_bytes=reserve,
            poll_interval=0.001,
            **kwargs,
        )

    def test_claim_uses_three_times_original_and_release_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            reservations = self.make_reservations(directory, reserve=100)
            with patch.object(locks.shutil, "disk_usage", return_value=disk_usage(400)):
                self.assertTrue(reservations.acquire("lane-a", 100, [], lambda: False))
            self.assertEqual(reservations._read()["lane-a"]["original_bytes"], 100)
            self.assertTrue(reservations.release("lane-a"))
            self.assertFalse(reservations.release("lane-a"))

    def test_reacquiring_same_owner_replaces_its_claim_instead_of_double_counting(self):
        with tempfile.TemporaryDirectory() as directory:
            reservations = self.make_reservations(directory, reserve=100)
            with patch.object(locks.shutil, "disk_usage", return_value=disk_usage(400)):
                self.assertTrue(reservations.acquire("lane", 100, [], lambda: False))
                self.assertTrue(reservations.acquire("lane", 100, [], lambda: False))
            self.assertEqual(set(reservations._read()), {"lane"})

    def test_existing_owned_file_size_reduces_future_demand(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owned = root / "backup.part"
            owned.write_bytes(b"x" * 100)
            reservations = self.make_reservations(directory, reserve=50)
            # 3*100 - 100 owned bytes + 50 reserve = 250.
            with patch.object(locks.shutil, "disk_usage", return_value=disk_usage(250)):
                self.assertTrue(reservations.acquire("lane", 100, [owned], lambda: False))

    def test_other_claims_are_counted_and_cancelled_wait_does_not_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            reservations = self.make_reservations(directory, reserve=100)
            with patch.object(locks.shutil, "disk_usage", return_value=disk_usage(400)):
                self.assertTrue(reservations.acquire("first", 100, [], lambda: False))

            cancelled = Event()
            with patch.object(locks.shutil, "disk_usage", return_value=disk_usage(500)), patch.object(
                locks.time, "sleep", side_effect=lambda _seconds: cancelled.set()
            ):
                self.assertFalse(reservations.acquire("second", 50, [], cancelled.is_set))
            self.assertEqual(set(reservations._read()), {"first"})

    def test_only_definitely_dead_pid_is_reaped(self):
        with tempfile.TemporaryDirectory() as directory:
            root_file = Path(directory) / "reservations.json"
            root_file.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "reservations": {
                            "dead": {"owner": "dead", "pid": 222, "original_bytes": 10, "paths": []},
                            "unknown": {
                                "owner": "unknown",
                                "pid": 333,
                                "original_bytes": 20,
                                "paths": [],
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            reservations = locks.DiskReservations(
                root_file,
                directory,
                reserve_bytes=0,
                pid_liveness=lambda pid: False if pid == 222 else None,
                poll_interval=0.001,
            )
            with patch.object(locks.shutil, "disk_usage", return_value=disk_usage(1000)):
                self.assertTrue(reservations.acquire("new", 1, [], lambda: False))
            self.assertEqual(set(reservations._read()), {"unknown", "new"})

    def test_atomic_replace_retries_transient_permission_error(self):
        with tempfile.TemporaryDirectory() as directory:
            reservations = self.make_reservations(directory, replace_retries=4)
            real_replace = os.replace
            attempts = 0

            def flaky_replace(source, destination):
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise PermissionError("temporary sharing violation")
                return real_replace(source, destination)

            with patch.object(locks.shutil, "disk_usage", return_value=disk_usage(3)), patch.object(
                locks.os, "replace", side_effect=flaky_replace
            ), patch.object(locks.time, "sleep"):
                self.assertTrue(reservations.acquire("lane", 1, [], lambda: False))
            self.assertEqual(attempts, 3)
            self.assertTrue((Path(directory) / "reservations.json").exists())


if __name__ == "__main__":
    unittest.main()
