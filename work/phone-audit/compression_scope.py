"""Authorized public media, including saved Instagram videos of any size."""
from pathlib import PurePosixPath


def public_video_path(path):
    prefix = '/storage/emulated/0/'
    if not path.startswith(prefix):
        return False
    relative = path[len(prefix):]
    parts = relative.split('/')
    return (relative.startswith(('DCIM/Camera/', 'Download/', 'Movies/', 'Pictures/'))
            and PurePosixPath(path).suffix.lower() in ('.mp4', '.mov', '.mpeg')
            and not any(p.startswith('.') or p.casefold() == 'private' for p in parts))


def eligible_video(row):
    return (public_video_path(row['path']) and row['bytes'] > 0
            and (row['bytes'] >= 25 * 1024**2
                 or 'instagram' in (p.casefold() for p in row['path'].split('/'))))
