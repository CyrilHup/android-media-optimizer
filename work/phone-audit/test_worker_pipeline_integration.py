"""Isolated integration checks for the phone worker's pipeline functions.

Only selected AST definitions are executed in a controlled namespace. The
worker module itself is never imported, so its phone/service startup cannot run.
"""

from __future__ import annotations

import ast
from copy import deepcopy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from compression_eta import CompressionETA
from phone_backup_prefetch import PhoneBackupPrefetch
from phone_transfer_pipeline import PhoneTransferPipeline


ROOT = Path(__file__).resolve().parents[2]
WORKER_PATH = ROOT / "work" / "phone-audit" / "compress_phone_video_batches.py"
EXTRACT_NAMES = {
    "PauseRequested",
    "BackgroundTransferError",
    "reconcile_completed_transaction_receipts",
    "collect_transfer",
    "close_pipeline",
    "check_control",
    "maybe_prefetch",
}


def extract_worker_definitions(namespace: dict[str, Any]) -> dict[str, Any]:
    """Execute only selected pure function/class definitions, never module code."""

    tree = ast.parse(WORKER_PATH.read_text(encoding="utf-8"), filename=str(WORKER_PATH))
    selected = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name in EXTRACT_NAMES
    ]
    found = {node.name for node in selected}
    missing = EXTRACT_NAMES - found
    if missing:
        raise AssertionError(f"worker integration definitions missing: {sorted(missing)}")
    isolated = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(isolated)
    exec(compile(isolated, str(WORKER_PATH), "exec"), namespace)
    return namespace


class ManualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class WorkerPipelineIntegrationTests(unittest.TestCase):
    def make_namespace(self, **overrides: Any) -> dict[str, Any]:
        receipts: dict[str, Any] = {"records": []}
        receipt_writes: list[dict[str, Any]] = []
        updates: list[dict[str, Any]] = []
        state = overrides.pop(
            "state",
            {"status": "running", "phase": "Encoding 1080p copy", "error": None},
        )

        def finish_record(record: dict[str, Any], status: str, **values: Any) -> None:
            record.update(status=status, **values)
            receipts["records"].append(record)
            write_json("receipts", receipts)

        def write_json(path: Any, value: Any) -> None:
            if path == "receipts":
                receipt_writes.append(json.loads(json.dumps(value)))

        def update(**values: Any) -> None:
            state.update(values)
            updates.append(dict(values))

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        namespace: dict[str, Any] = {
            "LANE": None,
            "json": json,
            "deepcopy": deepcopy,
            "time": time,
            "Path": Path,
            "PauseRequested": type("PauseRequestedPlaceholder", (Exception,), {}),
            "BackgroundTransferError": type("BackgroundTransferErrorPlaceholder", (RuntimeError,), {}),
            "transfer_pipeline": None,
            "transfer_record": None,
            "transfer_started": None,
            "transfer_error": None,
            "backup_prefetch": None,
            "prefetch_row": None,
            "eta_tracker": None,
            "ETA_HISTORY": Path(temp.name) / "eta-history.jsonl",
            "RECEIPTS": "receipts",
            "state": state,
            "finish_record": finish_record,
            "write_json": write_json,
            "print": lambda *_args, **_kwargs: None,
            "update": update,
            "command": lambda: "run",
            "receipts": receipts,
            "receipt_writes": receipt_writes,
            "updates": updates,
        }
        namespace.update(overrides)
        return extract_worker_definitions(namespace)

    def cleanup_transfer_pipeline(self, namespace: dict[str, Any]) -> None:
        pipeline = namespace.get("transfer_pipeline")
        if pipeline is not None:
            try:
                pipeline.close()
            except BaseException:
                pass

    def run_startup_reconciliation(self, namespace: dict[str, Any], transactions: list[dict[str, Any]]) -> None:
        namespace["latest_receipts"] = {
            record["phone_path"]: record for record in namespace["receipts"]["records"]
        }
        namespace["transactions"] = {"transactions": transactions}
        namespace.setdefault("assigned_paths", None)
        tree = ast.parse(WORKER_PATH.read_text(encoding="utf-8"), filename=str(WORKER_PATH))
        branch = next(
            node
            for node in tree.body
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Call)
            and isinstance(node.test.func, ast.Name)
            and node.test.func.id == "reconcile_completed_transaction_receipts"
        )
        selection = next(node for node in tree.body if isinstance(node, ast.Assign)
                         and any(isinstance(target, ast.Name) and target.id == 'reconcilable' for target in node.targets))
        isolated = ast.Module(body=[selection, branch], type_ignores=[])
        ast.fix_missing_locations(isolated)
        exec(compile(isolated, str(WORKER_PATH), "exec"), namespace)

    def test_startup_reconciliation_is_limited_to_the_lane_assignment(self):
        namespace=self.make_namespace(assigned_paths={'phone/A.mp4'})
        self.run_startup_reconciliation(namespace,[self.completed_transaction('phone/A.mp4'),self.completed_transaction('phone/B.mp4')])
        self.assertEqual([r['phone_path'] for r in namespace['receipts']['records']],['phone/A.mp4'])

    @staticmethod
    def completed_transaction(path: str = "phone/A.mp4") -> dict[str, Any]:
        return {
            "phone_path": path,
            "status": "complete_temporary_copies_removed",
            "phone_hash_verified": True,
            "media_index_updated": True,
            "original_backup_deleted": True,
            "compressed_pc_copy_deleted": True,
            "saved_bytes": 125,
            "nested": {"sha256": "verified"},
        }

    def test_startup_reconciliation_repairs_missing_receipt_once_without_eta(self):
        namespace = self.make_namespace()
        transaction = self.completed_transaction()
        original_transaction = deepcopy(transaction)

        self.run_startup_reconciliation(namespace, [transaction])

        self.assertEqual(len(namespace["receipts"]["records"]), 1)
        repaired = namespace["receipts"]["records"][0]
        self.assertEqual(repaired["status"], "completed")
        self.assertTrue(repaired["recovered_from_transaction_journal"])
        self.assertEqual(repaired["saved_bytes"], 125)
        self.assertTrue(repaired["temporary_backup_deleted"])
        self.assertNotIn("phone_transfer_seconds", repaired)
        self.assertNotIn("processing_timing", repaired)
        self.assertIsNot(repaired, transaction)
        self.assertIsNot(repaired["nested"], transaction["nested"])
        self.assertEqual(transaction, original_transaction)
        self.assertEqual(len(namespace["receipt_writes"]), 1)
        self.assertFalse(namespace["ETA_HISTORY"].exists())

        self.run_startup_reconciliation(namespace, [transaction])
        self.assertEqual(len(namespace["receipts"]["records"]), 1)
        self.assertEqual(len(namespace["receipt_writes"]), 1)

    def test_startup_reconciliation_repairs_latest_noncompleted_receipt_only(self):
        namespace = self.make_namespace()
        pending = {"phone_path": "phone/A.mp4", "status": "pending_phone_transfer", "note": "preserve"}
        namespace["receipts"]["records"].append(pending)
        transaction = self.completed_transaction()

        self.run_startup_reconciliation(namespace, [transaction])

        self.assertEqual(pending, {"phone_path": "phone/A.mp4", "status": "pending_phone_transfer", "note": "preserve"})
        self.assertEqual(len(namespace["receipts"]["records"]), 2)
        self.assertEqual(namespace["receipts"]["records"][-1]["status"], "completed")
        self.assertEqual(namespace["latest_receipts"]["phone/A.mp4"]["status"], "completed")
        self.assertEqual(len(namespace["receipt_writes"]), 1)

    def test_startup_reconciliation_does_not_mutate_completed_or_accept_incomplete_rows(self):
        namespace = self.make_namespace()
        completed_receipt = {"phone_path": "phone/already.mp4", "status": "completed", "saved_bytes": 8}
        namespace["receipts"]["records"].append(completed_receipt)
        complete_transaction = self.completed_transaction("phone/already.mp4")
        incomplete = self.completed_transaction("phone/incomplete.mp4")
        incomplete["phone_hash_verified"] = False
        unindexed = self.completed_transaction("phone/unindexed.mp4")
        unindexed["media_index_updated"] = False
        backup_retained = self.completed_transaction("phone/backup.mp4")
        backup_retained["original_backup_deleted"] = False
        compressed_retained = self.completed_transaction("phone/compressed.mp4")
        compressed_retained["compressed_pc_copy_deleted"] = False
        superseded = self.completed_transaction("phone/superseded.mp4")
        newer_incomplete = {"phone_path": "phone/superseded.mp4", "status": "replaced_verifying"}
        original_receipt = deepcopy(completed_receipt)
        original_transactions = deepcopy(
            [complete_transaction, incomplete, unindexed, backup_retained, compressed_retained, superseded, newer_incomplete]
        )

        self.run_startup_reconciliation(
            namespace,
            [complete_transaction, incomplete, unindexed, backup_retained, compressed_retained, superseded, newer_incomplete],
        )

        self.assertEqual(namespace["receipts"]["records"], [completed_receipt])
        self.assertEqual(completed_receipt, original_receipt)
        self.assertEqual(
            [complete_transaction, incomplete, unindexed, backup_retained, compressed_retained, superseded, newer_incomplete],
            original_transactions,
        )
        self.assertEqual(namespace["receipt_writes"], [])

    def test_successful_transfer_writes_once_without_resetting_next_file_eta(self):
        clock = ManualClock()
        eta = CompressionETA([], 5, "test+pipeline", clock)
        eta.begin_file("phone/A.mp4", 100)
        clock.now = 1
        eta.observe("Encoding 1080p copy", "running")
        clock.now = 3
        deferred_a = eta.finish_file("pending_phone_transfer", defer_transfer=True)
        assert deferred_a is not None

        eta.begin_file("phone/B.mp4", 200)
        clock.now = 4
        eta.observe("Encoding 1080p copy", "running")
        current_b_before = (eta.path, eta.original_bytes, eta.active_seconds, eta.phase_elapsed)

        started = threading.Event()
        release = threading.Event()
        pipeline: PhoneTransferPipeline[dict[str, Any], dict[str, Any]] = PhoneTransferPipeline()
        namespace = self.make_namespace(eta_tracker=eta, transfer_pipeline=pipeline)
        self.addCleanup(self.cleanup_transfer_pipeline, namespace)
        record_a = {"phone_path": "phone/A.mp4", "processing_timing": deferred_a}
        namespace["transfer_record"] = record_a

        def transfer(_record: dict[str, Any]) -> dict[str, Any]:
            started.set()
            if not release.wait(2):
                raise TimeoutError("test did not release fake transfer")
            return {"saved_bytes": 50, "original_backup_deleted": True}

        pipeline.submit(record_a, transfer)
        self.assertTrue(started.wait(1))
        release.set()

        deadline = time.monotonic() + 2
        while namespace["transfer_record"] is not None and time.monotonic() < deadline:
            namespace["collect_transfer"]()
            if namespace["transfer_record"] is not None:
                time.sleep(0.001)
        self.assertIsNone(namespace["transfer_record"])
        namespace["collect_transfer"]()

        self.assertEqual(len(namespace["receipts"]["records"]), 1)
        self.assertEqual(namespace["receipts"]["records"][0]["status"], "completed")
        self.assertEqual(namespace["receipts"]["records"][0]["saved_bytes"], 50)
        self.assertEqual(len(namespace["receipt_writes"]), 1)
        self.assertEqual(eta.path, "phone/B.mp4")
        self.assertEqual(eta.original_bytes, 200)
        self.assertEqual(
            (eta.path, eta.original_bytes, eta.active_seconds, eta.phase_elapsed),
            current_b_before,
        )
        self.assertEqual(eta.history[-1]["phone_path"], "phone/A.mp4")
        self.assertEqual(eta.history[-1]["status"], "completed")
        self.assertTrue(namespace["ETA_HISTORY"].exists())
        self.assertEqual(len(namespace["ETA_HISTORY"].read_text(encoding="utf-8").splitlines()), 1)

    def test_failed_background_transfer_sets_error_and_control_check_raises(self):
        started = threading.Event()
        failure = OSError("wireless transfer disconnected")
        pipeline: PhoneTransferPipeline[dict[str, Any], dict[str, Any]] = PhoneTransferPipeline()
        command_reads: list[str] = []
        namespace = self.make_namespace(
            transfer_pipeline=pipeline,
            command=lambda: command_reads.append("read") or "run",
        )
        self.addCleanup(self.cleanup_transfer_pipeline, namespace)
        namespace["transfer_record"] = {"phone_path": "phone/A.mp4"}

        def transfer(_record: dict[str, Any]) -> dict[str, Any]:
            started.set()
            raise failure

        pipeline.submit(namespace["transfer_record"], transfer)
        self.assertTrue(started.wait(1))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                pipeline.raise_if_failed()
            except OSError as error:
                self.assertIs(error, failure)
                break
            time.sleep(0.001)
        else:
            self.fail("transfer failure was not published")

        caught = None
        try:
            namespace["check_control"]()
        except namespace["BackgroundTransferError"] as error:
            caught = error

        self.assertIsNotNone(caught)
        self.assertIn("wireless transfer disconnected", str(caught))
        self.assertEqual(namespace["transfer_error"], str(failure))
        self.assertIsNotNone(namespace["transfer_record"])
        self.assertEqual(namespace["receipt_writes"], [])
        self.assertEqual(command_reads, [])

    def test_close_after_pause_drains_transfer_and_cancels_prefetch(self):
        events: list[str] = []
        prefetch_started = threading.Event()
        transfer_started = threading.Event()
        transfer_release = threading.Event()
        transfer_pipeline: PhoneTransferPipeline[dict[str, Any], dict[str, Any]] = PhoneTransferPipeline()
        backup_prefetch: PhoneBackupPrefetch[str, str] = PhoneBackupPrefetch()
        state = {"status": "paused", "phase": "Processing paused safely; backup retained", "error": None}

        def prefetch(cancel_event: threading.Event) -> str:
            prefetch_started.set()
            if cancel_event.wait(2):
                events.append("prefetch_cancelled")
                return "cancelled at safe point"
            return "unexpected timeout"

        def transfer(_record: dict[str, Any]) -> dict[str, Any]:
            transfer_started.set()
            if not transfer_release.wait(2):
                raise TimeoutError("test did not release transaction")
            events.append("transfer_committed")
            return {"saved_bytes": 20, "original_backup_deleted": True}

        backup_prefetch.start("phone/B.mp4", prefetch)
        self.assertTrue(prefetch_started.wait(1))
        record = {"phone_path": "phone/A.mp4"}
        transfer_pipeline.submit(record, transfer)
        self.assertTrue(transfer_started.wait(1))

        namespace = self.make_namespace(
            transfer_pipeline=transfer_pipeline,
            transfer_record=record,
            backup_prefetch=backup_prefetch,
            prefetch_row={"path": "phone/B.mp4", "bytes": 500},
            state=state,
        )
        self.addCleanup(self.cleanup_transfer_pipeline, namespace)
        release_timer = threading.Timer(0.05, transfer_release.set)
        release_timer.start()
        namespace["close_pipeline"]()
        release_timer.join(timeout=1)

        self.assertEqual(events[0], "prefetch_cancelled")
        self.assertEqual(events[-1], "transfer_committed")
        self.assertFalse(backup_prefetch.busy)
        self.assertFalse(transfer_pipeline.busy)
        self.assertIsNone(namespace["prefetch_row"])
        self.assertEqual(state["status"], "paused")
        self.assertEqual(state["phase"], "Processing paused safely; backup retained")
        self.assertEqual(len(namespace["receipts"]["records"]), 1)

    def test_prefetch_is_not_started_while_a_phone_transfer_owns_the_slot(self):
        class PrefetchSpy:
            busy = False

            def __init__(self) -> None:
                self.starts = 0

            def start(self, *_args: Any, **_kwargs: Any) -> None:
                self.starts += 1

        spy = PrefetchSpy()
        row = {"path": "phone/B.mp4", "bytes": 100}
        namespace = self.make_namespace(
            backup_prefetch=spy,
            prefetch_row=None,
            transfer_record={"phone_path": "phone/A.mp4"},
            transfer_error=None,
            current_queue_index=0,
            pending=[{"path": "phone/A.mp4", "bytes": 200}, row],
            state={"phase": "Encoding 1080p copy"},
        )

        namespace["maybe_prefetch"]()

        self.assertEqual(spy.starts, 0)
        self.assertIsNone(namespace["prefetch_row"])


if __name__ == "__main__":
    unittest.main()
