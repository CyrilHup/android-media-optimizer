"""Resumeable, bounded-disk 1080p compression of personal public phone videos.

One verified original is a backup batch; up to two batches can overlap. No app-private files,
hidden files or gallery trash are included. Public saved Instagram videos are eligible.
"""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
from copy import deepcopy
import re
import shlex
import shutil
import subprocess
import time
import traceback
import msvcrt
import math
import os
import sys
if __name__=='__main__' and not os.environ.get('PHONE_COMPRESSION_LANE'):
    parallel_config=Path(__file__).resolve().parents[2]/'outputs/phone-video-compression-batches/parallel-config.json'
    if parallel_config.exists() and json.loads(parallel_config.read_text(encoding='utf-8')).get('enabled'):
        from parallel_compression_coordinator import main as parallel_main
        raise SystemExit(parallel_main())
from device import phone_transport,read_phone_shell
from phone_video_transaction import apply,recover_pending
from compression_policy import VERSION,COLOR_ENUMS,classify,quality_passes,receipt_is_final,failure_reason
from video_metadata import validate_recording_tags
from video_encode_options import remux_args
from compression_scope import eligible_video
from compression_work_claims import claim_file
from media_execution_mode import permitted_video_encoders
from video_timing_validation import validate_timing,TimingValidationError
from video_quality_filters import quality_filter
from dolby_validation import FRAME_ENTRIES,validate_config,compare_frames,DolbyValidationError
from adaptive_bitrate import plan_bitrates,estimate_projected_output_bytes
from adaptive_encode import encoder_options,detect_encoders
from compression_eta import CompressionETA
from phone_transfer_pipeline import PhoneTransferPipeline
from phone_backup_prefetch import PhoneBackupPrefetch
from compression_workflow import cpu_only_row,valid_preflight
from parallel_resource_locks import DiskReservations
from shared_json import read_json

ROOT=Path(__file__).resolve().parents[2]
GLOBAL_OUT=ROOT/'outputs/phone-video-compression-batches'
LANE=os.environ.get('PHONE_COMPRESSION_LANE')
OUT=GLOBAL_OUT/'parallel'/LANE if LANE else GLOBAL_OUT
TEMP=GLOBAL_OUT/'temporary'
WORK=ROOT/'work/phone-video-compression-batches'
for folder in [OUT,TEMP,WORK]: folder.mkdir(parents=True,exist_ok=True)
reservations=DiskReservations(GLOBAL_OUT/'disk-reservations.json',ROOT) if LANE else None
reservation_owner=f'{LANE}:{os.getpid()}'
_lock=(OUT/'worker.lock').open('a+b')
_lock.seek(0)
_lock.write(b'1')
_lock.flush()
_lock.seek(0)
msvcrt.locking(_lock.fileno(),msvcrt.LK_NBLCK,1)
if LANE and json.loads((GLOBAL_OUT/'parallel-active-run.json').read_text(encoding='utf-8')).get('id')!=os.environ.get('PHONE_COMPRESSION_RUN_ID'):
    raise RuntimeError('Superseded parallel worker; refusing stale assignment')
assigned_paths=set(json.loads(Path(os.environ['PHONE_COMPRESSION_JOBS']).read_text(encoding='utf-8'))['paths']) if LANE else None
ADB=ROOT/'work/android-tools/platform-tools/adb.exe'
BIN=Path('C:/ProgramData/chocolatey/lib/ffmpeg/tools/ffmpeg/bin')
FFMPEG=str(BIN/'ffmpeg.exe')
FFPROBE=str(BIN/'ffprobe.exe')
STATUS=OUT/'worker-status.json'
RECEIPTS=OUT/'validation-receipts.json'
state={'status':'running','phase':'initializing','completed_files':0,'remaining_candidates':0,'skipped_files':0,'policy_version':VERSION,'policy':'Source-adaptive bitrate at every resolution, maximum 1080p. GPU first after real capability probe; sampled size/quality preflight, at most one full encode per file. Original FPS/audio/colours retained. Dolby Vision uses x265 with every-frame metadata validation. Saved Instagram videos included.'}
receipts=read_json(RECEIPTS,default={'records':[]})
_last_journal_snapshot=None
transport=None
CONTROL=GLOBAL_OUT/'control.json'
_phase_started=time.monotonic()
eta_tracker=None
ETA_HISTORY=OUT/'eta-history.jsonl'
preflight_deadline=None
transfer_pipeline=None
transfer_record=None
transfer_started=None
transfer_error=None
backup_prefetch=None
prefetch_row=None
current_queue_index=-1

class PauseRequested(Exception):
    def __init__(self,command):
        self.command=command

class MediaCommandError(RuntimeError):
    """A local media command failed before any phone replacement."""

class SizeBudgetExceeded(MediaCommandError):
    """Stop wasting encoding time; the original remains untouched."""

class PreflightBudgetExceeded(Exception):
    """Defer an unusually expensive local screen and continue other files."""

class BackgroundTransferError(RuntimeError):
    pass

def collect_transfer():
    global transfer_record,transfer_error,transfer_started
    if transfer_pipeline is None or transfer_error is not None:
        return
    try:
        completed=transfer_pipeline.poll()
    except Exception as error:
        transfer_error=str(error) or type(error).__name__
        raise BackgroundTransferError('Phone transfer failed; backups retained: '+transfer_error) from error
    if completed is None:
        return
    receipt=completed.record
    transaction=completed.result
    timing=receipt.get('processing_timing')
    if timing:
        timing=eta_tracker.complete_transfer(timing,completed.elapsed_seconds)
        receipt['processing_timing']=timing
        with ETA_HISTORY.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(timing)+'\n')
    finish_record(receipt,'completed',saved_bytes=transaction['saved_bytes'],temporary_backup_deleted=transaction['original_backup_deleted'])
    print(json.dumps({'completed_file':receipt['phone_path'],'saved_bytes':transaction['saved_bytes'],'phone_transfer_seconds':round(completed.elapsed_seconds,3)}),flush=True)
    transfer_record=None;transfer_started=None

def verified_phone_transfer(record):
    transaction=apply(record,remove_temporary=True)
    if not transaction['media_index_updated']:
        raise RuntimeError('Replacement valid but media index unresolved; backup retained')
    return transaction

