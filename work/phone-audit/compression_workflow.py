"""Pure scheduling and validated numeric preflight-cache helpers."""
import math

from compression_policy import classify, quality_passes


def cpu_only_row(row, metadata_cache):
    cached = metadata_cache.get(row['path'])
    if not cached or cached.get('original_bytes') != row['bytes'] or cached.get('mtime') != row['mtime']:
        return False
    info = cached.get('probe', {})
    video = next((s for s in info.get('streams', []) if s.get('codec_type') == 'video'), None)
    if video is None:
        return False
    route, _ = classify(video, float(info.get('format', {}).get('duration', 0)))
    return bool(route and route['dolby'])


def valid_preflight(cache, identity, expected_starts, hdr):
    """Reuse only three passing sections bound to the exact source and recipe."""
    if not isinstance(cache, dict) or cache.get('identity') != identity:
        return False
    windows, sizes, seconds = (cache.get(k) for k in ('quality_windows', 'sample_sizes', 'sample_encode_seconds'))
    if not all(isinstance(value, list) and len(value) == 3 for value in (windows, sizes, seconds)):
        return False
    if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in sizes):
        return False
    if any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0 for value in seconds):
        return False
    try:
        return all(window['start_seconds'] == start and window.get('sampled_frames', 0) >= 3 and quality_passes(window, hdr)
                   for window, start in zip(windows, expected_starts))
    except (KeyError, TypeError, ValueError):
        return False
