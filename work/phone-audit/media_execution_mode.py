"""Explicit scheduling policy; preparing a proposal does not activate it."""
from compression_workflow import cpu_only_row


def plan_video_work(pending,cache,available,config,assign,backlog):
    if config.get('cpu_video_enabled',True):
        return backlog(assign(pending,cache,available),pending),[]
    if not available:
        raise RuntimeError('GPU-only video mode requested, but no GPU encoder is available')
    deferred=[row for row in pending if cpu_only_row(row,cache)]
    excluded={row['path'] for row in deferred}
    lanes=assign([r for r in pending if r['path'] not in excluded],cache,available,cpu_small_limit=0)
    lanes.pop('cpu',None)
    return lanes,deferred


def permitted_video_encoders(gpus,lane,dolby,cpu_allowed=True):
    encoders=(['x265'] if dolby else [*gpus,'x265']) if cpu_allowed else ([] if dolby else list(gpus))
    preferred='x265' if lane=='cpu' else lane
    if preferred in encoders:encoders=[preferred,*[e for e in encoders if e!=preferred]]
    return encoders
