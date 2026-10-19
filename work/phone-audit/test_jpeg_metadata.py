"""Synthetic JPEG metadata-preservation tests; never reads personal media."""

import math
from pathlib import Path
import struct
import tempfile
import unittest

from PIL import Image, ImageChops

import jpeg_metadata


def marker_segment(marker, payload):
    return b"\xff" + bytes([marker]) + struct.pack(">H", len(payload) + 2) + payload


def synthetic_rgb(size=(128, 96)):
    width, height = size
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            pixels.extend(((x * 2 + y) % 256, (y * 2 + x // 2) % 256, (x + y) % 256))
    return Image.frombytes("RGB", size, bytes(pixels))


def add_segments_after_soi(path, *segments):
    data = path.read_bytes()
    if not data.startswith(b"\xff\xd8"):
        raise AssertionError("test fixture is not a JPEG")
    path.write_bytes(data[:2] + b"".join(segments) + data[2:])


def psnr(left, right):
    difference = ImageChops.difference(left.convert("RGB"), right.convert("RGB"))
    histogram = difference.histogram()
    samples = left.width * left.height * 3
    squared_error = sum(
        value * value * histogram[channel * 256 + value]
        for channel in range(3)
        for value in range(256)
    )
    mse = squared_error / samples
    return math.inf if mse == 0 else 10 * math.log10((255 * 255) / mse)


class JpegMetadataPreservationTests(unittest.TestCase):
    def make_metadata_jpeg(self, path, *, progressive=True):
        exif = Image.Exif()
        exif[274] = 1
        exif[36867] = "2026:10:18 12:34:56"
        icc = b"Synthetic ICC profile\x00" + bytes(range(64))
        synthetic_rgb().save(
            path,
            format="JPEG",
            quality=96,
            progressive=progressive,
            exif=exif,
            icc_profile=icc,
        )
        photoshop = b"Photoshop 3.0\x00" + bytes(range(256)) * 4 + b"\xff\xd8\x00opaque"
        comment = b"synthetic comment\x00with binary \xff\x00 bytes"
        add_segments_after_soi(
            path,
            marker_segment(0xED, photoshop),
            marker_segment(0xFE, comment),
        )
        return photoshop, comment, icc

    def test_progressive_fallback_preserves_app_com_bytes_and_decodes(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.jpg"
            target = Path(folder) / "target.jpg"
            photoshop, comment, icc = self.make_metadata_jpeg(source)

            source_data = source.read_bytes()
            source_parts, source_metadata = jpeg_metadata.jpeg_parts(source_data)
            with Image.open(source) as opened:
                opened.load()
                self.assertEqual(opened.format, "JPEG")
                self.assertEqual(opened.size, (128, 96))
                self.assertEqual(opened.info.get("progressive"), 1)
                self.assertGreaterEqual(source_data.count(b"\xff\xda"), 2)
                self.assertEqual(opened.info.get("icc_profile"), icc)
                self.assertIn(b"Exif\x00\x00", opened.info.get("exif", b""))

            self.assertTrue(source_parts)
            self.assertIn(marker_segment(0xED, photoshop), source_metadata)
            self.assertIn(marker_segment(0xFE, comment), source_metadata)
            self.assertTrue(any(part.endswith(b"\xff\xd9") for part in source_parts))

            result = jpeg_metadata.encode_preserving_jpeg(source, target, quality=90)
            self.assertTrue(result["jpeg_app_com_segments_identical"])
            self.assertTrue(result["jpeg_encoded_payload_identical"])

            target_parts, target_metadata = jpeg_metadata.jpeg_parts(target.read_bytes())
            self.assertEqual(target_metadata, source_metadata)
            self.assertEqual(len(target_parts), len(source_parts))
            with Image.open(source) as before, Image.open(target) as after:
                before.load()
                after.load()
                self.assertEqual(after.format, "JPEG")
                self.assertEqual(after.size, before.size)
                self.assertEqual(after.info.get("icc_profile"), icc)
                self.assertEqual(after.info.get("exif"), before.info.get("exif"))
                self.assertGreater(psnr(before, after), 32.0)

    def test_adobe_app14_transform_zero_uses_rgb_preserving_encode(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "adobe-rgb.jpg"
            target = Path(folder) / "target.jpg"
            synthetic_rgb((64, 48)).save(
                source, format="JPEG", quality=96, keep_rgb=True, progressive=True
            )
            _, source_metadata = jpeg_metadata.jpeg_parts(source.read_bytes())
            app14 = [segment for segment in source_metadata if segment[1] == 0xEE]
            self.assertEqual(len(app14), 1)
            self.assertEqual(app14[0][4:9], b"Adobe")
            self.assertEqual(app14[0][-1], 0)
            with Image.open(source) as opened:
                opened.load()
                self.assertEqual(opened.info.get("adobe_transform"), 0)
                source_pixels = opened.convert("RGB")

            result = jpeg_metadata.encode_preserving_jpeg(source, target, quality=90)
            self.assertTrue(result["jpeg_app_com_segments_identical"])
            _, target_metadata = jpeg_metadata.jpeg_parts(target.read_bytes())
            self.assertEqual(target_metadata, source_metadata)
            with Image.open(target) as opened:
                opened.load()
                self.assertEqual(opened.size, (64, 48))
                self.assertEqual(opened.info.get("adobe_transform"), 0)
                self.assertGreater(psnr(source_pixels, opened.convert("RGB")), 32.0)

    def test_rejects_mpf_multiple_picture_jpeg(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "mpf.jpg"
            target = Path(folder) / "target.jpg"
            synthetic_rgb().save(source, format="JPEG", quality=95)
            add_segments_after_soi(source, marker_segment(0xE2, b"MPF\x00synthetic index"))
            with self.assertRaisesRegex(ValueError, "Multiple JPEG images"):
                jpeg_metadata.encode_preserving_jpeg(source, target)
            self.assertFalse(target.exists())

    def test_rejects_trailing_payload_after_eoi(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "trailing.jpg"
            target = Path(folder) / "target.jpg"
            synthetic_rgb().save(source, format="JPEG", quality=95)
            source.write_bytes(source.read_bytes() + b"synthetic trailing video payload")
            with self.assertRaisesRegex(ValueError, "trailing payload"):
                jpeg_metadata.encode_preserving_jpeg(source, target)
            self.assertFalse(target.exists())

    def test_rejects_motion_photo_xmp(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "motion.jpg"
            target = Path(folder) / "target.jpg"
            synthetic_rgb().save(source, format="JPEG", quality=95)
            xmp = (
                b"http://ns.adobe.com/xap/1.0/\x00"
                b"<x:xmpmeta>MotionPhoto</x:xmpmeta>"
            )
            add_segments_after_soi(source, marker_segment(0xE1, xmp))
            with Image.open(source) as opened:
                self.assertIn(b"MotionPhoto", opened.info.get("xmp", b""))
            with self.assertRaisesRegex(ValueError, "motion or HDR metadata"):
                jpeg_metadata.encode_preserving_jpeg(source, target)
            self.assertFalse(target.exists())

    def test_rejects_cmyk_jpeg(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "cmyk.jpg"
            target = Path(folder) / "target.jpg"
            Image.new("CMYK", (32, 24), (10, 20, 30, 40)).save(
                source, format="JPEG", quality=95
            )
            with Image.open(source) as opened:
                self.assertEqual(opened.mode, "CMYK")
            with self.assertRaisesRegex(ValueError, "Unsupported JPEG pixel layout"):
                jpeg_metadata.encode_preserving_jpeg(source, target)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
