"""Safety tests for the single-slot background phone-transfer coordinator."""

from __future__ import annotations

import threading
import time
import unittest

from phone_transfer_pipeline import PhoneTransferPipeline


class PhoneTransferPipelineTests(unittest.TestCase):
    def test_pending_transfer_allows_local_work_and_rejects_a_second_submit(self):
        entered = threading.Event()
        release = threading.Event()
        pipeline: PhoneTransferPipeline[dict, str] = PhoneTransferPipeline()
        self.addCleanup(pipeline.close)

        def transfer(record: dict) -> str:
            entered.set()
            self.assertTrue(release.wait(2))
            record["worker_mutation"] = True
            return record["name"]

        record = {"name": "first", "nested": {"value": 1}}
        pipeline.submit(record, transfer)
        self.assertTrue(entered.wait(1))

        # The owner thread can perform independent local work during the I/O.
        local_started = time.monotonic()
        local_value = sum(range(10_000))
        self.assertGreater(local_value, 0)
        self.assertLess(time.monotonic() - local_started, 0.5)
        self.assertTrue(pipeline.busy)
        with self.assertRaisesRegex(RuntimeError, "slot is occupied"):
            pipeline.submit({"name": "second"}, lambda item: item["name"])

        record["nested"]["value"] = 9
        release.set()
        result = pipeline.wait()
        assert result is not None
        self.assertEqual(result.record, {"name": "first", "nested": {"value": 1}})
        self.assertEqual(result.result, "first")
        self.assertGreaterEqual(result.elapsed_seconds, 0)
        self.assertLessEqual(result.started_monotonic, result.completed_monotonic)
        self.assertLessEqual(result.submitted_monotonic, result.started_monotonic)
        self.assertAlmostEqual(
            result.elapsed_seconds,
            result.completed_monotonic - result.started_monotonic,
            places=6,
        )
        self.assertFalse(pipeline.busy)

    def test_poll_returns_none_until_ready_then_consumes_success_exactly_once(self):
        entered = threading.Event()
        release = threading.Event()
        pipeline: PhoneTransferPipeline[dict, int] = PhoneTransferPipeline()
        self.addCleanup(pipeline.close)

        def transfer(record: dict) -> int:
            entered.set()
            self.assertTrue(release.wait(2))
            return record["value"]

        pipeline.submit({"value": 7}, transfer)
        self.assertTrue(entered.wait(1))
        self.assertIsNone(pipeline.poll())
        release.set()
        result = pipeline.wait()
        assert result is not None
        self.assertEqual(result.result, 7)
        self.assertIsNone(pipeline.poll())
        self.assertIsNone(pipeline.wait())

    def test_raise_if_failed_does_not_consume_a_success(self):
        entered = threading.Event()
        release = threading.Event()
        pipeline: PhoneTransferPipeline[dict, str] = PhoneTransferPipeline()
        self.addCleanup(pipeline.close)

        def transfer(record: dict) -> str:
            entered.set()
            self.assertTrue(release.wait(2))
            return record["value"]

        pipeline.submit({"value": "verified"}, transfer)
        self.assertTrue(entered.wait(1))
        pipeline.raise_if_failed()
        release.set()
        result = pipeline.wait()
        assert result is not None
        self.assertEqual(result.result, "verified")

    def test_failure_is_repeated_and_permanently_blocks_new_submissions(self):
        failure = ValueError("transaction failed")
        pipeline: PhoneTransferPipeline[dict, str] = PhoneTransferPipeline()

        def fail(_record: dict) -> str:
            raise failure

        pipeline.submit({"id": "bad"}, fail)
        with self.assertRaises(ValueError) as first:
            pipeline.wait()
        self.assertIs(first.exception, failure)
        self.assertTrue(pipeline.failed)
        self.assertTrue(pipeline.busy)

        with self.assertRaises(ValueError) as second:
            pipeline.poll()
        self.assertIs(second.exception, failure)
        with self.assertRaises(ValueError) as third:
            pipeline.raise_if_failed()
        self.assertIs(third.exception, failure)
        with self.assertRaises(ValueError) as fourth:
            pipeline.submit({"id": "next"}, lambda item: item["id"])
        self.assertIs(fourth.exception, failure)
        with self.assertRaises(ValueError) as fifth:
            pipeline.close()
        self.assertIs(fifth.exception, failure)

    def test_close_drains_active_transfer_and_returns_its_result(self):
        entered = threading.Event()
        release = threading.Event()
        pipeline: PhoneTransferPipeline[dict, str] = PhoneTransferPipeline()

        def transfer(record: dict) -> str:
            entered.set()
            self.assertTrue(release.wait(2))
            return "committed:" + record["id"]

        pipeline.submit({"id": "safe"}, transfer)
        self.assertTrue(entered.wait(1))
        timer = threading.Timer(0.05, release.set)
        timer.start()
        started = time.monotonic()
        result = pipeline.close()
        elapsed = time.monotonic() - started
        timer.join(timeout=1)

        assert result is not None
        self.assertEqual(result.result, "committed:safe")
        self.assertGreaterEqual(elapsed, 0.03)
        self.assertFalse(pipeline.busy)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            pipeline.submit({"id": "later"}, lambda item: item["id"])
        self.assertIsNone(pipeline.poll())

    def test_close_drains_active_failure_then_reraises_original_error(self):
        entered = threading.Event()
        release = threading.Event()
        failure = RuntimeError("commit verification failed")
        pipeline: PhoneTransferPipeline[dict, str] = PhoneTransferPipeline()

        def transfer(_record: dict) -> str:
            entered.set()
            self.assertTrue(release.wait(2))
            raise failure

        pipeline.submit({"id": "active"}, transfer)
        self.assertTrue(entered.wait(1))
        timer = threading.Timer(0.05, release.set)
        timer.start()
        started = time.monotonic()
        with self.assertRaises(RuntimeError) as caught:
            pipeline.close()
        elapsed = time.monotonic() - started
        timer.join(timeout=1)

        self.assertIs(caught.exception, failure)
        self.assertGreaterEqual(elapsed, 0.03)
        self.assertTrue(pipeline.failed)
        with self.assertRaises(RuntimeError) as later:
            pipeline.submit({"id": "next"}, lambda item: item["id"])
        self.assertIs(later.exception, failure)


if __name__ == "__main__":
    unittest.main()
