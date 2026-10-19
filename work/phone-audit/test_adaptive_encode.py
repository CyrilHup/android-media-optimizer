"""Regression guards for GPU routing, bounded rate control and retry safety."""
import unittest
from adaptive_encode import encoder_options
from compression_policy import classify,quality_passes,receipt_is_final,VERSION,failure_reason


class AdaptiveEncodingTests(unittest.TestCase):
    def test_every_encoder_has_explicit_bounded_average_and_peak(self):
        for encoder in ('nvenc','amf','x265'):
            args=encoder_options(encoder,2_000_000,3_000_000)
            self.assertEqual(int(args[args.index('-b:v')+1]),2_000_000)
            self.assertLessEqual(int(args[args.index('-maxrate')+1]),3_000_000)
            with self.assertRaises(ValueError):
                encoder_options(encoder,4_000_000,3_000_000)

    def test_gpu_routes_explicitly_request_ten_bit_surface(self):
        for encoder in ('nvenc','amf'):
            args=encoder_options(encoder,1_000_000,2_000_000,'yuv420p10le')
            self.assertEqual(args[args.index('-pix_fmt')+1],'p010le')

    def test_dolby_cannot_accidentally_use_unvalidated_gpu_route(self):
        for encoder in ('nvenc','amf'):
            with self.assertRaises(ValueError):
                encoder_options(encoder,1_000_000,2_000_000,dolby=True)
        args=encoder_options('x265',1_000_000,2_000_000,'yuv420p10le',True)
        self.assertEqual(args[args.index('-dolbyvision')+1],'1')
        self.assertIn('dolby-vision-profile=8.4',args[args.index('-x265-params')+1])

    def test_720p_hevc_and_full_range_are_still_eligible(self):
        base={'codec_name':'hevc','pix_fmt':'yuv420p','color_range':'tv','color_space':'bt709','color_transfer':'bt709','color_primaries':'bt709','width':1280,'height':720}
        self.assertIsNotNone(classify(base,300)[0])
        self.assertIsNotNone(classify({**base,'pix_fmt':'yuvj420p','color_range':'pc'},300)[0])

    def test_relaxed_visual_screen_retains_additional_hdr_pixel_guard(self):
        metrics={'vmaf_mean':91.9,'vmaf_p5':85.8,'psnr_average_db':35,'ssim_all':.96}
        self.assertTrue(quality_passes(metrics))
        self.assertFalse(quality_passes(metrics,True))
        self.assertTrue(quality_passes({**metrics,'psnr_average_db':43,'ssim_all':.975},True))
        self.assertFalse(quality_passes({**metrics,'vmaf_p5':79}))

    def test_failure_reason_describes_the_combination_and_full_mismatch(self):
        self.assertEqual(failure_reason([{'result':'quality_screen_failed','sample_preflight_only':True},{'result':'sample_size_budget_exceeded','sample_preflight_only':True}]),'no_size_quality_combination')
        self.assertEqual(failure_reason([{'result':'quality_screen_failed'}]),'full_encode_quality_mismatch')

    def test_old_skips_retry_but_successes_never_recompress(self):
        self.assertFalse(receipt_is_final({'status':'skipped_less_than_10_percent_saving','policy_version':4}))
        self.assertTrue(receipt_is_final({'status':'completed','policy_version':1}))
        self.assertTrue(receipt_is_final({'status':'skipped_less_than_10_percent_saving','policy_version':VERSION}))


if __name__=='__main__':unittest.main()
