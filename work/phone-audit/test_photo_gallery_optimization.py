"""Regression tests for reduced MediaStore work during photo replacement.

The tests reuse the tiny in-memory FakePhone and synthetic bytes from
test_photo_transaction.py. They never invoke ADB or inspect personal media.
"""
import unittest

import test_photo_transaction as support

FakePhone = support.FakePhone


class GalleryRacePhone(FakePhone):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.deleted_ids = []
        self.drift_source_id_before_cleanup = False
        self.target_id_override = None
        self.remove_source_index_on_hide = False
        self.tamper_target_after_hidden_remove = False

    def scan(self, path):
        super().scan(path)
        if path == self.target_path and self.target_id_override is not None:
            self.rows[path]["_id"] = self.target_id_override

    def move(self, source, target):
        super().move(source, target)
        if (self.remove_source_index_on_hide and source == self.SOURCE
                and ".codex-photo-original-" in target):
            self.rows.pop(self.SOURCE, None)

    def delete_stale_row(self, path, identifier):
        if self.drift_source_id_before_cleanup and path == self.SOURCE:
            if path in self.rows:
                self.rows[path]["_id"] = identifier + 1000
        before = self.rows.get(path)
        super().delete_stale_row(path, identifier)
        if before is not None and path not in self.rows:
            self.deleted_ids.append(identifier)

    def remove(self, path):
        super().remove(path)
        if (self.tamper_target_after_hidden_remove
                and ".codex-photo-original-" in path):
            self.files[self.target_path] = b"tampered-after-original-cleanup"


class PhotoGalleryOptimizationTests(unittest.TestCase):
    """Keep the shared tiny-file fixture while injecting gallery races."""

    make_row = support.PhotoTransactionTests.make_row
    apply = support.PhotoTransactionTests.apply
    journal_record = support.PhotoTransactionTests.journal_record

    def setUp(self):
        support.PhotoTransactionTests.setUp(self)
        self.phone = GalleryRacePhone(self.original_bytes)
        self.row = self.make_row(self.phone.TARGET, "image/webp")

    def tearDown(self):
        support.PhotoTransactionTests.tearDown(self)

    def test_changed_source_gallery_id_is_never_deleted(self):
        self.phone.drift_source_id_before_cleanup = True

        with self.assertRaisesRegex(ValueError, "Gallery identity conflict|source gallery ID changed"):
            self.apply()

        # The changed row represents a different MediaStore entry. Keep it and
        # both recoverable original copies instead of deleting by stale ID.
        self.assertEqual(self.phone.rows[self.phone.SOURCE]["_id"], 1017)
        self.assertEqual(self.phone.deleted_ids, [])
        self.assertTrue(self.backup.exists())
        self.assertTrue(self.phone.exists(self.journal_record()["hidden_original"]))

    def test_target_reusing_old_source_id_is_rejected(self):
        self.phone.target_id_override = self.phone.rows[self.phone.SOURCE]["_id"]

        with self.assertRaisesRegex(ValueError, "Gallery identity conflict"):
            self.apply()

        self.assertTrue(self.backup.exists())
        self.assertEqual(self.phone.files[self.phone.TARGET], self.candidate_bytes)
        self.assertTrue(self.phone.exists(self.journal_record()["hidden_original"]))
        self.assertEqual(self.phone.deleted_ids, [])

    def test_target_tampered_after_cleanup_keeps_local_original_backup(self):
        self.phone.tamper_target_after_hidden_remove = True

        with self.assertRaisesRegex(ValueError, "Photo target hash or file date mismatch"):
            self.apply()

        # Even though the phone-side transaction crossed the rename boundary,
        # the local original remains the recovery copy until the final target
        # hash/stat check succeeds.
        self.assertEqual(self.backup.read_bytes(), self.original_bytes)
        self.assertTrue(self.backup.exists())
        self.assertEqual(self.phone.files[self.phone.TARGET], b"tampered-after-original-cleanup")

    def test_already_absent_source_index_is_not_deleted_by_id(self):
        self.phone.remove_source_index_on_hide = True

        result = self.apply()

        self.assertEqual(result["status"], "complete")
        self.assertNotIn(self.phone.SOURCE, self.phone.rows)
        self.assertEqual(self.phone.deleted_ids, [])
        self.assertEqual(self.phone.files[self.phone.TARGET], self.candidate_bytes)


if __name__ == "__main__":
    unittest.main()
