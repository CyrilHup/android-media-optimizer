"""Atomic per-run file ownership shared by encoder processes."""
import json
import os
from pathlib import Path
from parallel_resource_locks import file_mutex


def claim_file(folder, run_id, lane, path):
    folder = Path(folder)
    with file_mutex(folder/'work-claims.lock', timeout=30):
        active = json.loads((folder/'parallel-active-run.json').read_text(encoding='utf-8'))
        if active['id'] != run_id:
            raise RuntimeError('Superseded worker cannot claim a video')
        journal = folder/'work-claims.json'
        claims = json.loads(journal.read_text(encoding='utf-8'))
        if claims['run_id'] != run_id:
            raise RuntimeError('Work claims belong to another run')
        if path in claims['owners']:
            return False
        claims['owners'][path] = {'lane': lane, 'pid': os.getpid()}
        temporary = folder/'work-claims.new.json'
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(claims, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(journal)
        return True


def with_cpu_backlog(lanes, pending, max_bytes=50*1024**2):
    """CPU may compete for unstarted small files; claims decide sole ownership."""
    result = {key: list(rows) for key, rows in lanes.items()}
    own = {row['path'] for row in result.get('cpu', [])}
    extra = sorted((row for row in pending
                    if row['bytes'] <= max_bytes and row['path'] not in own),
                   key=lambda row: row['bytes'])
    if extra:
        result.setdefault('cpu', []).extend(extra)
    return result
