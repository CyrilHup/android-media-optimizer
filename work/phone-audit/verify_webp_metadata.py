"""Synthetic metadata regression tests, using installed webpmux and bundled Pillow."""
from pathlib import Path
import tempfile
import unittest
from PIL import Image,ImageCms,PngImagePlugin
from analyze_phone_photos import compare
from webp_metadata import inject_metadata,metadata,raw_profile_exif,chunks


class WebPMetadataChecks(unittest.TestCase):
    def test_real_mux_preserves_orientation_dates_gps_icc_xmp(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);source=folder/'source.png';encoded=folder/'encoded.webp';target=folder/'fixed.webp'
            image=Image.new('RGB',(8,12),(10,20,30))
            exif=Image.Exif();exif[274]=6;exif[306]='2020:01:02 03:04:05'
            exif[34853]={1:'N',2:(1.0,2.0,3.0),3:'E',4:(4.0,5.0,6.0)}
            exif[34665]={36867:'2020:01:02 03:04:05'}
            icc=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
            png=PngImagePlugin.PngInfo();png.add_itxt('XML:com.adobe.xmp','<x:xmpmeta xmlns:x="adobe:ns:meta/"/>')
            image.save(source,exif=exif,icc_profile=icc,pnginfo=png)
            image.save(encoded,lossless=True)
            checks=inject_metadata(source,encoded,target,mtime=1577934245)
            self.assertTrue(all(checks[k] for k in ('exif_bytes_identical','icc_bytes_identical','xmp_bytes_identical','encoded_image_payload_identical','mtime_preserved')))
            self.assertTrue(dict(chunks(target))[b'EXIF'].startswith((b'II\x2a\x00',b'MM\x00\x2a')))
            with Image.open(source) as a,Image.open(target) as b:
                self.assertEqual(a.getexif().get_ifd(34853),b.getexif().get_ifd(34853))
                self.assertEqual(a.getexif().get_ifd(34665),b.getexif().get_ifd(34665))
            self.assertTrue(compare(source,target)['pixels_identical'])

    def test_raw_png_exif_profile_recovered_and_srgb_mapped(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);source=folder/'source.png';encoded=folder/'encoded.webp';target=folder/'fixed.webp'
            image=Image.new('RGBA',(4,4),(1,2,3,128))
            exif=Image.Exif();exif[274]=0;exif[306]='2020:01:02 03:04:05';payload=exif.tobytes()
            png=PngImagePlugin.PngInfo();png.add_text('Raw profile type APP1','\nAPP1\n'+str(len(payload))+'\n'+payload.hex()+'\n');png.add(b'sRGB',b'\x00')
            image.save(source,pnginfo=png);image.save(encoded,lossless=True)
            checks=inject_metadata(source,encoded,target)
            self.assertTrue(checks['raw_profile_exif_recovered'])
            self.assertTrue(checks['srgb_mapped_to_icc'])
            self.assertEqual(metadata(source)['exif'],metadata(target)['exif'])

    def test_refuses_overwriting_inputs(self):
        with self.assertRaisesRegex(ValueError,'new local copy'):
            inject_metadata('source.png','encoded.webp','source.png')

    def test_hdr_gain_map_is_withheld_even_if_base_pixels_decode(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'hdr.png'
            png=PngImagePlugin.PngInfo()
            png.add_itxt('XML:com.adobe.xmp','<hdrgm:GainMap xmlns:hdrgm="http://ns.adobe.com/hdr-gain-map/1.0/"/>')
            Image.new('RGB',(4,4),(1,2,3)).save(source,pnginfo=png)
            self.assertIn('hdr_gain_map_not_preserved',metadata(source)['issues'])

    def test_unmapped_png_text_is_withheld(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'note.png'
            png=PngImagePlugin.PngInfo();png.add_text('Comment','Synthetic test')
            Image.new('RGB',(4,4),(1,2,3)).save(source,pnginfo=png)
            self.assertIn('unmapped_metadata:Comment',metadata(source)['issues'])

    def test_sixteen_bit_png_is_withheld(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'sixteen.png'
            Image.new('I;16',(4,4),1).save(source)
            self.assertTrue(any(issue.startswith('unvalidated_pixel_mode:') for issue in metadata(source)['issues']))

    def test_bad_raw_profile_or_container_is_not_silently_accepted(self):
        with self.assertRaises(ValueError):raw_profile_exif('APP1\n5\n0001')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'bad.webp';path.write_bytes(b'not webp')
            with self.assertRaises(ValueError):chunks(path)

    def test_generic_profile_with_bare_tiff_header(self):
        exif=Image.Exif();exif[274]=1;exif[306]='2020:01:02 03:04:05'
        normalized=exif.tobytes();bare=normalized[6:]
        text='\ngeneric profile\n'+str(len(bare))+'\n'+bare.hex()+'\n'
        self.assertEqual(raw_profile_exif(text),normalized)

    def test_missing_capture_date_can_be_enriched_without_changing_pixels(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);source=folder/'source.png';encoded=folder/'encoded.webp';target=folder/'dated.webp'
            image=Image.new('RGB',(4,4),(1,2,3));image.save(source);image.save(encoded,lossless=True)
            result=inject_metadata(source,encoded,target,gallery_taken_ms=1577934245863)
            self.assertTrue(result['capture_date_enriched_from_original_mediastore'])
            with Image.open(target) as output:
                camera=output.getexif().get_ifd(34665)
                self.assertEqual(camera[36881],'+00:00')
                self.assertEqual(camera[37521],'863')
            self.assertTrue(compare(source,target)['pixels_identical'])


if __name__=='__main__':unittest.main()
