"""Strict classification: only proven connectivity failures auto-restart."""
import re
from datetime import datetime


def network_error_only(message):
    return bool(re.fullmatch(r'(?:(?:device\.)?PhoneUnavailableError: |Phone transfer failed; backups retained: )?The previously verified Nothing phone is not connected\.',str(message or '')))


def fresh_worker_status(worker,started):
    try:return datetime.fromisoformat(worker.get('updated_utc',''))>=datetime.fromisoformat(started)
    except (TypeError,ValueError):return False


def retry_video_network_failure(worker):
    failures=[r.get('error') for r in worker.get('workers',[]) if r.get('status')=='attention_required' and r.get('error')]
    if not failures:failures=[worker.get('error')]
    return bool(failures) and all(network_error_only(message) for message in failures)
