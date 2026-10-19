"""Retry shared snapshots and serialize short journal I/O on Windows."""
import json
from pathlib import Path
import time
from contextlib import nullcontext
from parallel_resource_locks import file_mutex

MISSING=object()


def _open_shared(path):
    return Path(path).open('r',encoding='utf-8')


def journal_io(path,timeout=.2):
    return file_mutex(Path(path).with_suffix('.io.lock'),timeout=timeout,poll=.01)


def read_json(path,default=MISSING,attempts=60,delay=.05):
    if attempts<1:
        raise ValueError('attempts must be positive')
    for attempt in range(attempts):
        try:
            gate=journal_io(path) if Path(path).name=='transactions.json' else nullcontext()
            with gate:
                with _open_shared(path) as stream:
                    return json.load(stream)
        except FileNotFoundError:
            if default is not MISSING:
                return default
            if attempt+1==attempts:raise
        except (PermissionError,TimeoutError,json.JSONDecodeError):
            if attempt+1==attempts:raise
        time.sleep(delay)
