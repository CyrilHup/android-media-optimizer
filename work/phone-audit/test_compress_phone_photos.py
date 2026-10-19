"""Synthetic prepare()/approval tests. No ADB process or personal image access."""
from pathlib import Path, PurePosixPath
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import compress_phone_photos as worker


class FakePhone:
    transport = "fake-transport"

    def __init__(self, source_path, payload, width, height, target_exists=False):
        self.files = {source_path: payload}
        self.times = {source_path: 1234567890}
        self.width, self.height = width, height
        self.destination_exists = target_exists

    def exists(self, path):
        return path in self.files or (self.destination_exists and path.endswith(".webp"))

    def stat(self, path):
        return len(self.files[path]), self.times[path]

    def sha(self, path):
        return hashlib.sha256(self.files[path]).hexdigest()

    def media(self, path):
        if path not in self.files:
            return "No result found."
        return ("Row: 0 _id=41, _size=%d, date_modified=1234567890, "
                "datetaken=1700000000123, date_added=1700000100, width=%d, height=%d, "
                "orientation=0, mime_type=image/%s" % (
                    len(self.files[path]), self.width, self.height,
                    "png" if path.lower().endswith(".png") else "jpeg"))


class PhotoPrepareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.out = self.root / "outputs/phone-photo-compression-batches"
        self.out.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def image_source(self, ext, noisy=False):
        # Synthetic pixels only. Random bytes are deterministic and never shown.
        width, height = (512, 384) if noisy else (320, 240)
        image = Image.new("RGB", (width, height))
        if noisy:
            import random
            rng = random.Random(87321)
            image = Image.frombytes("RGB", (width, height), rng.randbytes(width * height * 3))
        else:
            for y in range(height):
                for x in range(width):
                    image.putpixel((x, y), (20 if x < width // 2 else 80,
                                            40 if y < height // 2 else 120, 60))
        path = self.root / ("fixture" + ext)
        if ext == ".png":
            image.save(path, format="PNG", optimize=True)
        else:
            image.save(path, format="JPEG", quality=98, subsampling=0)
        return path, image.size

    def item_and_phone(self, source, size):
        phone_path = "/storage/emulated/0/Pictures/fixture" + source.suffix
        payload = source.read_bytes()
        item = {"path": phone_path, "target_phone_path": str(PurePosixPath(phone_path).with_suffix(".webp")),
                "bytes": len(payload), "mtime": 1234567890}
        return item, FakePhone(phone_path, payload, *size)

    def approval_files(self, records, queue_bytes=None, approved=True):
        queue_bytes = queue_bytes if queue_bytes is not None else json.dumps(
            {"records": records}, separators=(",", ":")).encode()
        (self.out / "queue.json").write_bytes(queue_bytes)
        (self.out / "bulk-authorization.json").write_text(json.dumps({
            "approved": approved,
            "scope": "replace_eligible_public_photos",
            "queue_sha256": hashlib.sha256(queue_bytes).hexdigest(),
        }), encoding="utf-8")

    def test_bulk_approval_is_bound_to_exact_queue_bytes(self):
        item = {"path": "/storage/emulated/0/Pictures/a.jpg",
                "target_phone_path": "/storage/emulated/0/Pictures/a.webp"}
        original = json.dumps({"records": [item]}, separators=(",", ":")).encode()
        self.approval_files([item], queue_bytes=original)
        with patch.object(worker, "OUT", self.out):
            self.assertEqual(worker.require_approval(), [item])

        changed = json.dumps({"records": [{**item, "bytes": 1}]}, separators=(",", ":")).encode()
        (self.out / "queue.json").write_bytes(changed)
        with patch.object(worker, "OUT", self.out):
            with self.assertRaises(PermissionError):
                worker.require_approval()

    def test_approval_rejects_private_and_duplicate_sources(self):
        private = {"path": "/storage/emulated/0/Pictures/.private/a.jpg",
                   "target_phone_path": "/storage/emulated/0/Pictures/.private/a.webp"}
        self.approval_files([private])
        with patch.object(worker, "OUT", self.out):
            with self.assertRaisesRegex(ValueError, "outside approved public scope"):
                worker.require_approval()

        duplicate = {"path": "/storage/emulated/0/Pictures/a.jpg",
                     "target_phone_path": "/storage/emulated/0/Pictures/a.webp"}
        self.approval_files([duplicate, duplicate])
        with patch.object(worker, "OUT", self.out):
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                worker.require_approval()

    def test_approval_rejects_unbound_or_unsafe_destination(self):
        source = "/storage/emulated/0/Pictures/a.jpg"
        bad_target = {"path": source, "target_phone_path": "/storage/emulated/0/Android/data/owned.webp"}
        self.approval_files([bad_target])
        with patch.object(worker, "OUT", self.out):
            # The authorization digest alone must not bless an arbitrary path.
            with self.assertRaisesRegex(ValueError, "target|destination|scope"):
                worker.require_approval()

    def install_prepare_mocks(self, phone, *, metadata_value=None, quality_value=None,
                              mux_write=None, hdr=False, hdr_checks=None):
        calls = []
        metadata_value = metadata_value or {"issues": [], "xmp": b"", "exif": b"", "icc": b""}
        quality_value = quality_value or {
            "alpha_identical": True, "orientation_preserved": True,
            "pixels_identical": False, "psnr_rgb_db": 40.0,
        }
        mux_checks = {"exif_bytes_identical": True, "icc_bytes_identical": True,
                      "xmp_bytes_identical": True, "encoded_image_payload_identical": True,
                      "unsupported_features": []}
        hdr_checks = hdr_checks or {
            "full_hdr_decode_passed": True,
            "linear_fp16_pixels_identical": True,
            "exif_identical": True,
            "icc_identical": True,
            "gainmap_colour_space_flag_identical": True,
            "max_gainmap_metadata_absolute_difference": 0.0,
        }

        def run(args, timeout=240):
            args = [str(arg) for arg in args]
            calls.append(args)
            if "pull" in args:
                local = Path(args[-1])
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(phone.files[args[-2]])
                return ""
            if "-o" in args and "-outfile" not in args:
                source = Path(args[args.index("-o") - 1])
                output = Path(args[args.index("-o") + 1])
                output.parent.mkdir(parents=True, exist_ok=True)
                with Image.open(source) as image:
                    image.save(output, format="WEBP", quality=12,
                               lossless="-lossless" in args, exact="-exact" in args)
                return ""
            if "extract" in args:
                folder = Path(args[-1]); folder.mkdir(parents=True, exist_ok=True)
                with Image.open(args[args.index("extract") + 1]) as image:
                    image.convert("RGB").save(folder / "base.jpg", format="JPEG", quality=65,
                        xmp=b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/></x:xmpmeta>')
                (folder / "gainmap.jpg").write_bytes(b"synthetic gain map")
                return json.dumps({"extracted": True})
            if "-outfile" in args:
                output = Path(args[args.index("-outfile") + 1])
                source = Path(args[args.index("-outfile") + 2])
                output.write_bytes(source.read_bytes())
                return ""
            if "pack" in args:
                source = Path(args[args.index("pack") + 2])
                output = Path(args[-1])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(source.read_bytes())
                return ""
            if "compare" in args:
                return json.dumps(hdr_checks)
            raise AssertionError(f"Unexpected mocked command: {args}")

        def metadata(_path):
            if hdr:
                return {**metadata_value, "issues": ["hdr_gain_map_not_preserved"],
                        "xmp": b"<xmp/>"}
            return metadata_value

        def inject(source, encoded, target, **kwargs):
            if mux_write is not None:
                mux_write(Path(target), Path(encoded), Path(source))
            else:
                Path(target).write_bytes(Path(encoded).read_bytes())
            return mux_checks

        patches = [
            patch.object(worker, "OUT", self.out),
            patch.object(worker, "ADB", Path("fake-adb.exe")),
            patch.object(worker, "HDR", Path("fake-uhdr.exe")),
            patch.object(worker, "JPEGTRAN", Path("fake-jpegtran.exe")),
            patch.object(worker, "CWEBP", Path("fake-cwebp.exe")),
            patch.object(worker, "run", side_effect=run),
            patch.object(worker, "metadata", side_effect=metadata),
            patch.object(worker, "inject_metadata", side_effect=inject),
            patch.object(worker, "compare", return_value=quality_value),
            patch.object(worker, "hdr_xmp_issues", return_value=[]),
        ]
        return patches, calls

    def run_prepare(self, item, phone, **options):
        patches, calls = self.install_prepare_mocks(phone, **options)
        with patch.object(worker, "OUT", self.out):
            for context in patches[1:]:
                context.start()
            try:
                result = worker.prepare(item, phone, None)
            finally:
                for context in reversed(patches[1:]):
                    context.stop()
        return result, calls

    def test_prepare_jpeg_accepts_numeric_quality_and_preserves_metadata(self):
        source, size = self.image_source(".jpg", noisy=True)
        item, phone = self.item_and_phone(source, size)
        checks = {"alpha_identical": True, "orientation_preserved": True,
                  "pixels_identical": False, "psnr_rgb_db": 40.0}
        result, calls = self.run_prepare(item, phone, quality_value=checks)

        self.assertEqual(result["output_mime"], "image/webp")
        self.assertEqual(result["output_resolution"], list(size))
        self.assertLess(result["compressed_bytes"], item["bytes"] * .9)
        self.assertTrue(result["metadata_checks"]["encoded_image_payload_identical"])
        self.assertTrue(result["quality_checks"]["alpha_identical"])
        self.assertTrue(any("-q" in call for call in calls))
        self.assertEqual(phone.sha(item["path"]), hashlib.sha256(source.read_bytes()).hexdigest())

    def test_prepare_png_requires_exact_lossless_pixels(self):
        source, size = self.image_source(".png")
        item, phone = self.item_and_phone(source, size)
        checks = {"alpha_identical": True, "orientation_preserved": True,
                  "pixels_identical": True, "psnr_rgb_db": None}
        result, calls = self.run_prepare(item, phone, quality_value=checks)
        encoder = next(call for call in calls if "-o" in call)

        self.assertIn("-lossless", encoder)
        self.assertIn("-exact", encoder)
        self.assertEqual(result["quality_checks"]["pixels_identical"], True)
        self.assertEqual(result["output_resolution"], list(size))

    def test_prepare_keeps_low_numeric_quality(self):
        source, size = self.image_source(".jpg", noisy=True)
        item, phone = self.item_and_phone(source, size)
        checks = {"alpha_identical": True, "orientation_preserved": True,
                  "pixels_identical": False, "psnr_rgb_db": 37.9}
        with self.assertRaisesRegex(worker.KeepOriginal, "numeric_quality_below_38db"):
            self.run_prepare(item, phone, quality_value=checks)

    def test_prepare_keeps_candidate_when_gain_is_below_ten_percent(self):
        source, size = self.image_source(".jpg", noisy=True)
        item, phone = self.item_and_phone(source, size)

        def oversized(target, encoded, original):
            # This guard runs before decoding, so bytes need not form an image.
            target.write_bytes(b"x" * int(item["bytes"] * .91))

        with self.assertRaisesRegex(worker.KeepOriginal, "gain_below_10_percent"):
            self.run_prepare(item, phone, mux_write=oversized)

    def test_prepare_keeps_unsupported_metadata_without_encoding(self):
        source, size = self.image_source(".jpg")
        item, phone = self.item_and_phone(source, size)
        meta = {"issues": ["unmapped_metadata:custom"], "xmp": b"", "exif": b"", "icc": b""}
        with self.assertRaisesRegex(worker.KeepOriginal, "unmapped_metadata:custom"):
            self.run_prepare(item, phone, metadata_value=meta)
        self.assertEqual(phone.sha(item["path"]), hashlib.sha256(source.read_bytes()).hexdigest())

    def test_prepare_preserves_existing_destination(self):
        source, size = self.image_source(".jpg")
        item, phone = self.item_and_phone(source, size)
        phone.destination_exists = True
        with self.assertRaisesRegex(worker.KeepOriginal, "existing_destination"):
            self.run_prepare(item, phone)

    def test_prepare_hdr_keeps_jpeg_container_and_requires_full_hdr_checks(self):
        source, size = self.image_source(".jpg", noisy=True)
        item, phone = self.item_and_phone(source, size)
        checks = {
            "full_hdr_decode_passed": True,
            "linear_fp16_pixels_identical": True,
            "exif_identical": True,
            "icc_identical": True,
            "gainmap_colour_space_flag_identical": True,
            "max_gainmap_metadata_absolute_difference": 0.0,
        }
        result, calls = self.run_prepare(item, phone, hdr=True, hdr_checks=checks)

        self.assertEqual(result["target_phone_path"], item["path"])
        self.assertEqual(result["output_mime"], "image/jpeg")
        self.assertTrue(result["metadata_checks"]["hdr_lossless_verified"])
        self.assertTrue(any("extract" in call for call in calls))
        self.assertTrue(any("pack" in call for call in calls))

    def test_prepare_hdr_rejects_failed_pixel_identity(self):
        source, size = self.image_source(".jpg", noisy=True)
        item, phone = self.item_and_phone(source, size)
        checks = {
            "full_hdr_decode_passed": True,
            "linear_fp16_pixels_identical": False,
            "exif_identical": True,
            "icc_identical": True,
            "gainmap_colour_space_flag_identical": True,
            "max_gainmap_metadata_absolute_difference": 0.0,
        }
        with self.assertRaisesRegex(worker.KeepOriginal, "hdr_validation_failed"):
            self.run_prepare(item, phone, hdr=True, hdr_checks=checks)


if __name__ == "__main__":
    unittest.main()