def prefetch_copy(row,destination,selected_transport,cancel_event):
    # Read-only phone operation. Every copied byte is reverified by the main
    # worker before encoding; an interrupted partial copy is never trusted.
    log=WORK/(hashlib.sha256(row['path'].encode()).hexdigest()[:12]+'-prefetch.log')
    with log.open('wb') as stream:
        child=subprocess.Popen([str(ADB),'-s',selected_transport,'pull',row['path'],str(destination)],stdout=stream,stderr=subprocess.STDOUT)
        started=time.monotonic()
        try:
            while child.poll() is None:
                if cancel_event.wait(.25) or time.monotonic()-started>900:
                    raise RuntimeError('Backup prefetch interrupted; partial computer copy retained')
                if shutil.disk_usage(ROOT).free<3*1024**3:
                    raise RuntimeError('Backup prefetch stopped at disk reserve')
            if child.returncode:
                raise RuntimeError('Backup prefetch failed; main worker will verify and retry normally')
        finally:
            if child.poll() is None:
                child.terminate();child.wait(timeout=30)
    return {'backup_path':str(destination),'bytes':destination.stat().st_size}

def maybe_prefetch():
    global prefetch_row
    if LANE or backup_prefetch is None or backup_prefetch.busy or transfer_record is not None or transfer_error is not None:
        return
    if not state.get('phase','').startswith('Encoding 1080p copy') or current_queue_index+1>=len(pending):
        return
    row=pending[current_queue_index+1]
    cached=preflight.get(row['path'])
    if cached and cached['original_bytes']==row['bytes'] and cached['mtime']==row['mtime']:
        source_video=next(s for s in cached['probe']['streams'] if s['codec_type']=='video')
        cached_route,_=classify(source_video,float(cached['probe']['format']['duration']))
        if cached_route is None:
            return
    token=hashlib.sha256(row['path'].encode()).hexdigest()[:12]
    destination=TEMP/(token+'-original'+Path(row['path']).suffix)
    if destination.exists() or (row['path'] in initial_backups and Path(initial_backups[row['path']]['backup_path']).exists()):
        return
    if shutil.disk_usage(ROOT).free<row['bytes']*3+8*1024**3:
        return
    selected_transport=transport
    backup_prefetch.start(row['path'],lambda cancel_event:prefetch_copy(row,destination,selected_transport,cancel_event))
    prefetch_row=row

def await_prefetch(path):
    global prefetch_row
    if backup_prefetch is None or not backup_prefetch.busy:
        return
    if backup_prefetch.key!=path:
        raise RuntimeError('Prefetched backup does not match the next queued file; copies retained')
    while backup_prefetch.busy:
        check_control()
        result=backup_prefetch.poll()
        if result is not None:
            # A failed or cancelled copy is handled by the existing live-hash
            # and partial-copy verification, with a normal pull if necessary.
            prefetch_row=None
            break
        update(phase='Waiting for prefetched backup to finish',status='running')
        time.sleep(.5)

def close_pipeline():
    """Cancel read-only prefetch, but finish any started phone transaction."""
    global prefetch_row
    if backup_prefetch is not None:
        backup_prefetch.close()
        prefetch_row=None
    target={key:state.get(key) for key in ('status','phase','error')}
    failure=None
    try:
        while transfer_pipeline is not None and transfer_pipeline.busy and transfer_error is None:
            collect_transfer()
            if transfer_pipeline.busy:
                update(status='running',phase='Finishing in-flight phone transfer safely')
                time.sleep(.5)
        if transfer_error is not None:
            failure=BackgroundTransferError(transfer_error)
    except Exception as error:
        failure=error
    finally:
        if transfer_pipeline is not None:
            try:
                transfer_pipeline.close()
            except Exception as error:
                failure=failure or error
    if failure:
        update(status='attention_required',phase='Phone transfer stopped; available backups retained',error=str(failure))
        if target['status']!='attention_required':
            raise failure
    elif target['status'] in ('paused','stopped','attention_required'):
        update(**target)

def command():
    try:
        return read_json(CONTROL).get('command','run')
    except (FileNotFoundError,json.JSONDecodeError):
        return 'run'

def check_control():
    collect_transfer()
    value=command()
    if value in ('pause','stop'):
        raise PauseRequested(value)

def write_json(path,value):
    temporary=path.with_suffix('.new.json')
    temporary.write_text(json.dumps(value,indent=2),encoding='utf-8')
    # Windows can deny rename briefly while the HTTP progress reader has the
    # destination open. Progress reporting must never cancel a valid encode.
    for attempt in range(50):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            time.sleep(.1)
    if path==STATUS:
        return
    temporary.replace(path)

