"""Machine-specific configuration stays outside version control."""
from pathlib import Path
import json
import os

ROOT=Path(__file__).resolve().parents[2]


def local_config():
    path=ROOT/'local-config.json'
    if not path.exists():return {}
    value=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value,dict):raise ValueError('local-config.json must contain an object')
    return value


def phone_serial():
    serial=os.environ.get('PHONE_HARDWARE_SERIAL') or local_config().get('phone_serial')
    if not isinstance(serial,str) or not serial.strip():
        raise ValueError('Configure phone_serial in local-config.json or PHONE_HARDWARE_SERIAL before connecting a phone')
    return serial.strip()
