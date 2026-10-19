"""Failure-injection tests for durable single-photo publication.

These tests use only an in-memory fake phone and tiny synthetic byte strings.
They never invoke ADB or inspect personal media.
"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import tempfile
import unittest
from unittest.mock import patch

import photo_transaction as transaction


class SimulatedInterruption(RuntimeError):
    pass


class FakePhone:
    """Small MediaStore/filesystem model implementing photo_transaction's API."""

    SOURCE = "/storage/emulated/0/Pictures/sample.jpg"
    TARGET = "/storage/emulated/0/Pictures/sample.webp"

    def __init__(self, source_bytes=b"source-image", target_path=None):
        self.target_path = target_path or self.TARGET
        self.files = {self.SOURCE: source_bytes}
        self.times = {self.SOURCE: 1234567890}
        self.rows = {
            self.SOURCE: {
                "_id": 17,
                "_size": len(source_bytes),
                "date_modified": 1234567890,
                "datetaken": 1700000000123,
                "date_added": 1700000100,
                "width": 12,
                "height": 8,
                "orientation": 0,
                "mime_type": "image/jpeg",
            }
        }
        self.next_id = 18
        self.fail_gallery = False
        self.interrupt_after_move = None
        self.interrupt_after_remove = False
        self.moves = 0
        self.removals = 0

    def exists(self, path):
        return path in self.files

    def stat(self, path):
        payload = self.files[path]
        return len(payload), self.times[path]

    def sha(self, path):
        return hashlib.sha256(self.files[path]).hexdigest()

    def media(self, path):
        row = self.rows.get(path)
        if row is None:
            return "No result found."
        return "Row: 0 " + ", ".join(f"{key}={value}" for key, value in row.items())

    def scan(self, path):
        if path not in self.files:
            return
        # Model the media scanner using the file's preserved timestamp and the
        # original capture date. A failure mode below simulates a wrong index.
        if path not in self.rows or path != self.SOURCE:
            self.rows[path] = {
                "_id": self.next_id,
                "_size": len(self.files[path]),
                "date_modified": self.times[path],
                "datetaken": 1700000000123,
                "date_added": 1800000000,
                "width": 12,
                "height": 8,
                "orientation": 0,
                "mime_type": "image/jpeg" if path.lower().endswith(".jpg") else "image/webp",
            }
            self.next_id += 1
        else:
            row = self.rows[path]
            row.update(_size=len(self.files[path]), date_modified=self.times[path])
        if self.fail_gallery and path == self.target_path:
            self.rows[path]["datetaken"] += 999

    def push(self, source, target):
        self.files[target] = Path(source).read_bytes()
        self.times[target] = self.times.get(self.SOURCE, 0)

    def move(self, source, target):
        # Match `mv -n`: retain an existing target and leave source in place.
        if source not in self.files:
            raise FileNotFoundError(source)
        if target not in self.files:
            self.files[target] = self.files.pop(source)
            self.times[target] = self.times.pop(source)
            if source in self.rows and source != self.SOURCE:
                self.rows.pop(source)
        self.moves += 1
        if self.interrupt_after_move == self.moves:
            self.interrupt_after_move = None
            raise SimulatedInterruption("power loss after phone rename")

    def remove(self, path):
        self.files.pop(path, None)
        self.times.pop(path, None)
        self.removals += 1
        if self.interrupt_after_remove:
            self.interrupt_after_remove = False
            raise SimulatedInterruption("power loss after hidden original removal")

    def touch(self, path, mtime):
        self.times[path] = mtime

    def free(self):
        return 20 * 1024**3

    def delete_stale_row(self, path, identifier):
        if path in self.files:
            raise ValueError("source unexpectedly exists during stale index cleanup")
        current = transaction.fields(self.media(path))
        if current and current.get("_id") != identifier:
            raise ValueError("source gallery ID changed")
        if current:
            self.rows.pop(path, None)


class PhotoTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.out = self.root / "outputs/phone-photo-compression-batches"
        (self.out / "temporary").mkdir(parents=True)
        self.backup = self.out / "temporary/source.backup"
        self.compressed = self.out / "temporary/candidate.webp"
        self.original_bytes = b"source-image"
        self.candidate_bytes = b"synthetic-webp"
        self.backup.write_bytes(self.original_bytes)
        self.compressed.write_bytes(self.candidate_bytes)
        self.phone = FakePhone(self.original_bytes)
        self.row = self.make_row(self.phone.TARGET, "image/webp")

    def tearDown(self):
        self.temp.cleanup()

    def make_row(self, target, mime):
        return {
            "phone_path": self.phone.SOURCE,
            "target_phone_path": target,
            "backup_path": str(self.backup),
            "compressed_path": str(self.compressed),
            "original_sha256": hashlib.sha256(self.original_bytes).hexdigest(),
            "compressed_sha256": hashlib.sha256(self.candidate_bytes).hexdigest(),
            "original_bytes": len(self.original_bytes),
            "compressed_bytes": len(self.candidate_bytes),
            "mtime": 1234567890,
            "source_media_row": self.phone.media(self.phone.SOURCE),
            "output_resolution": [12, 8],
            "output_mime": mime,
        }

    def apply(self):
        with patch.object(transaction.time, "sleep", return_value=None):
            return transaction.apply_photo(self.root, self.row, self.phone)

    def journal_record(self):
        path = self.out / "transactions.json"
        return json.loads(path.read_text(encoding="utf-8"))["records"][self.phone.SOURCE]

    def test_webp_replacement_verifies_gallery_then_removes_original(self):
        result = self.apply()

        self.assertFalse(self.phone.exists(self.phone.SOURCE))
        self.assertEqual(self.phone.files[self.phone.TARGET], self.candidate_bytes)
        self.assertFalse(self.phone.exists(result["hidden_original"]))
        self.assertEqual(self.phone.rows[self.phone.TARGET]["datetaken"], 1700000000123)
        self.assertEqual(self.phone.rows[self.phone.TARGET]["date_modified"], 1234567890)
        self.assertNotIn(self.phone.SOURCE, self.phone.rows)
        self.assertFalse(self.backup.exists())
        self.assertFalse(self.compressed.exists())
        self.assertEqual(result["status"], "complete")
        self.assertTrue(result["phone_original_deleted"])
        self.assertEqual(result["saved_bytes"], len(self.original_bytes) - len(self.candidate_bytes))

    def test_same_path_lossless_hdr_replacement(self):
        self.row = self.make_row(self.phone.SOURCE, "image/jpeg")
        result = self.apply()

        self.assertEqual(self.phone.files[self.phone.SOURCE], self.candidate_bytes)
        self.assertFalse(self.phone.exists(result["hidden_original"]))
        self.assertEqual(self.phone.rows[self.phone.SOURCE]["mime_type"], "image/jpeg")
        self.assertEqual(self.phone.rows[self.phone.SOURCE]["datetaken"], 1700000000123)
        self.assertTrue(result["phone_original_deleted"])
        self.assertEqual(result["status"], "complete")

    def test_existing_destination_collision_preserves_both_files(self):
        self.phone.files[self.phone.TARGET] = b"unrelated-existing-photo"
        with self.assertRaisesRegex(ValueError, "destination preserved"):
            self.apply()

        self.assertEqual(self.phone.files[self.phone.SOURCE], self.original_bytes)
        self.assertEqual(self.phone.files[self.phone.TARGET], b"unrelated-existing-photo")
        self.assertTrue(self.backup.exists() and self.compressed.exists())

    def test_candidate_hash_failure_preserves_original_and_backups(self):
        self.compressed.write_bytes(b"corrupted candidate")
        with self.assertRaisesRegex(ValueError, "copy changed"):
            self.apply()

        self.assertEqual(self.phone.files[self.phone.SOURCE], self.original_bytes)
        self.assertFalse(self.phone.exists(self.phone.TARGET))
        self.assertTrue(self.backup.exists() and self.compressed.exists())

    def test_gallery_metadata_mismatch_retains_phone_and_local_backups(self):
        self.phone.fail_gallery = True
        with self.assertRaisesRegex(ValueError, "gallery size/date/dimensions"):
            self.apply()

        self.assertEqual(self.phone.files[self.phone.SOURCE], self.original_bytes)
        self.assertEqual(self.phone.files[self.phone.TARGET], self.candidate_bytes)
        self.assertTrue(self.backup.exists() and self.compressed.exists())
        self.assertFalse(self.phone.exists(self.journal_record()["hidden_original"]))

    def test_resume_after_source_rename_finishes_without_reupload(self):
        # On a WebP transaction, move 1 installs the stage as destination;
        # move 2 renames the original to its hidden backup. Interrupt after 2.
        self.phone.interrupt_after_move = 2
        with self.assertRaises(SimulatedInterruption):
            self.apply()
        self.assertTrue(self.phone.exists(self.journal_record()["hidden_original"]))
        self.assertFalse(self.phone.exists(self.phone.SOURCE))

        result = self.apply()
        self.assertEqual(result["status"], "complete")
        self.assertFalse(self.phone.exists(result["hidden_original"]))
        self.assertEqual(self.phone.files[self.phone.TARGET], self.candidate_bytes)
        self.assertFalse(self.backup.exists() or self.compressed.exists())

    def test_resume_after_hidden_original_remove_was_interrupted(self):
        self.phone.interrupt_after_remove = True
        with self.assertRaises(SimulatedInterruption):
            self.apply()
        record = self.journal_record()
        self.assertEqual(record["status"], "ready_to_remove_hidden_original")
        self.assertFalse(self.phone.exists(record["hidden_original"]))
        self.assertTrue(self.backup.exists() and self.compressed.exists())

        result = self.apply()
        self.assertEqual(result["status"], "complete")
        self.assertTrue(result["phone_original_deleted"])
        self.assertEqual(self.phone.files[self.phone.TARGET], self.candidate_bytes)
        self.assertFalse(self.backup.exists() or self.compressed.exists())

    def test_resume_after_partial_local_cleanup(self):
        real_unlink = Path.unlink

        def unlink_once(path, *args, **kwargs):
            if path == self.compressed:
                raise SimulatedInterruption("power loss after deleting verified backup")
            return real_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", unlink_once):
            with self.assertRaises(SimulatedInterruption):
                self.apply()
        record = self.journal_record()
        self.assertTrue(record["phone_original_deleted"])
        self.assertFalse(self.backup.exists())
        self.assertTrue(self.compressed.exists())
        self.assertEqual(self.phone.files[self.phone.TARGET], self.candidate_bytes)

        result = self.apply()
        self.assertEqual(result["status"], "complete")
        self.assertFalse(self.compressed.exists())
        self.assertEqual(self.phone.files[self.phone.TARGET], self.candidate_bytes)


if __name__ == "__main__":
    unittest.main()
