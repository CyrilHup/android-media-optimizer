import unittest
from compression_scope import eligible_video, public_video_path
from video_timing_validation import TimingValidationError, validate_timing
from video_encode_options import remux_args


class ScopeTests(unittest.TestCase):
    def test_saved_instagram_small_and_large_authorized(self):
        for size in (1000, 40*1024**2):
            self.assertTrue(eligible_video({'path':'/storage/emulated/0/Movies/Instagram/video.mp4','bytes':size}))

    def test_private_hidden_and_app_files_still_excluded(self):
        for relative in ('Movies/.hidden/Instagram/v.mp4','Movies/private/Instagram/v.mp4',
                         'Android/data/com.instagram.android/v.mp4','Movies/Instagram/../v.mp4'):
            self.assertFalse(public_video_path('/storage/emulated/0/'+relative))

    def test_other_small_files_keep_existing_scope(self):
        self.assertFalse(eligible_video({'path':'/storage/emulated/0/Movies/v.mp4','bytes':1000}))


class TimingTests(unittest.TestCase):
    def audio(self, offset=0, payload='same'):
        return [{'pts_time': str(.023197+offset), 'data_hash':payload}]

    def test_measured_remux_fix_passes_original_threshold(self):
        result=validate_timing([0, 1], [0, 1], self.audio(), self.audio(-.000205))
        self.assertAlmostEqual(result['max_av_sync_difference_ms'], .205)

    def test_previous_failure_rejected_with_detail(self):
        with self.assertRaisesRegex(TimingValidationError, '1.202000 ms'):
            validate_timing([0, 1], [0, 1], self.audio(), self.audio(-.001202))

    def test_video_origin_shift_cannot_hide_sync_change(self):
        with self.assertRaises(TimingValidationError):
            validate_timing([0, 1], [.05, 1.05], self.audio(), self.audio())

    def test_audio_payload_and_video_frame_count_remain_strict(self):
        with self.assertRaises(TimingValidationError):
            validate_timing([0, 1], [0, 1], self.audio(), self.audio(payload='changed'))
        with self.assertRaises(TimingValidationError):
            validate_timing([0, 1], [0], self.audio(), self.audio())

    def test_mp4_remux_has_precise_movie_clock(self):
        args=remux_args('ffmpeg','source','target','filter')
        self.assertEqual(args[args.index('-movie_timescale')+1], '1000000')


if __name__ == '__main__':
    unittest.main()
