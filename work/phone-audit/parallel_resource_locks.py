"""Crash-released file locks and conservative disk-space reservations.

These helpers coordinate local Windows worker processes. They do not invoke
shell commands, access Android devices, or remove media files.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

try:
    import msvcrt
except ImportError:  # pragma: no cover - production target is Windows
    msvcrt = None  # type: ignore[assignment]


DEFAULT_POLL_SECONDS = 0.2
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_INVALID_PARAMETER = 87
_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def _is_lock_contention(error: OSError) -> bool:
    return error.errno in (11, 13, 36) or getattr(error, "winerror", None) in (32, 33, 36)


@contextmanager
def file_mutex(
    path: str | Path,
    timeout: float = 1800,
    poll: float = DEFAULT_POLL_SECONDS,
) -> Iterator[None]:
    """Take an exclusive, process-wide one-byte Windows file lock.

    The operating system releases the lock if the process exits or crashes.
    Raises ``TimeoutError`` if it cannot acquire the lock before ``timeout``.
    """
    if msvcrt is None:
        raise OSError("file_mutex requires Windows msvcrt byte-range locking")
    if timeout < 0 or poll <= 0:
        raise ValueError("timeout must be nonnegative and poll must be positive")

    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_key = os.path.normcase(os.path.abspath(lock_path))
    with _LOCAL_LOCKS_GUARD:
        local_lock = _LOCAL_LOCKS.setdefault(lock_key, threading.Lock())
    local_acquired = local_lock.acquire(timeout=timeout)
    if not local_acquired:
        raise TimeoutError(f"timed out waiting for file lock: {lock_path}")

    fd = -1
    acquired = False
    deadline = time.monotonic() + timeout
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o666)
        with os.fdopen(fd, "r+b", buffering=0) as lock_file:
            fd = -1
            if os.fstat(lock_file.fileno()).st_size == 0:
                lock_file.seek(0)
                lock_file.write(b"\0")
                lock_file.flush()

            while True:
                lock_file.seek(0)
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError as error:
                    if not _is_lock_contention(error):
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"timed out waiting for file lock: {lock_path}") from error
                    time.sleep(min(poll, remaining))

            try:
                yield
            finally:
                if acquired:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                    acquired = False
    finally:
        if fd >= 0:
            os.close(fd)
        local_lock.release()


def _process_alive(pid: int) -> bool | None:
    """Return False only when the PID is known to have exited or not exist."""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True

    if os.name == "nt":
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        handle = open_process(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False if ctypes.get_last_error() == _ERROR_INVALID_PARAMETER else None
        try:
            code = wintypes.DWORD()
            if not get_exit_code(handle, ctypes.byref(code)):
                return None
            return code.value == _STILL_ACTIVE
        finally:
            close_handle(handle)

    try:  # Makes unit tests and local tooling usable off Windows as well.
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None


class DiskReservations:
    """Coordinate future temporary-space needs for concurrent encode lanes.

    A claim reserves three times the original size, less the sizes of owned
    files that already exist. Unknown PID state is treated as live, so a
    reservation is reaped only after a positive dead-process result.
    """

    def __init__(
        self,
        root_file: str | Path,
        disk_root: str | Path,
        reserve_bytes: int = 8 * 1024**3,
        *,
        pid_liveness: Callable[[int], bool | None] | None = None,
        poll_interval: float = DEFAULT_POLL_SECONDS,
        replace_retries: int = 20,
    ) -> None:
        if reserve_bytes < 0:
            raise ValueError("reserve_bytes must be nonnegative")
        if poll_interval <= 0 or replace_retries < 1:
            raise ValueError("poll_interval and replace_retries must be positive")
        self.root_file = Path(root_file)
        self.disk_root = Path(disk_root)
        self.reserve_bytes = int(reserve_bytes)
        self._pid_liveness = pid_liveness or _process_alive
        self._poll_interval = poll_interval
        self._replace_retries = replace_retries
        self._mutex_path = Path(str(self.root_file) + ".lock")

    @staticmethod
    def _normalize_paths(paths: list[str | Path]) -> list[str]:
        normalized: dict[str, str] = {}
        for raw_path in paths:
            path = Path(raw_path).absolute()
            value = os.fspath(path)
            normalized.setdefault(os.path.normcase(value), value)
        return list(normalized.values())

    def _read(self) -> dict[str, dict[str, object]]:
        try:
            with self.root_file.open("r", encoding="utf-8") as source:
                payload = json.load(source)
        except FileNotFoundError:
            return {}
        if not isinstance(payload, dict) or not isinstance(payload.get("reservations"), dict):
            raise ValueError(f"invalid disk reservation file: {self.root_file}")

        entries: dict[str, dict[str, object]] = {}
        for owner, entry in payload["reservations"].items():
            if (
                not isinstance(owner, str)
                or not owner
                or not isinstance(entry, dict)
                or not isinstance(entry.get("pid"), int)
                or not isinstance(entry.get("original_bytes"), int)
                or entry["original_bytes"] < 0
                or not isinstance(entry.get("paths"), list)
                or any(not isinstance(item, str) for item in entry["paths"])
            ):
                raise ValueError(f"invalid reservation entry for owner {owner!r}")
            entries[owner] = {
                "owner": owner,
                "original_bytes": entry["original_bytes"],
                "paths": list(entry["paths"]),
                "pid": entry["pid"],
            }
        return entries

    def _write(self, entries: dict[str, dict[str, object]]) -> None:
        self.root_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "reservations": entries}
        fd, temp_name = tempfile.mkstemp(
            prefix=self.root_file.name + ".", suffix=".tmp", dir=self.root_file.parent
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as target:
                json.dump(payload, target, indent=2, sort_keys=True)
                target.write("\n")
                target.flush()
                os.fsync(target.fileno())

            for attempt in range(self._replace_retries):
                try:
                    os.replace(temp_path, self.root_file)
                    return
                except PermissionError:
                    if attempt + 1 >= self._replace_retries:
                        raise
                    time.sleep(min(0.05 * (attempt + 1), 0.5))
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    def _existing_owned_bytes(self, paths: list[str]) -> int:
        try:
            root_device = self.disk_root.stat().st_dev
        except OSError:
            root_device = None
        total = 0
        for raw_path in paths:
            try:
                stat = Path(raw_path).stat()
                if not Path(raw_path).is_file():
                    continue
                if root_device is not None and stat.st_dev != root_device:
                    continue
                total += stat.st_size
            except OSError:
                continue
        return total

    def _future_bytes(self, original_bytes: int, paths: list[str]) -> int:
        existing = self._existing_owned_bytes(paths)
        return max(0, 3 * original_bytes - existing)

    def _reap_dead(self, entries: dict[str, dict[str, object]]) -> bool:
        removed = False
        for owner, entry in list(entries.items()):
            alive = self._pid_liveness(int(entry["pid"]))
            if alive is False:
                del entries[owner]
                removed = True
        return removed

    def acquire(
        self,
        owner: str,
        original_bytes: int,
        paths: list[str | Path],
        cancelled: Callable[[], bool],
    ) -> bool:
        """Wait until this lane's conservative disk claim fits, or cancel."""
        if not owner:
            raise ValueError("owner must not be empty")
        if original_bytes < 0:
            raise ValueError("original_bytes must be nonnegative")
        owned_paths = self._normalize_paths(paths)
        owner_future = self._future_bytes(int(original_bytes), owned_paths)

        while True:
            if cancelled():
                return False
            with file_mutex(self._mutex_path):
                entries = self._read()
                changed = self._reap_dead(entries)
                other_future = sum(
                    self._future_bytes(int(entry["original_bytes"]), list(entry["paths"]))
                    for other_owner, entry in entries.items()
                    if other_owner != owner
                )
                required = self.reserve_bytes + other_future + owner_future
                enough_space = shutil.disk_usage(self.disk_root).free >= required
                if enough_space and not cancelled():
                    entries[owner] = {
                        "owner": owner,
                        "original_bytes": int(original_bytes),
                        "paths": owned_paths,
                        "pid": os.getpid(),
                    }
                    changed = True
                    self._write(entries)
                    return True
                if changed:
                    self._write(entries)

            if cancelled():
                return False
            time.sleep(self._poll_interval)

    def release(self, owner: str) -> bool:
        """Remove an owner's claim. Returns whether a claim was present."""
        with file_mutex(self._mutex_path):
            entries = self._read()
            if owner not in entries:
                return False
            del entries[owner]
            self._write(entries)
            return True
