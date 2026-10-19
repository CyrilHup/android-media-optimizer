"""Compare recording metadata without confusing formatting with changed data."""
from decimal import Decimal
import re


def location_components(value):
    """Decimal-degree ISO 6709 used by the phone/MP4 muxer, no rounding."""
    match=re.fullmatch(r'([+-]\d{1,2}(?:\.\d+)?)([+-]\d{1,3}(?:\.\d+)?)([+-]\d+(?:\.\d+)?)?/',value)
    if not match:
        raise ValueError('Unsupported location metadata representation')
    latitude,longitude,altitude=match.groups()
    result=(Decimal(latitude),Decimal(longitude),Decimal(altitude) if altitude is not None else None)
    if abs(result[0])>90 or abs(result[1])>180:
        raise ValueError('Location metadata outside decimal-degree bounds')
    return result


def validate_recording_tags(source,target):
    result={}
    keys={'creation_time','location','location-eng','com.android.capture.fps'}
    keys.update(key for key in source if key.startswith('com.apple.quicktime.'))
    for key in sorted(keys):
        if key not in source:
            continue
        if key not in target:
            raise ValueError('Recording metadata missing after encoding: '+key)
        if source[key]==target[key]:
            result[key]='identical'
        elif key.startswith('location') and location_components(source[key])==location_components(target[key]):
            result[key]='same_coordinates_different_padding'
        else:
            raise ValueError('Recording metadata changed after encoding: '+key)
    return result
