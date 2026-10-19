"""Plan bounded average video bitrates for adaptive phone-video compression.

The returned targets are video-only bitrates in bits per second. Audio is copied
and reserved separately, so every planned target fits inside a conservative
whole-file size budget before encoding starts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import ceil, floor, isfinite, sqrt
from typing import TypedDict


MIN_VIDEO_BITRATE_BPS = 80_000
DEFAULT_AUDIO_BITRATE_BPS = 256_000
MIN_MUX_RESERVE_BPS = 16_000
SIZE_BUDGET_FRACTION = 0.88
INITIAL_VIDEO_FRACTION = 0.58


class AdaptiveBitratePlan(TypedDict):
    """JSON-friendly bitrate plan, with video targets ordered low to high."""

    viable: bool
    reason: str | None
    source_total_bps: int
    source_video_available_bps: int
    audio_bps: int
    audio_bitrate_source: str
    mux_reserve_bps: int
    max_output_bytes: int
    max_video_bps: int
    area_ratio: float
    initial_video_fraction: float
    target_width: int
    target_height: int
    targets: list[int]


def _positive_bitrate(stream: Mapping[str, object]) -> int | None:
    value = stream.get("bit_rate")
    if value is None or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(numeric) or numeric <= 0:
        return None
    return int(round(numeric))


def _audio_reserve(audio_streams: Sequence[Mapping[str, object]]) -> tuple[int, str]:
    if not audio_streams:
        return 0, "no_audio_streams"

    supplied = [_positive_bitrate(stream) for stream in audio_streams]
    if all(value is not None for value in supplied):
        return sum(value for value in supplied if value is not None), "stream_bit_rate"

    # A missing rate on one stream makes the total uncertain. Reserve the
    # fallback for every stream instead of undercounting the unknown track.
    return DEFAULT_AUDIO_BITRATE_BPS * len(audio_streams), "fallback_256_kbps_per_stream"


def plan_bitrates(
    original_bytes: int,
    duration_seconds: float,
    video_stream: Mapping[str, object],
    audio_streams: Sequence[Mapping[str, object]],
    target_dimensions: tuple[int, int],
) -> AdaptiveBitratePlan:
    """Return safe, ascending video-bitrate targets for a source video.

    The initial target is 58% of available source-video bitrate, adjusted by
    the square root of the target/source pixel-area ratio and clamped to
    20–65%. Higher targets approach the safe size-budget ceiling, allowing a
    quality preflight to improve quality without trying an over-budget encode.
    """
    if isinstance(original_bytes, bool) or original_bytes <= 0:
        raise ValueError("original_bytes must be a positive integer")
    if not isfinite(duration_seconds) or duration_seconds <= 0:
        raise ValueError("duration_seconds must be finite and positive")
    if len(target_dimensions) != 2:
        raise ValueError("target_dimensions must contain width and height")

    target_width, target_height = target_dimensions
    source_width = video_stream.get("width")
    source_height = video_stream.get("height")
    dimensions = (source_width, source_height, target_width, target_height)
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in dimensions):
        raise ValueError("source and target dimensions must be positive integers")

    source_total_bps = int(round(original_bytes * 8 / duration_seconds))
    audio_bps, audio_source = _audio_reserve(audio_streams)
    mux_reserve_bps = max(MIN_MUX_RESERVE_BPS, int(ceil(source_total_bps * 0.01)))
    source_video_available_bps = max(0, source_total_bps - audio_bps - mux_reserve_bps)

    max_output_bytes = int(floor(original_bytes * SIZE_BUDGET_FRACTION))
    budget_total_bps = max_output_bytes * 8 / duration_seconds
    max_video_bps = max(0, int(floor(budget_total_bps - audio_bps - mux_reserve_bps)))

    area_ratio = min(
        1.0,
        (target_width * target_height) / (source_width * source_height),
    )
    initial_video_fraction = min(0.65, max(0.20, INITIAL_VIDEO_FRACTION * sqrt(area_ratio)))

    viable = max_video_bps >= MIN_VIDEO_BITRATE_BPS and source_video_available_bps >= MIN_VIDEO_BITRATE_BPS
    targets: list[int] = []
    reason: str | None = None
    if viable:
        initial = min(max_video_bps, max(MIN_VIDEO_BITRATE_BPS, int(floor(source_video_available_bps * initial_video_fraction))))
        midpoint = initial + (max_video_bps - initial) // 2
        targets = sorted({initial, midpoint, max_video_bps})
        # Keep the safety condition explicit in case this planner is adjusted.
        targets = [target for target in targets if MIN_VIDEO_BITRATE_BPS <= target <= max_video_bps]
        if not targets:
            viable = False
            reason = "no_safe_video_bitrate_target"
    else:
        reason = "safe_video_bitrate_below_80_kbps"

    return {
        "viable": viable,
        "reason": reason,
        "source_total_bps": source_total_bps,
        "source_video_available_bps": source_video_available_bps,
        "audio_bps": audio_bps,
        "audio_bitrate_source": audio_source,
        "mux_reserve_bps": mux_reserve_bps,
        "max_output_bytes": max_output_bytes,
        "max_video_bps": max_video_bps,
        "area_ratio": round(area_ratio, 6),
        "initial_video_fraction": round(initial_video_fraction, 6),
        "target_width": target_width,
        "target_height": target_height,
        "targets": targets,
    }


def estimate_projected_output_bytes(
    sample_sizes_bytes: Sequence[int],
    sample_durations_seconds: Sequence[float],
    whole_duration_seconds: float,
    mux_reserve_bps: int,
) -> int:
    """Project full-file size from muxed samples, including copied audio.

    Sample sizes already include copied audio and sample-container overhead;
    they are aggregated by total bytes over total sample duration. The
    planner's conservative mux reserve is then added once to the projected
    whole-file rate.
    """
    if not sample_sizes_bytes or len(sample_sizes_bytes) != len(sample_durations_seconds):
        raise ValueError("sample sizes and durations must be non-empty and have equal length")
    if not isfinite(whole_duration_seconds) or whole_duration_seconds <= 0:
        raise ValueError("whole_duration_seconds must be finite and positive")
    if isinstance(mux_reserve_bps, bool) or mux_reserve_bps < 0:
        raise ValueError("mux_reserve_bps must be a non-negative integer")

    total_bytes = 0
    total_duration = 0.0
    for size, duration in zip(sample_sizes_bytes, sample_durations_seconds):
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("sample sizes must be non-negative integers")
        if not isfinite(duration) or duration <= 0:
            raise ValueError("sample durations must be finite and positive")
        total_bytes += size
        total_duration += duration

    measured_muxed_bps = total_bytes * 8 / total_duration
    return int(ceil((measured_muxed_bps + mux_reserve_bps) * whole_duration_seconds / 8))
