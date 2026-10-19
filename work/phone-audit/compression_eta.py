"""Measured ETA estimates; pauses, old policies and GPU regimes stay separate."""
from __future__ import annotations

import math
from statistics import median


def phase_key(phase: str, detail: str = '') -> str:
    text = phase.lower()
    if 'paused' in text or text.startswith('processing paused'):
        return 'paused'
    if 'waiting for' in text:
        return 'waiting' if 'wireless' in text else 'other'
    if 'testing higher quality' in text or 'testing adaptive bitrate' in text or (('-sample-' in detail) and ('quality screening' in text or 'preserving source' in text)):
        return 'preflight'
    for prefix, key in (
        ('encoding ', 'encode'), ('full decode', 'decode'),
        ('backing up', 'backup'), ('checking backup', 'verify'),
        ('verifying original', 'verify'), ('replacing', 'replace'),
        ('quality screening', 'quality'), ('preserving source', 'remux'),
        ('checking all frame', 'packets'), ('checking dolby', 'dolby'),
        ('comparing dolby', 'dolby'), ('connecting', 'connect'),
    ):
        if text.startswith(prefix):
            return key
    return 'other'


def _positive(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def progress_eta(elapsed, percent):
    """Use live progress only after enough work to reduce startup distortion."""
    if not _positive(elapsed) or not isinstance(percent, (float, int)) or not 2 <= percent < 100:
        return 0.0 if percent == 100 else None
    return elapsed * (100 - percent) / percent


class CompressionETA:
    def __init__(self, history, policy_version, regime, clock):
        self.history = [r for r in history if isinstance(r, dict) and r.get('policy_version') == policy_version and r.get('regime') == regime and _positive(r.get('active_seconds')) and _positive(r.get('original_bytes'))][-40:]
        self.policy_version = policy_version
        self.regime = regime
        self.clock = clock
        self.path = None
        self.original_bytes = 0
        self.active_seconds = 0.0
        self.phase_seconds = {}
        self.phase_counts = {}
        self.phase_elapsed = 0.0
        self.key = 'other'
        self.phase = None
        self.last = clock()
        self.active = False
        self.sample_encode_seconds = None
        self.attempt_id = None
        self.active_run_seconds=0.0
        self.run_processed_bytes=0
        self.run_completed_files=0

    def observe(self, phase, status, detail=''):
        now = self.clock()
        elapsed = max(0.0, now - self.last)
        if self.active:
            self.active_run_seconds+=elapsed
        if self.active and self.path:
            self.active_seconds += elapsed
            self.phase_elapsed += elapsed
            self.phase_seconds[self.key] = self.phase_seconds.get(self.key, 0.0) + elapsed
        key = phase_key(phase, detail)
        identity = (phase, detail)
        if identity != self.phase:
            self.phase_elapsed = 0.0
            self.phase = identity
            if self.path and status == 'running' and key not in ('paused', 'waiting'):
                self.phase_counts[key] = self.phase_counts.get(key, 0) + 1
        self.key, self.last = key, now
        self.active = status == 'running' and key not in ('paused', 'waiting')

    def begin_file(self, path, original_bytes):
        self.observe('', 'running')
        self.path, self.original_bytes = path, original_bytes
        self.active_seconds, self.phase_elapsed, self.phase_seconds, self.phase_counts = 0.0, 0.0, {}, {}
        self.phase, self.key = None, 'other'
        self.sample_encode_seconds, self.attempt_id = None, None

    def selected_encode(self, seconds, attempt):
        self.sample_encode_seconds, self.attempt_id = seconds, attempt

    def finish_file(self, status, defer_transfer=False):
        self.observe(self.phase[0] if self.phase else '', 'running', self.phase[1] if self.phase else '')
        if not self.path or (not defer_transfer and not status.startswith(('completed', 'skipped'))):
            return None
        record = {'phone_path': self.path, 'original_bytes': self.original_bytes, 'active_seconds': round(self.active_seconds, 3), 'phase_seconds': {k: round(v, 3) for k, v in self.phase_seconds.items()}, 'phase_counts': dict(self.phase_counts), 'status': status, 'policy_version': self.policy_version, 'regime': self.regime}
        if not defer_transfer:
            self.history.append(record)
            self.history = self.history[-40:]
            self.run_processed_bytes+=record['original_bytes']
            self.run_completed_files+=status=='completed'
        self.path = None
        self.active_seconds, self.phase_elapsed, self.phase_seconds, self.phase_counts = 0.0, 0.0, {}, {}
        self.active = False
        return record

    def complete_transfer(self, timing, seconds):
        # Transfer belongs to the previous file; never reset the next file's
        # live local-stage timing while acknowledging this background result.
        record={**timing,'phase_seconds':dict(timing.get('phase_seconds',{})),'phase_counts':dict(timing.get('phase_counts',{})),'status':'completed'}
        record['active_seconds']=round(record['active_seconds']+seconds,3)
        record['phase_seconds']['replace']=round(record['phase_seconds'].get('replace',0)+seconds,3)
        record['phase_counts']['replace']=record['phase_counts'].get('replace',0)+1
        self.history.append(record);self.history=self.history[-40:]
        self.run_processed_bytes+=record['original_bytes']
        self.run_completed_files+=1
        return record

    def _model(self, records):
        # Short preflights, sampled quality and connection overhead scale more
        # with file count; encoding/transfers/decoding scale more with work.
        fixed_keys = {'connect', 'preflight', 'quality', 'other'}
        fixed = [sum(r.get('phase_seconds', {}).get(k, 0) for k in fixed_keys) for r in records]
        rates = [max(0.0, r['active_seconds'] - f) / r['original_bytes'] for r, f in zip(records, fixed)]
        return median(fixed), median(rates)

    def snapshot(self, state):
        status = state.get('status')
        suspended = status in ('paused', 'stopped', 'waiting_for_phone', 'attention_required')
        phase_seconds = progress_eta(self.phase_elapsed, state.get('phase_progress_percent'))
        phase_basis = 'live_progress' if phase_seconds is not None else None
        if phase_seconds is None and self.key == 'encode' and _positive(self.sample_encode_seconds):
            phase_seconds = max(0.0, self.sample_encode_seconds - self.phase_elapsed)
            phase_basis = 'selected_samples'
        comparable = self.history
        completed = [r for r in comparable if r['status'] == 'completed']
        if phase_seconds is None and len(completed) >= 3 and self.path and self.key not in ('paused', 'waiting', 'other'):
            measured = []
            fixed_phase = self.key in ('connect', 'preflight', 'quality')
            for previous in completed:
                seconds = previous.get('phase_seconds', {}).get(self.key)
                if _positive(seconds):
                    per_occurrence = seconds / max(1, previous.get('phase_counts', {}).get(self.key, 1))
                    measured.append(per_occurrence if fixed_phase else per_occurrence * self.original_bytes / previous['original_bytes'])
            if len(measured) >= 3:
                phase_seconds = max(0.0, median(measured) - self.phase_elapsed)
                phase_basis = 'historical_phase'
        file_seconds = global_seconds = None
        reason = None
        if len(completed) >= 3:
            fixed, rate = self._model(comparable)
            count = max(0, state.get('remaining_candidates', 0))
            remaining_bytes = max(0, state.get('remaining_original_bytes', 0))
            global_seconds = max(0.0, fixed * count + rate * remaining_bytes - self.active_seconds)
            if self.run_completed_files>=3 and self.run_processed_bytes>0:
                # Wall time of the active pipeline counts overlap only once,
                # unlike adding each file's compute and transfer durations.
                global_seconds=remaining_bytes*self.active_run_seconds/self.run_processed_bytes
        else:
            reason = 'new_regime_calibration'
        # Successful files calibrate the time to finish an actual replacement;
        # fast rejects alone must not pretend that file completion is cheap.
        if len(completed) >= 3 and self.path:
            fixed, rate = self._model(completed)
            file_seconds = max(0.0, fixed + rate * self.original_bytes - self.active_seconds)
            downstream = {'encode': {'remux', 'quality', 'packets', 'dolby', 'decode', 'replace'}, 'remux': {'quality', 'packets', 'dolby', 'decode', 'replace'}, 'quality': {'packets', 'dolby', 'decode', 'replace'}, 'packets': {'dolby', 'decode', 'replace'}, 'dolby': {'decode', 'replace'}, 'decode': {'replace'}}.get(self.key)
            if downstream is not None:
                tail_rate = median(sum(r.get('phase_seconds', {}).get(k, 0) for k in downstream) / r['original_bytes'] for r in completed)
                file_seconds = (phase_seconds or 0) + tail_rate * self.original_bytes
        if phase_seconds is not None and file_seconds is not None:
            file_seconds = max(file_seconds, phase_seconds)
        if global_seconds is not None and file_seconds is not None:
            global_seconds = max(global_seconds, file_seconds)
        if status == 'complete':
            phase_seconds = file_seconds = global_seconds = 0
            reason = None
        def estimate(seconds, basis, samples):
            if seconds is None:
                return None
            # These are indicative ranges, not statistical confidence intervals.
            spread = (0.5, 2.0) if samples < 10 else (0.65, 1.6)
            return {'remaining_seconds': round(seconds), 'low_seconds': round(seconds * spread[0]), 'high_seconds': round(seconds * spread[1]), 'basis': basis, 'samples': samples}
        global_basis='observed_pipeline_throughput' if self.run_completed_files>=3 else 'processed_files_same_regime'
        return {'phase': estimate(phase_seconds, phase_basis, 0), 'file': estimate(file_seconds, 'completed_files_same_regime', len(completed)), 'global': estimate(global_seconds, global_basis, len(comparable)), 'suspended': suspended, 'calibration_processed_files': len(comparable), 'calibration_completed_files': len(completed), 'calibration_minimum_files': 3, 'reason': reason, 'regime': self.regime, 'excludes_future_interruptions': True}
