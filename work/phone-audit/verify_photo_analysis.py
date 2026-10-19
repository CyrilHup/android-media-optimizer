"""Synthetic checks using bundled Pillow; no phone or personal media access."""
import tempfile
import math
import unittest
from pathlib import Path
from PIL import Image
from analyze_phone_photos import compare,choose_samples,public_photo


class PhotoAnalysisChecks(unittest.TestCase):
    def test_opaque_rgb_metric_has_exact_expected_mse(self):
        with tempfile.TemporaryDirectory() as temporary:
            source=Path(temporary)/'a.png';target=Path(temporary)/'b.png'
            Image.new('RGB',(17,13),(20,30,40)).save(source)
            Image.new('RGB',(17,13),(21,32,43)).save(target)
            result=compare(source,target)
            self.assertAlmostEqual(result['psnr_rgb_db'],10*math.log10(255**2/(14/3)),places=10)
            self.assertTrue(result['alpha_identical'])
            self.assertFalse(result['pixels_identical'])

    def test_exif_rotation_still_applies_without_alpha_allocation(self):
        with tempfile.TemporaryDirectory() as temporary:
            source=Path(temporary)/'a.png';target=Path(temporary)/'b.png'
            exif=Image.Exif();exif[274]=6
            image=Image.new('RGB',(3,2),(20,30,40))
            image.save(source,exif=exif);image.save(target,exif=exif)
            result=compare(source,target)
            self.assertEqual(result['dimensions'],[2,3])
            self.assertTrue(result['pixels_identical'])
    def test_alpha_and_pixels_are_checked_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            source=Path(temporary)/'a.png';target=Path(temporary)/'b.png'
            Image.new('RGBA',(3,3),(20,30,40,0)).save(source)
            Image.new('RGBA',(3,3),(20,30,41,0)).save(target)
            result=compare(source,target)
            self.assertFalse(result['pixels_identical'])
            self.assertTrue(result['alpha_identical'])
            self.assertGreater(result['psnr_rgb_db'],40)

    def test_lossless_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            source=Path(temporary)/'a.png';target=Path(temporary)/'b.webp'
            image=Image.new('RGB',(3,3),(20,30,40))
            image.save(source);image.save(target,lossless=True)
            result=compare(source,target)
            self.assertTrue(result['pixels_identical'])
            self.assertIsNone(result['psnr_rgb_db'])

    def test_private_paths_excluded(self):
        for relative in ('Pictures/.hidden/a.jpg','Pictures/private/a.jpg','Android/data/a.jpg'):
            self.assertFalse(public_photo({'path':'/storage/emulated/0/'+relative}))

    def test_sample_selection_is_bounded_and_deduplicated(self):
        rows=[{'path':f'/storage/emulated/0/Pictures/Screenshots/{i}.png','bytes':i+1000} for i in range(100)]
        chosen=choose_samples(rows)
        self.assertEqual(len(chosen),4)
        self.assertEqual(len({r['path'] for r in chosen}),4)


if __name__=='__main__':unittest.main()
