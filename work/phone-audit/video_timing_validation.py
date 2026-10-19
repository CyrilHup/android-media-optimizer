"""Numeric validation only; never displays personal media."""


class TimingValidationError(ValueError):
    pass


def validate_timing(source_video, target_video, source_audio, target_audio):
    if not source_video or len(source_video) != len(target_video):
        raise TimingValidationError('Video frame count changed')
    origin_shift = target_video[0] - source_video[0]
    video_delta = max(abs((x-source_video[0])-(y-target_video[0]))
                      for x, y in zip(source_video, target_video))
    if video_delta >= .001:
        raise TimingValidationError(f'Video timestamp drift: {video_delta*1000:.6f} ms')
    if [p['data_hash'] for p in source_audio] != [p['data_hash'] for p in target_audio]:
        raise TimingValidationError('Copied audio packet payloads changed')
    differences = [float(y['pts_time'])-float(x['pts_time'])
                   for x, y in zip(source_audio, target_audio)]
    audio_delta = max((abs(d) for d in differences), default=0)
    sync_delta = max((abs(d-origin_shift) for d in differences), default=0)
    if max(audio_delta, sync_delta) >= .0011:
        raise TimingValidationError(
            f'Audio timestamp change: {audio_delta*1000:.6f} ms; '
            f'audio/video sync change: {sync_delta*1000:.6f} ms (limit 1.1 ms)')
    return {'frame_count': len(source_video),
            'max_normalized_video_timestamp_difference_ms': video_delta*1000,
            'audio_payloads_identical': True,
            'max_audio_timestamp_difference_ms': audio_delta*1000,
            'max_av_sync_difference_ms': sync_delta*1000}