def update(**values):
    global _phase_started,_last_journal_snapshot
    if 'phase' in values and values['phase']!=state.get('phase') and 'phase_progress_percent' not in values:
        values['phase_progress_percent']=None
    if 'phase' in values and values['phase']!=state.get('phase'):
        # Append-only timings survive retries and log overwrites. This also
        # measures hashing, phone transactions and waiting between commands.
        timing={'ended_utc':datetime.now(timezone.utc).isoformat(),'phase':state.get('phase'),'phone_path':state.get('current_file'),'seconds':round(time.monotonic()-_phase_started,3),'policy_version':VERSION}
        with (OUT/'phase-timings.jsonl').open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(timing)+'\n')
        _phase_started=time.monotonic()
    state.update(values,updated_utc=datetime.now(timezone.utc).isoformat())
    if 'phase' in values and 'eta_detail' not in values:
        state['eta_detail']=''
    try:
        transactions=read_json(GLOBAL_OUT/'transactions.json',attempts=3,delay=.02)
        _last_journal_snapshot=transactions
        state.pop('journal_snapshot_stale',None)
    except (PermissionError,FileNotFoundError,TimeoutError,json.JSONDecodeError):
        if _last_journal_snapshot is None:
            raise
        # This snapshot drives progress counters only. apply()/recovery always
        # require a fresh journal under the phone transaction mutex.
        transactions=_last_journal_snapshot
        state['journal_snapshot_stale']=True
    state['completed_files']=sum(bool(r.get('phone_hash_verified')) for r in transactions['transactions'])
    state['saved_bytes']=sum(r.get('saved_bytes',0) for r in transactions['transactions'] if r.get('phone_hash_verified'))
    latest={r['phone_path']:r for r in receipts['records']}
    skipped=[r for r in latest.values() if r.get('status','').startswith('skipped') and receipt_is_final(r)]
    state['skipped_files']=len(skipped)
    reasons={}
    for receipt in skipped:
        reason=receipt['status']
        if reason=='skipped_quality_screen_after_all_encoders' and receipt.get('policy_version',0)>=5 and receipt.get('attempts'):
            reason='skipped_'+failure_reason(receipt['attempts'])
        reasons[reason]=reasons.get(reason,0)+1
    state['skip_reasons']=reasons
    state['pending_prior_skips']=sum(r.get('status','').startswith('skipped') and not receipt_is_final(r) for r in latest.values())
    if 'queue' in globals():
        final_paths={path for path,receipt in latest.items() if receipt_is_final(receipt)}
        final_paths.update(t['phone_path'] for t in transactions['transactions'] if t.get('phone_hash_verified'))
        state['processed_candidates']=sum(row['path'] in final_paths for row in queue)
        state['processed_original_bytes']=sum(row['bytes'] for row in queue if row['path'] in final_paths)
        state['remaining_candidates']=len(queue)-state['processed_candidates']
        state['remaining_original_bytes']=sum(row['bytes'] for row in queue if row['path'] not in final_paths)
    state['pipeline_mode']='prefetch_encode_and_phone_transfer'
    state['phone_transfer']=None
    if transfer_record is not None:
        elapsed=time.monotonic()-transfer_started
        journal_row=next((r for r in reversed(transactions['transactions']) if r['phone_path']==transfer_record['phone_path']),{})
        estimates=[]
        for measured in eta_tracker.history:
            seconds=measured.get('phase_seconds',{}).get('replace')
            if measured.get('status')=='completed' and seconds:
                estimates.append(seconds/(measured['original_bytes']+measured.get('compressed_bytes',0)))
        eta=None
        if len(estimates)>=3:
            estimates.sort()
            eta=max(0,estimates[len(estimates)//2]*(transfer_record['original_bytes']+transfer_record['compressed_bytes'])-elapsed)
        state['phone_transfer']={'phone_path':transfer_record['phone_path'],'compressed_bytes':transfer_record['compressed_bytes'],'elapsed_seconds':round(elapsed,1),'transaction_status':journal_row.get('status','checking_original'),'eta_remaining_seconds':round(eta) if eta is not None else None,'error':transfer_error}
    state['backup_prefetch']=None
    if prefetch_row is not None:
        prefetch_token=hashlib.sha256(prefetch_row['path'].encode()).hexdigest()[:12]
        prefetch_path=TEMP/(prefetch_token+'-original'+Path(prefetch_row['path']).suffix)
        copied=prefetch_path.stat().st_size if prefetch_path.exists() else 0
        state['backup_prefetch']={'phone_path':prefetch_row['path'],'original_bytes':prefetch_row['bytes'],'copied_bytes':copied,'progress_percent':min(100,round(copied/prefetch_row['bytes']*100,1))}
    if eta_tracker is not None:
        eta_tracker.observe(state.get('phase',''),state.get('status',''),state.get('eta_detail',''))
        state['eta']=eta_tracker.snapshot(state)
    write_json(STATUS,state)

def sha(path):
    with Path(path).open('rb') as stream: return hashlib.file_digest(stream,'sha256').hexdigest()

def shell(command,timeout=180):
    global transport
    output,transport=read_phone_shell(ADB,transport,command,timeout=timeout)
    return output

def wait_phone():
    global transport
    while True:
        check_control()
        try:
            transport=phone_transport(ADB)
            update(status='running',phase='Connecting')
            return
        except Exception:
            update(status='waiting_for_phone',phase='Waiting for wireless ADB; originals and backups preserved')
            time.sleep(20)

def run(args,name,phase,duration=None,decode=False,max_bytes=None):
    check_control()
    update(status='running',phase=phase,phase_progress_percent=None,eta_detail=name)
    log=WORK/(name+'.log')
    progress=WORK/(name+'-progress.txt')
    if progress.exists(): progress.unlink()
    if args[0]==FFMPEG:
        args=args[:1]+['-progress',str(progress),'-nostats']+args[1:]
    with log.open('wb') as stream:
        process=subprocess.Popen(args,stdout=stream,stderr=subprocess.STDOUT,cwd=WORK)
        try:
            last_status=time.monotonic()
            while process.poll() is None:
                time.sleep(.5)
                check_control()
                maybe_prefetch()
                if preflight_deadline and '-sample-' in name and time.monotonic()>preflight_deadline:
                    raise PreflightBudgetExceeded('Preflight time budget reached; original retained for a later targeted pass')
                if max_bytes and Path(args[-1]).exists() and Path(args[-1]).stat().st_size>max_bytes:
                    raise SizeBudgetExceeded('Encoded copy exceeded the source-adaptive size budget')
                if process.poll() is not None:
                    break
                if time.monotonic()-last_status<3:
                    continue
                last_status=time.monotonic()
                if shutil.disk_usage(ROOT).free<3*1024**3:
                    raise RuntimeError('Computer free space fell below 3 GiB; stopping this encode with backup retained.')
                if duration and progress.exists():
                    progress_text=progress.read_text(errors='replace')
                    values=re.findall(r'out_time_us=(\d+)',progress_text)
                    if values: update(phase_progress_percent=min(100,round(int(values[-1])/1e6/duration*100,1)))
                elif args[0]==str(ADB) and 'pull' in args and Path(args[-1]).exists():
                    update(phase_progress_percent=round(Path(args[-1]).stat().st_size/state['current_original_bytes']*100,1))
                else:
                    update()
            if process.returncode:
                error=MediaCommandError if args[0]==FFMPEG else RuntimeError
                raise error(f'{name} failed; see {log}')
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=30)
    if decode and log.stat().st_size: raise RuntimeError('Full decode reported errors: '+str(log))
    return log

def probe(path,packets=False):
    args=[FFPROBE,'-v','error']+(['-count_packets'] if packets else [])+['-show_streams','-show_format','-of','json',str(path)]
    return json.loads(subprocess.run(args,capture_output=True,check=True,timeout=180).stdout)

def rotation(stream):
    return [s.get('rotation') for s in stream.get('side_data_list',[]) if s.get('side_data_type')=='Display Matrix']

def video_pts(path):
    args=[FFPROBE,'-v','error','-select_streams','v:0','-show_packets','-show_entries','packet=pts_time','-of','json',str(path)]
    return sorted(float(p['pts_time']) for p in json.loads(subprocess.run(args,capture_output=True,check=True,timeout=180).stdout)['packets'])

def audio(path):
    args=[FFPROBE,'-v','error','-select_streams','a','-show_packets','-show_data_hash','sha256','-show_entries','packet=stream_index,data_hash,pts_time','-of','json',str(path)]
    return json.loads(subprocess.run(args,capture_output=True,check=True,timeout=180).stdout)['packets']

def finish_record(record,status,**values):
    record.update(status=status,**values)
    receipts['records'].append(record)
    write_json(RECEIPTS,receipts)

def reconcile_completed_transaction_receipts(receipt_history,transactions,latest_receipts):
    """Repair a stale/missing receipt only from a fully completed journal row."""
    latest_transactions={}
    for transaction in transactions:
        path=transaction.get('phone_path')
        if path:
            latest_transactions[path]=transaction
    repaired=False
    for path,transaction in latest_transactions.items():
        if (transaction.get('status')!='complete_temporary_copies_removed'
                or transaction.get('phone_hash_verified') is not True
                or transaction.get('media_index_updated') is not True
                or transaction.get('original_backup_deleted') is not True
                or transaction.get('compressed_pc_copy_deleted') is not True
                or not isinstance(transaction.get('saved_bytes'),int)
                or isinstance(transaction.get('saved_bytes'),bool)):
            continue
        latest=latest_receipts.get(path)
        if latest is not None and latest.get('status')=='completed':
            continue
        receipt=deepcopy(transaction)
        receipt.update(status='completed',recovered_from_transaction_journal=True,
                       saved_bytes=transaction['saved_bytes'],temporary_backup_deleted=True)
        receipt_history['records'].append(receipt)
        latest_receipts[path]=receipt
        repaired=True
    return repaired

def discard_unused_backup(path,backup,original_sha):
    # A rejected encode never replaces the phone file. Confirm the original
    # still exists there before removing its temporary computer copy.
    assert sha(backup)==original_sha
    assert shell('sha256sum '+shlex.quote(path)).split()[0]==original_sha
    backup.unlink()

def assess_quality(backup,compressed,width,height,duration,token,pixel_range,sample_starts=None,compressed_starts=None,hdr=False,section_offset=0):
    windows=[]
    if sample_starts is None:
        sample_starts=[round(max(0,min(duration*fraction,duration-3.05)),3) for fraction in (.12,.5,.85)]
    for j,start in enumerate(sample_starts):
        name=f'{token}-quality-{j}'
        filt=quality_filter(width,height,pixel_range,name,hdr)
        compressed_start=start if compressed_starts is None else compressed_starts[j]
        args=[FFMPEG,'-hide_banner','-nostdin','-threads','4','-noautorotate','-ss',str(compressed_start),'-t','3','-i',str(compressed),'-threads','4','-noautorotate','-ss',str(start),'-t','3','-i',str(backup),'-filter_complex',filt,'-map','[v]','-map','[p]','-map','[s]','-an','-f','null','-']
        log=run(args,name,f'Quality screening section {j+1+section_offset}/3')
        metric=json.loads((WORK/(name+'.json')).read_text())
        scores=sorted(f['metrics']['vmaf'] for f in metric['frames'])
        assert len(scores)>=3
        text=log.read_text(errors='replace')
        psnr=float(re.findall(r'PSNR y:\S+ u:\S+ v:\S+ average:(\S+)',text)[-1])
        ssim=float(re.findall(r'SSIM .*All:(\S+)',text)[-1])
        sample={'start_seconds':start,'sampled_frames':len(scores),'vmaf_mean':metric['pooled_metrics']['vmaf']['mean'],'vmaf_p5':scores[int(len(scores)*.05)],'psnr_average_db':psnr if math.isfinite(psnr) else None,'psnr_infinite':psnr==math.inf,'ssim_all':ssim,'quality_domain':'common HLG-to-SDR display transform for VMAF; native 10-bit PSNR/SSIM' if hdr else 'SDR'}
        sample['passes']=quality_passes(sample,hdr)
        windows.append(sample)
    return windows

inventory=json.loads((ROOT/'work/media-audit-2026-10-14/all-files.json').read_text(encoding='utf-8'))
initial=json.loads((ROOT/'outputs/phone-camera-compression-batch/manifest.json').read_text(encoding='utf-8'))
initial_backups={r['phone_path']:r for r in initial['records']}
indexed_paths=re.findall(r'_data=(.*?), _size=',(ROOT/'work/media-audit-2026-10-14/mediastore.txt').read_text(encoding='utf-8'))
media_paths={p.casefold():p for p in indexed_paths}
preflight_path=GLOBAL_OUT/'preflight-cache.json'
preflight={r['phone_path']:r for r in json.loads(preflight_path.read_text(encoding='utf-8'))['records']} if preflight_path.exists() else {}
# Reuse metadata already read from unchanged originals during earlier policies.
# In particular, do not copy gigabytes of known Dolby Vision just to defer it.
for receipt in receipts['records']:
    metadata=receipt.get('source_metadata')
    if metadata and metadata.get('duration') and receipt['phone_path'] not in preflight:
        preflight[receipt['phone_path']]={'original_bytes':receipt['original_bytes'],'mtime':receipt['mtime'],'probe':{'streams':[metadata],'format':{'duration':metadata['duration']}}}
allowed=('DCIM/Camera/','Download/','Movies/','Pictures/')
queue=[]
for row in inventory:
    if assigned_paths is not None and row['path'] not in assigned_paths: continue
    if not eligible_video(row): continue
    queue.append(row)
queue.sort(key=lambda r:(r['path'] in initial_backups,r['bytes']),reverse=True)
write_json(OUT/'queue.json',{'scope':'Public videos of at least 25 MiB, plus saved Instagram videos of any size. Hidden/private folders excluded. Unsupported formats or low savings are skipped.','records':queue})
latest_receipts={r['phone_path']:r for r in receipts['records']}
done={path for path,r in latest_receipts.items() if receipt_is_final(r)}
if not LANE: recover_pending()
transactions=read_json(GLOBAL_OUT/'transactions.json')
_last_journal_snapshot=transactions
reconcilable=[t for t in transactions['transactions'] if assigned_paths is None or t['phone_path'] in assigned_paths]
if reconcile_completed_transaction_receipts(receipts,reconcilable,latest_receipts):
    write_json(RECEIPTS,receipts)
done.update(r['phone_path'] for r in transactions['transactions'] if r.get('phone_hash_verified'))
unresolved=[] if LANE else [r for r in transactions['transactions'] if not r.get('phone_hash_verified')]
if unresolved:
    update(status='attention_required',phase='An incomplete phone transaction needs recovery before another replacement',error=unresolved[-1]['phone_path'])
    raise RuntimeError('Unresolved previous phone transaction')
pending=[r for r in queue if r['path'] not in done]
# Interrupted work first, then previously refused files under the new policy.
def retry_priority(row):
    receipt=latest_receipts.get(row['path'],{})
    status=receipt.get('status','')
    return 2 if status.startswith(('error_','paused_','pending_phone_transfer')) else 1 if status.startswith('skipped') and not receipt_is_final(receipt) else 0
pending.sort(key=lambda row:(latest_receipts.get(row['path'],{}).get('status')=='pending_phone_transfer',not cpu_only_row(row,preflight),retry_priority(row)),reverse=True)
state['cpu_only_queued']=sum(cpu_only_row(row,preflight) for row in pending)
write_json(OUT/'retry-plan.json',{'policy_version':VERSION,'priority_order':['gpu_eligible_interrupted','gpu_eligible_prior_refusals','other_videos','known_dolby_cpu_only'],'cpu_only_deferred_to_end':[r['path'] for r in pending if cpu_only_row(r,preflight)],'previously_compressed_excluded':sum(bool(r.get('phone_hash_verified')) for r in transactions['transactions']),'prior_refusals':[{'phone_path':row['path'],'previous_status':latest_receipts[row['path']]['status']} for row in pending if retry_priority(row)==1],'first_pending_paths':[row['path'] for row in pending[:12]]})
processed={r['path'] for r in queue if r['path'] in done}
total_bytes=sum(r['bytes'] for r in queue)
update(total_candidates=len(queue),total_original_bytes=total_bytes,processed_candidates=len(processed),processed_original_bytes=sum(r['bytes'] for r in queue if r['path'] in processed),remaining_candidates=len(pending),skipped_files=len({r['phone_path'] for r in receipts['records'] if r['status'].startswith('skipped') and r.get('policy_version',1)>=VERSION}))
stopped_by_control=False
update(phase='Checking available GPU encoders')
if LANE:
    encoder_capabilities=json.loads((GLOBAL_OUT/'encoder-capabilities.json').read_text(encoding='utf-8'))
    gpu_encoders=[key for key in ('nvenc','amf') if encoder_capabilities.get(key,{}).get('available')]
else:
    gpu_encoders,encoder_capabilities=detect_encoders(FFMPEG,WORK)
write_json(OUT/'encoder-capabilities.json',encoder_capabilities)
state['available_gpu_encoders']=gpu_encoders
eta_history=[]
if ETA_HISTORY.exists():
    for line in ETA_HISTORY.read_text(encoding='utf-8').splitlines():
        try: eta_history.append(json.loads(line))
        except json.JSONDecodeError: pass  # A crash can leave the last append incomplete.
eta_tracker=CompressionETA(eta_history,VERSION,('+'.join(gpu_encoders) or 'cpu')+'+pipeline'+('+'+LANE if LANE else ''),time.monotonic)
transfer_pipeline=PhoneTransferPipeline()
backup_prefetch=PhoneBackupPrefetch()
sample_rules_signature=hashlib.sha256(b''.join((Path(__file__).parent/name).read_bytes() for name in (Path(__file__).name,'compression_policy.py','adaptive_encode.py','video_encode_options.py','video_quality_filters.py','compression_workflow.py'))).hexdigest()
remaining_original_bytes=sum(row['bytes'] for row in pending)
update(remaining_original_bytes=remaining_original_bytes)

try:
    for number,row in enumerate(pending):
        if LANE and command() in ('pause','stop'):
            stopped_by_control=True
            update(status='paused' if command()=='pause' else 'stopped',phase='Processing paused safely; backup retained')
            break
        if LANE and not claim_file(GLOBAL_OUT,os.environ['PHONE_COMPRESSION_RUN_ID'],LANE,row['path']):
            continue
        current_queue_index=number
        path=row['path']
        token=hashlib.sha256(path.encode()).hexdigest()[:12]
        backup=TEMP/(token+'-original'+Path(path).suffix)
        compressed=TEMP/(token+'-1080p.mp4')
        raw=WORK/(token+'-raw.mp4')
        record={'phone_path':path,'media_store_path':media_paths.get(path.casefold(),path),'original_bytes':row['bytes'],'mtime':row['mtime'],'policy':'Source-adaptive bitrate; GPU-first; maximum 1080p; original FPS/audio/colour range retained','policy_version':VERSION,'started_utc':datetime.now(timezone.utc).isoformat()}
        record['backup_path']=str(backup)
        eta_tracker.begin_file(path,row['bytes'])
        update(current_file=path,current_original_bytes=row['bytes'],remaining_candidates=len(pending)-number,remaining_original_bytes=remaining_original_bytes,phase='Connecting',error=None,active_encoder=None)
        try:
            check_control()
            if reservations is not None:
                update(phase='Waiting for shared disk budget',status='running')
                previous_backup=initial_backups.get(path,{}).get('backup_path')
                reservation_source=Path(previous_backup) if previous_backup and Path(previous_backup).exists() else backup
                reservation_paths=[reservation_source,raw,compressed]
                if not reservations.acquire(reservation_owner,row['bytes'],reservation_paths,lambda:command() in ('pause','stop')):
                    raise PauseRequested(command())
            wait_phone()
            await_prefetch(path)
            current=tuple(map(int,shell('stat -c %s,%Y '+shlex.quote(path)).split(',')))
            if current!=(row['bytes'],row['mtime']):
                finish_record(record,'skipped_changed_since_inventory')
                continue
            cached=preflight.get(path)
            if cached and cached['original_bytes']==row['bytes'] and cached['mtime']==row['mtime']:
                cached_video=next(s for s in cached['probe']['streams'] if s['codec_type']=='video')
                cached_duration=float(cached['probe']['format']['duration'])
                cached_route,cached_reason=classify(cached_video,cached_duration)
                if cached_route is None:
                    finish_record(record,'skipped_'+cached_reason,source_metadata=cached_video,phone_original_unchanged=True)
                    continue
            previous=initial_backups.get(path)
            if previous and Path(previous['backup_path']).exists():
                backup=Path(previous['backup_path'])
            else:
                # Reserve enough space for source, raw encode, finalized copy and logs.
                if reservations is None and shutil.disk_usage(ROOT).free<row['bytes']*3+8*1024**3:
                    finish_record(record,'skipped_computer_space_limit')
                    continue
                update(phase='Backing up this batch to computer')
                if backup.exists():
                    update(phase='Checking backup left by previous run')
                    live_hash=shell('sha256sum '+shlex.quote(path)).split()[0]
                    assert tuple(map(int,shell('stat -c %s,%Y '+shlex.quote(path)).split(',')))==current
                    if backup.stat().st_size!=row['bytes'] or sha(backup)!=live_hash:
                        assert backup.resolve().is_relative_to(TEMP.resolve())
                        backup.unlink()  # Incomplete task copy; verified live original remains.
                if not backup.exists():
                    run([str(ADB),'-s',transport,'pull',path,str(backup)],token+'-backup','Backing up this batch to computer')
            record['backup_path']=str(backup)
            update(phase='Verifying original backup')
            original_sha=sha(backup)
            assert backup.stat().st_size==row['bytes'] and shell('sha256sum '+shlex.quote(path)).split()[0]==original_sha
            assert tuple(map(int,shell('stat -c %s,%Y '+shlex.quote(path)).split(',')))==current
            record['original_sha256']=original_sha
            info=probe(backup,True)
            v=next(s for s in info['streams'] if s['codec_type']=='video')
            duration=float(info['format']['duration'])
            record['source_metadata']=v
            record['source_duration_seconds']=duration
            colors=['color_range','color_space','color_transfer','color_primaries']
            enums=COLOR_ENUMS
            route,reason=classify(v,duration)
            if route is None:
                finish_record(record,'skipped_'+reason,source_metadata=v)
                discard_unused_backup(path,backup,original_sha)
                continue
            if route['dolby'] and os.environ.get('PHONE_VIDEO_CPU_ALLOWED','1')=='0':
                finish_record(record,'deferred_cpu_reserved_for_photos',source_metadata=v)
                discard_unused_backup(path,backup,original_sha)
                continue
            w,h=v['width'],v['height']
            factor=min(1,(1920/w if w>=h else 1080/w),(1080/h if w>=h else 1920/h))
            width,height=int(w*factor)//2*2,int(h*factor)//2*2
            record['original_resolution']=[w,h]
            record['output_resolution']=[width,height]
            color_args=['-color_range',v['color_range'],'-colorspace',v['color_space'],'-color_trc',v['color_transfer'],'-color_primaries',v['color_primaries']]
            pixel_range='full' if v['color_range']=='pc' else 'limited'
            scaling=f'scale={width}:{height}:flags=lanczos:in_range={pixel_range}:out_range={pixel_range}'
            bitrate_plan=plan_bitrates(row['bytes'],duration,v,[s for s in info['streams'] if s['codec_type']=='audio'],(width,height))
            record['bitrate_plan']=bitrate_plan
            if not bitrate_plan['viable']:
                finish_record(record,'skipped_no_safe_bitrate_budget')
                discard_unused_backup(path,backup,original_sha)
                continue
            # Hardware may fail for a particular colour format even if its basic
            # probe passed. Screen every route on short clips before full encoding.
            encoders=permitted_video_encoders(gpu_encoders,LANE,route['dolby'],os.environ.get('PHONE_VIDEO_CPU_ALLOWED','1')!='0')
            candidates=[(encoder,bitrate) for encoder in encoders for bitrate in bitrate_plan['targets']]
            record['attempts']=[]
            previous_screen=latest_receipts.get(path,{})
            reusable_rejections={}
            if previous_screen.get('status')=='paused_backup_retained' and previous_screen.get('policy_version')==VERSION and previous_screen.get('original_sha256')==original_sha and previous_screen.get('bitrate_plan')==bitrate_plan and previous_screen.get('output_resolution')==[width,height]:
                reusable_rejections={trial['encoder']:trial for trial in previous_screen.get('attempts',[]) if trial.get('sample_preflight_only') and trial.get('result') in ('quality_screen_failed','sample_size_budget_exceeded','encoder_error')}
            disabled_encoders=set()
            preflight_deadline=time.monotonic()+(300 if route['hdr'] else 180)
            accepted=False
            for encoder,bitrate in candidates:
                if encoder in disabled_encoders:
                    continue
                attempt=f'{encoder}-bps{bitrate}'
                if attempt in reusable_rejections:
                    trial={**reusable_rejections[attempt],'reused_preflight_rejection':True}
                    record['attempts'].append(trial)
                    if trial['result']=='encoder_error':
                        disabled_encoders.add(encoder)
                    continue
                record['encoder_attempt']=attempt
                state['active_encoder']=encoder
                args=[FFMPEG,'-hide_banner','-nostdin','-y','-threads','4','-noautorotate','-i',str(backup),'-map','0:v:0','-map','0:a?','-map_metadata','0',*encoder_options(encoder,bitrate,bitrate_plan['max_video_bps'],route['pixel_format'],route['dolby']),'-vf',scaling,*color_args,'-fps_mode','passthrough','-enc_time_base:v','demux','-c:a','copy','-tag:v','hvc1','-movflags','+faststart+use_metadata_tags',str(raw)]
                bsf=f'hevc_metadata=colour_primaries={enums[v["color_primaries"]]}:transfer_characteristics={enums[v["color_transfer"]]}:matrix_coefficients={enums[v["color_space"]]}:video_full_range_flag={int(v["color_range"]=="pc")}'
                checkpoint_path=WORK/(token+'-'+attempt+'-encode-checkpoint.json')
                identity={'original_sha256':original_sha,'encoder':attempt,'policy_version':VERSION,'output_resolution':[width,height],'source_colour':{key:v[key] for key in colors},'pixel_format':route['pixel_format'],'dolby':route['dolby'],'max_video_bps':bitrate_plan['max_video_bps']}
                checkpoint=json.loads(checkpoint_path.read_text(encoding='utf-8')) if checkpoint_path.exists() else {}
                reuse=all(checkpoint.get(key)==value for key,value in identity.items()) and compressed.exists() and checkpoint.get('compressed_sha256')==sha(compressed)
                if reuse:
                    update(phase='Reusing encoded copy; all validation checks will run again')
                else:
                    # Screen size and quality before any full encode, at every resolution.
                    sample_checkpoint=WORK/(token+'-'+attempt+'-preflight-checkpoint.json')
                    sample_identity={**identity,'encode_args':args,'remux_args':remux_args(FFMPEG,raw,compressed,bsf,route['dolby']),'rules_signature':sample_rules_signature,'ffmpeg_size':Path(FFMPEG).stat().st_size,'ffmpeg_mtime_ns':Path(FFMPEG).stat().st_mtime_ns}
                    starts=[round(max(0,min(duration*fraction,duration-3.05)),3) for fraction in (.12,.5,.85)]
                    try:
                        saved_screen=json.loads(sample_checkpoint.read_text(encoding='utf-8'))
                    except (OSError,json.JSONDecodeError):
                        saved_screen=None
                    reuse_screen=valid_preflight(saved_screen,sample_identity,starts,route['hdr'])
                    sample_windows=saved_screen['quality_windows'] if reuse_screen else []
                    sample_sizes=saved_screen['sample_sizes'] if reuse_screen else []
                    sample_encode_times=saved_screen['sample_encode_seconds'] if reuse_screen else []
                    if reuse_screen:
                        record['reused_successful_preflight']=True
                        update(phase='Reusing verified sample preflight; full output checks retained')
                    sample_error=None
                    for j,fraction in enumerate(() if reuse_screen else (.12,.5,.85)):
                        start=round(max(0,min(duration*fraction,duration-3.05)),3)
                        sample=WORK/(token+'-'+attempt+f'-sample-{j}.mp4')
                        sample_raw=WORK/(token+'-'+attempt+f'-sample-{j}-raw.mp4')
                        input_index=args.index('-i')
                        sample_args=args[:input_index]+['-ss',str(start)]+args[input_index:-1]+['-t','3',str(sample_raw)]
                        try:
                            sample_started=time.monotonic()
                            run(sample_args,token+'-'+attempt+f'-sample-{j}',f'Testing adaptive bitrate {attempt}, section {j+1}/3',3)
                            sample_encode_times.append(time.monotonic()-sample_started)
                            # Screen the same retagged representation used for a
                            # full candidate, not incomplete hardware VUI tags.
                            run(remux_args(FFMPEG,sample_raw,sample,bsf,route['dolby']),token+'-'+attempt+f'-sample-{j}-retag','Preserving source colour metadata in sample')
                            sample_sizes.append(sample.stat().st_size)
                            sample_info=probe(sample)
                            sample_video=next(s for s in sample_info['streams'] if s['codec_type']=='video')
                            if not all(v[k]==sample_video[k] for k in colors):
                                differences={key:{'source':v[key],'encoded':sample_video.get(key)} for key in colors if v[key]!=sample_video.get(key)}
                                raise MediaCommandError('Sample encoder changed source colour metadata: '+json.dumps(differences))
                            if route['pixel_format']=='yuv420p10le' and sample_video.get('pix_fmt')!='yuv420p10le':
                                raise MediaCommandError('Sample encoder did not retain 10-bit pixels')
                            if route['dolby']:
                                validate_config(v,sample_video)
                            sample_windows.extend(assess_quality(backup,sample,width,height,duration,token+'-'+attempt+f'-sample-{j}',pixel_range,[start],[0],route['hdr'],section_offset=j))
                        except MediaCommandError as error:
                            sample_error=str(error)
                            break
                        finally:
                            sample_raw.unlink(missing_ok=True)
                            sample.unlink(missing_ok=True)
                        if not sample_windows[-1]['passes']:
                            break
                    if sample_error:
                        disabled_encoders.add(encoder)
                        record['attempts'].append({'encoder':attempt,'result':'encoder_error','sample_preflight_only':True,'encoder_disabled_for_file':True,'error':sample_error})
                        continue
                    if not all(window['passes'] for window in sample_windows):
                        record['attempts'].append({'encoder':attempt,'result':'quality_screen_failed','sample_preflight_only':True,'quality_windows':sample_windows})
                        continue
                    projected=estimate_projected_output_bytes(sample_sizes,[3]*len(sample_sizes),duration,bitrate_plan['mux_reserve_bps'])
                    if projected>bitrate_plan['max_output_bytes']:
                        record['attempts'].append({'encoder':attempt,'result':'sample_size_budget_exceeded','sample_preflight_only':True,'projected_bytes':projected,'quality_windows':sample_windows})
                        continue
                    if not reuse_screen:
                        write_json(sample_checkpoint,{'identity':sample_identity,'quality_windows':sample_windows,'sample_sizes':sample_sizes,'sample_encode_seconds':sample_encode_times})
                    eta_tracker.selected_encode(sum(sample_encode_times)/(3*len(sample_encode_times))*duration,attempt)
                    try:
                        # At most one full attempt per file. A size failure must
                        # never trigger a still larger, slower quality ladder.
                        run(args,token+'-'+attempt+'-encode','Encoding 1080p copy ('+attempt+')',duration,max_bytes=bitrate_plan['max_output_bytes'])
                        run(remux_args(FFMPEG,raw,compressed,bsf,route['dolby']),token+'-'+attempt+'-retag','Preserving source colour metadata')
                    except MediaCommandError as error:
                        record['attempts'].append({'encoder':attempt,'result':'less_than_10_percent_saving' if isinstance(error,SizeBudgetExceeded) else 'encoder_error','error':str(error)})
                        break
                    write_json(checkpoint_path,{**identity,'compressed_sha256':sha(compressed)})
                record['compressed_bytes']=compressed.stat().st_size
                trial={'encoder':attempt,'compressed_bytes':record['compressed_bytes'],'reused_encoded_copy':reuse}
                record['attempts'].append(trial)
                if record['compressed_bytes']>bitrate_plan['max_output_bytes']:
                    trial['result']='less_than_10_percent_saving'
                    break
                final=probe(compressed,True)
                fv=next(s for s in final['streams'] if s['codec_type']=='video')
                assert (fv['width'],fv['height'])==(width,height)
                assert all(v[k]==fv[k] for k in colors) and rotation(v)==rotation(fv)
                assert v['nb_read_packets']==fv['nb_read_packets'] and abs(duration-float(final['format']['duration']))<.1
                record['recording_metadata_checks']=validate_recording_tags(info['format'].get('tags',{}),final['format'].get('tags',{}))
                if v.get('tags',{}).get('timecode'):
                    if v['tags']['timecode']!=fv.get('tags',{}).get('timecode'):
                        raise ValueError('Video timecode changed after encoding')
                    record['video_timecode_preserved']=True
                if route['dolby']:
                    record['dolby_container_checks']=validate_config(v,fv)
                windows=assess_quality(backup,compressed,width,height,duration,token+'-'+attempt,pixel_range,hdr=route['hdr'])
                trial['quality_windows']=windows
                record['quality_windows']=windows
                if not all(sample['passes'] for sample in windows):
                    trial['result']='quality_screen_failed'
                    break
                trial['result']='accepted'
                accepted=True
                break
            if not accepted:
                reason=failure_reason(record['attempts'])
                finish_record(record,'skipped_'+reason)
                discard_unused_backup(path,backup,original_sha)
                for temporary in [compressed,raw]: temporary.unlink(missing_ok=True)
                continue
            update(phase='Checking all frame timestamps and copied audio')
            a,b=video_pts(backup),video_pts(compressed)
            aa,ab=audio(backup),audio(compressed)
            record.update(validate_timing(a,b,aa,ab),source_colour_preserved=True,orientation_preserved=True,recording_metadata_preserved=True)
            if route['dolby']:
                metadata_files=[]
                for label,media in (('source',backup),('target',compressed)):
                    metadata_file=WORK/(token+'-dolby-'+label+'-frames.json')
                    run([FFPROBE,'-v','error','-threads','4','-select_streams','v:0','-show_frames','-show_entries',FRAME_ENTRIES,'-of','json','-o',str(metadata_file),str(media)],token+'-dolby-'+label+'-probe','Checking Dolby Vision metadata on every '+label+' frame',decode=True)
                    metadata_files.append(metadata_file)
                update(phase='Comparing Dolby Vision metadata and timestamps on every frame')
                record['dolby_every_frame_checks']=compare_frames(*metadata_files,len(a))
            run([FFMPEG,'-v','error','-nostdin','-threads','4','-noautorotate','-i',str(compressed),'-map','0:v:0','-map','0:a?','-fps_mode','passthrough','-enc_time_base:v','demux','-f','null','-'],token+'-decode','Full decode validation',duration,decode=True)
            assert sha(backup)==original_sha
            record.update(compressed_path=str(compressed),compressed_sha256=sha(compressed),full_decode_no_errors=True)
            check_control()
            while transfer_pipeline.busy:
                check_control()
                if transfer_pipeline.busy:
                    update(phase='Waiting for previous phone transfer slot',status='running')
                    time.sleep(.5)
            record['processing_timing']=eta_tracker.finish_file('pending_phone_transfer',defer_transfer=True)
            record['processing_timing']['compressed_bytes']=record['compressed_bytes']
            finish_record(record,'pending_phone_transfer')
            transfer_record=record
            transfer_started=time.monotonic()
            transfer_pipeline.submit(record,verified_phone_transfer)
            if raw.exists(): raw.unlink()
            update(phase='Local validation complete; phone transfer running in background')
            if LANE:
                # One original batch per lane. Other lanes continue encoding
                # while this one finishes its serialized phone transaction.
                while transfer_pipeline.busy:
                    collect_transfer()
                    if transfer_pipeline.busy:
                        update(phase='Replacing phone file after successful checks',status='running')
                        time.sleep(.5)
        except PreflightBudgetExceeded as error:
            finish_record(record,'skipped_preflight_time_budget',error=str(error))
            discard_unused_backup(path,backup,original_sha)
            for temporary in (compressed,raw):temporary.unlink(missing_ok=True)
        except (DolbyValidationError,MediaCommandError,TimingValidationError) as error:
            # Local media commands and Dolby validation precede phone replacement.
            status=('skipped_timestamp_validation_failed' if isinstance(error,TimingValidationError) else 'skipped_dolby_vision_validation_failed' if isinstance(error,DolbyValidationError) else 'skipped_media_validation_command_failed')
            finish_record(record,status,error=str(error))
            discard_unused_backup(path,backup,original_sha)
            for temporary in (compressed,raw):temporary.unlink(missing_ok=True)
        except PauseRequested as pause:
            stopped_by_control=True
            finish_record(record,'paused_backup_retained',command=pause.command)
            update(status='paused' if pause.command=='pause' else 'stopped',phase='Processing paused safely; backup retained',phase_progress_percent=None)
            break
        except Exception as error:
            record['error']=str(error) or type(error).__name__
            finish_record(record,'error_backup_retained',traceback=traceback.format_exc())
            update(status='attention_required',phase='Stopped with backup retained',error=record['error'])
            print(traceback.format_exc(),flush=True)
            raise
        finally:
            timing=eta_tracker.finish_file(record.get('status',''))
            if timing:
                record['processing_timing']=timing
                with ETA_HISTORY.open('a',encoding='utf-8') as stream:
                    stream.write(json.dumps(timing)+'\n')
                write_json(RECEIPTS,receipts)
            latest_receipts={r['phone_path']:r for r in receipts['records']}
            finished={path for path,r in latest_receipts.items() if receipt_is_final(r)}
            finished.update(done)
            state['skipped_files']=len({r['phone_path'] for r in receipts['records'] if r['status'].startswith('skipped') and r.get('policy_version',1)>=VERSION})
            state['processed_candidates']=sum(r['path'] in finished for r in queue)
            state['processed_original_bytes']=sum(r['bytes'] for r in queue if r['path'] in finished)
            state['remaining_candidates']=state['total_candidates']-state['processed_candidates']
            if path in finished:
                remaining_original_bytes-=row['bytes']
            state['remaining_original_bytes']=remaining_original_bytes
            update()
            if reservations is not None and not transfer_pipeline.busy:
                reservations.release(reservation_owner)
finally:
    try:
        close_pipeline()
    finally:
        if reservations is not None:
            reservations.release(reservation_owner)
if not stopped_by_control:
    update(status='complete',phase='No unclaimed eligible videos remain for this worker',current_file=None,remaining_candidates=0)
