"""Focused tests for the adaptive video-bitrate planner."""

from __future__ import annotations

import unittest

from adaptive_bitrate import (
    MIN_VIDEO_BITRATE_BPS,
    SIZE_BUDGET_FRACTION,
    estimate_projected_output_bytes,
    plan_bitrates,
)


class AdaptiveBitrateTests(unittest.TestCase):
    def test_411_mb_720p_video_gets_bounded_ascending_targets(self) -> None:
        plan = plan_bitrates(
            original_bytes=411_000_000,
            duration_seconds=868.947,
            video_stream={"width": 1280, "height": 720},
            audio_streams=[{"bit_rate": "128000"}],
            target_dimensions=(1280, 720),
        )

        self.assertTrue(plan["viable"])
        self.assertGreater(plan["source_total_bps"], 3_700_000)
        self.assertEqual(plan["audio_bps"], 128_000)
        self.assertEqual(plan["targets"], sorted(set(plan["targets"])))
        self.assertTrue(all(MIN_VIDEO_BITRATE_BPS <= value <= plan["max_video_bps"] for value in plan["targets"]))
        self.assertLessEqual(
            plan["max_video_bps"] + plan["audio_bps"] + plan["mux_reserve_bps"],
            plan["max_output_bytes"] * 8 / 868.947,
        )

    def test_4k_to_1080p_uses_stronger_initial_reduction(self) -> None:
        plan_4k = plan_bitrates(
            5_000_000_000,
            600,
            {"width": 3840, "height": 2160},
            [],
            (1920, 1080),
        )
        plan_1080 = plan_bitrates(
            5_000_000_000,
            600,
            {"width": 1920, "height": 1080},
            [],
            (1920, 1080),
        )

        self.assertAlmostEqual(plan_4k["area_ratio"], 0.25)
        self.assertLess(plan_4k["initial_video_fraction"], plan_1080["initial_video_fraction"])
        self.assertLess(plan_4k["targets"][0], plan_1080["targets"][0])

    def test_no_audio_does_not_reserve_audio_bitrate(self) -> None:
        plan = plan_bitrates(100_000_000, 120, {"width": 1920, "height": 1080}, [], (1920, 1080))

        self.assertEqual(plan["audio_bps"], 0)
        self.assertEqual(plan["audio_bitrate_source"], "no_audio_streams")

    def test_multiple_audio_streams_sum_supplied_rates(self) -> None:
        plan = plan_bitrates(
            100_000_000,
            120,
            {"width": 1920, "height": 1080},
            [{"bit_rate": "128000"}, {"bit_rate": 256_000}],
            (1920, 1080),
        )

        self.assertEqual(plan["audio_bps"], 384_000)
        self.assertEqual(plan["audio_bitrate_source"], "stream_bit_rate")

    def test_missing_rate_uses_conservative_fallback_per_audio_stream(self) -> None:
        plan = plan_bitrates(
            100_000_000,
            120,
            {"width": 1920, "height": 1080},
            [{"bit_rate": "128000"}, {}],
            (1920, 1080),
        )

        self.assertEqual(plan["audio_bps"], 512_000)
        self.assertEqual(plan["audio_bitrate_source"], "fallback_256_kbps_per_stream")

    def test_tiny_file_with_insufficient_video_budget_is_explicitly_not_viable(self) -> None:
        plan = plan_bitrates(1_000_000, 3_600, {"width": 1920, "height": 1080}, [], (1920, 1080))

        self.assertFalse(plan["viable"])
        self.assertEqual(plan["reason"], "safe_video_bitrate_below_80_kbps")
        self.assertEqual(plan["targets"], [])

    def test_audio_and_mux_reserve_never_exceed_hard_output_budget(self) -> None:
        plan = plan_bitrates(
            411_000_000,
            868.947,
            {"width": 1280, "height": 720},
            [{"bit_rate": 160_000}, {"bit_rate": 192_000}],
            (1280, 720),
        )
        hard_budget_bps = plan["max_output_bytes"] * 8 / 868.947

        self.assertLessEqual(plan["max_video_bps"] + plan["audio_bps"] + plan["mux_reserve_bps"], hard_budget_bps)
        self.assertLessEqual(plan["max_output_bytes"], int(411_000_000 * SIZE_BUDGET_FRACTION))
        self.assertEqual(plan["targets"][-1], plan["max_video_bps"])

    def test_sample_projection_weights_duration_and_includes_copied_audio_once(self) -> None:
        projected = estimate_projected_output_bytes(
            sample_sizes_bytes=[3_000_000, 5_000_000],
            sample_durations_seconds=[10.0, 20.0],
            whole_duration_seconds=300.0,
            mux_reserve_bps=16_000,
        )
        expected = int(((8_000_000 * 8 / 30.0) + 16_000) * 300.0 / 8 + 0.999999)

        self.assertEqual(projected, expected)


if __name__ == "__main__":
    unittest.main()
