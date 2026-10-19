"""Bounded-memory validation of decoded Dolby Vision metadata on every frame."""
from decimal import Decimal
from hashlib import sha256
from itertools import zip_longest
from pathlib import Path
import json
import re

FRAME_ENTRIES='frame=pts_time,best_effort_timestamp_time:frame_side_data'

class DolbyValidationError(ValueError):
    pass

def validate_config(source,target):
    def config(stream):
        return next((item for item in stream.get('side_data_list',[]) if item.get('side_data_type')=='DOVI configuration record'),None)
    a,b=config(source),config(target)
    if not a or not b:
        raise DolbyValidationError('Dolby Vision configuration missing from MP4 container')
    for field in ('dv_version_major','dv_version_minor','dv_profile','rpu_present_flag','el_present_flag','bl_present_flag','dv_bl_signal_compatibility_id'):
        if a.get(field)!=b.get(field):
            raise DolbyValidationError('Dolby Vision configuration changed: '+field)
    if not b.get('dv_level'):
        raise DolbyValidationError('Dolby Vision target level missing')
    if target.get('pix_fmt')!='yuv420p10le':
        raise DolbyValidationError('Dolby Vision target is not 10-bit')
    return {'source_profile':a['dv_profile'],'compatibility_id':b['dv_bl_signal_compatibility_id'],'source_level':a['dv_level'],'output_level':b['dv_level'],'container_config_preserved':True}

def frames(path):
    decoder=json.JSONDecoder()
    with Path(path).open(encoding='utf-8') as stream:
        buffer=''
        while True:
            chunk=stream.read(65536)
            if not chunk:
                raise DolbyValidationError('Frame probe has no frames array')
            buffer+=chunk
            match=re.search(r'"frames"\s*:\s*\[',buffer)
            if match:
                buffer=buffer[match.end():]
                break
            if len(buffer)>1024*1024:
                raise DolbyValidationError('Unexpected frame probe header')
        while True:
            buffer=buffer.lstrip()
            if buffer.startswith(']'):
                return
            try:
                frame,end=decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                chunk=stream.read(65536)
                if not chunk:
                    raise DolbyValidationError('Incomplete frame probe JSON')
                buffer+=chunk
                if len(buffer)>2*1024*1024:
                    raise DolbyValidationError('Unexpectedly large frame metadata object')
                continue
            if not isinstance(frame,dict):
                raise DolbyValidationError('Unexpected frame probe item')
            yield frame
            buffer=buffer[end:].lstrip()
            while not buffer:
                buffer=stream.read(65536).lstrip()
                if not buffer:
                    raise DolbyValidationError('Unterminated frame probe JSON')
            if buffer.startswith(','):
                buffer=buffer[1:]
            elif not buffer.startswith(']'):
                raise DolbyValidationError('Invalid frame probe separator')

def signature(frame):
    data=frame.get('side_data_list',[])
    metadata=[item for item in data if item.get('side_data_type')=='Dolby Vision Metadata']
    if len(metadata)!=1 or not any(item.get('side_data_type')=='Dolby Vision RPU Data' for item in data):
        raise DolbyValidationError('Dolby Vision RPU metadata missing on a decoded frame')
    payload=json.dumps(metadata[0],sort_keys=True,separators=(',',':')).encode()
    timestamp=frame.get('pts_time',frame.get('best_effort_timestamp_time'))
    if timestamp is None:
        raise DolbyValidationError('Dolby Vision frame timestamp missing')
    return Decimal(timestamp),sha256(payload).digest()

def compare_frames(source,target,expected_frames):
    count=0
    source_hash,target_hash=sha256(),sha256()
    max_delta=Decimal(0)
    for a,b in zip_longest(frames(source),frames(target)):
        if a is None or b is None:
            raise DolbyValidationError('Dolby Vision decoded frame counts differ')
        pts_a,hash_a=signature(a)
        pts_b,hash_b=signature(b)
        if hash_a!=hash_b:
            raise DolbyValidationError(f'Dolby Vision parsed RPU metadata differs on frame {count}')
        delta=abs(pts_a-pts_b)
        if delta>=Decimal('.001'):
            raise DolbyValidationError(f'Dolby Vision presentation timestamp differs on frame {count}')
        source_hash.update(hash_a);target_hash.update(hash_b)
        max_delta=max(max_delta,delta)
        count+=1
    if count!=expected_frames or not count:
        raise DolbyValidationError(f'Dolby Vision decoded frame count {count} differs from expected {expected_frames}')
    return {'all_frames_checked':count,'parsed_rpu_metadata_identical':True,'source_rpu_stream_sha256':source_hash.hexdigest(),'target_rpu_stream_sha256':target_hash.hexdigest(),'max_absolute_frame_timestamp_difference_ms':float(max_delta*1000),'comparison':'full structured decoded metadata with RPU presence; not raw SEI bytes'}
