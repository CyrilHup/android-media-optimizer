"""Tests for bounded, cooperative read-only phone-backup prefetching."""

from __future__ import annotations

import threading
import time
import unittest

from phone_backup_prefetch import PhoneBackupPrefetch


class PhoneBackupPrefetchTests(unittest.TestCase):
    def test_prefetch_runs_while_owner_does_local_work_and_slot_is_single(self):
        entered = threading.Event()
        release = threading.Event()
        prefetch: PhoneBackupPrefetch[str, str] = PhoneBackupPrefetch()
        self.addCleanup(prefetch.close)

        def copy(cancel_event: threading.Event) -> str:
            entered.set()
            self.assertFalse(cancel_event.is_set())
            self.assertTrue(release.wait(2))
            return "ready"

        prefetch.start("/phone/next.mp4", copy)
        self.assertTrue(entered.wait(1))
        local_started = time.monotonic()
        self.assertGreater(sum(range(10_000)), 0)
        self.assertLess(time.monotonic() - local_started, 0.5)
        self.assertTrue(prefetch.busy)
        self.assertEqual(prefetch.key, "/phone/next.mp4")
        with self.assertRaisesRegex(RuntimeError, "slot is occupied"):
            prefetch.start("/phone/another.mp4", lambda _cancel: "other")
        release.set()
        result = prefetch.wait_for("/phone/next.mp4")
        assert result is not None
        self.assertEqual(result.result, "ready")
        self.assertIsNone(result.error)
        self.assertFalse(prefetch.busy)

    def test_poll_and_wait_for_consume_completed_result_once(self):
        entered = threading.Event()
        release = threading.Event()
        prefetch: PhoneBackupPrefetch[str, int] = PhoneBackupPrefetch()
        self.addCleanup(prefetch.close)

        def copy(_cancel: threading.Event) -> int:
            entered.set()
            self.assertTrue(release.wait(2))
            return 42

        prefetch.start("batch-a", copy)
        self.assertTrue(entered.wait(1))
        self.assertIsNone(prefetch.poll())
        release.set()
        envelope = prefetch.wait_for("batch-a")
        assert envelope is not None
        self.assertEqual(envelope.result, 42)
        self.assertIsNone(prefetch.poll())
        self.assertIsNone(prefetch.wait_for("batch-a"))

    def test_close_signals_cooperative_cancel_and_joins_worker(self):
        entered = threading.Event()
        finished = threading.Event()
        prefetch: PhoneBackupPrefetch[str, str] = PhoneBackupPrefetch()

        def copy(cancel_event: threading.Event) -> str:
            entered.set()
            if cancel_event.wait(2):
                finished.set()
                return "cancelled safely"
            return "unexpected timeout"

        prefetch.start("batch-b", copy)
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        envelope = prefetch.close()
        elapsed = time.monotonic() - started

        assert envelope is not None
        self.assertEqual(envelope.result, "cancelled safely")
        self.assertTrue(finished.is_set())
        self.assertGreaterEqual(elapsed, 0)
        self.assertFalse(prefetch.busy)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            prefetch.start("later", lambda _cancel: "no")

    def test_operation_failure_is_returned_and_slot_can_be_reused(self):
        failure = OSError("temporary local-copy failure")
        prefetch: PhoneBackupPrefetch[str, str] = PhoneBackupPrefetch()
        self.addCleanup(prefetch.close)

        def fail(_cancel: threading.Event) -> str:
            raise failure

        prefetch.start("bad", fail)
        failed = prefetch.wait_for("bad")
        assert failed is not None
        self.assertIs(failed.error, failure)
        self.assertIsNone(failed.result)
        self.assertFalse(prefetch.busy)

        prefetch.start("good", lambda _cancel: "copied")
        succeeded = prefetch.wait_for("good")
        assert succeeded is not None
        self.assertEqual(succeeded.result, "copied")
        self.assertIsNone(succeeded.error)

    def test_wait_for_mismatched_key_does_not_consume_or_block(self):
        entered = threading.Event()
        release = threading.Event()
        prefetch: PhoneBackupPrefetch[str, str] = PhoneBackupPrefetch()
        self.addCleanup(prefetch.close)

        def copy(_cancel: threading.Event) -> str:
            entered.set()
            self.assertTrue(release.wait(2))
            return "done"

        prefetch.start("expected", copy)
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        self.assertIsNone(prefetch.wait_for("other"))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(prefetch.busy)
        self.assertEqual(prefetch.key, "expected")
        release.set()
        result = prefetch.wait_for("expected")
        assert result is not None
        self.assertEqual(result.result, "done")


if __name__ == "__main__":
    unittest.main()
