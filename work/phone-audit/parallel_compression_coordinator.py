"""Supervise disjoint encoder lanes; publish one durable dashboard/receipt feed."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone

from adaptive_encode import detect_encoders
from compression_policy import VERSION, receipt_is_final
from compression_workflow import cpu_only_row
from compression_scope import eligible_video
from compression_work_claims import with_cpu_backlog
from media_execution_mode import plan_video_work
from parallel_resource_locks import file_mutex
from phone_video_transaction import recover_pending
from shared_json import read_json,MISSING

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'outputs/phone-video-compression-batches'
WORK=ROOT/'work/phone-video-compression-batches'
_read_snapshots={}


def read(path,default):
    if Path(path).name not in ('worker-status.json','transactions.json','validation-receipts.json'):
        return read_json(path,default=default)
    key=str(path)
    try:
        value=read_json(path,default=MISSING if key in _read_snapshots else default,attempts=3 if key in _read_snapshots else 60)
        _read_snapshots[key]=value
        return value
    except (PermissionError,FileNotFoundError,TimeoutError,json.JSONDecodeError):
        if key not in _read_snapshots:raise
        # Publication only once the initial snapshot is known. Startup recovery
        # and phone mutations use their own fresh, strict journal reads.
        return _read_snapshots[key]


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.coordinator-new.json')
    temporary.write_text(json.dumps(value,indent=2),encoding='utf-8')
    for _ in range(50):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            time.sleep(.1)
    temporary.replace(path)


def assign_lanes(pending,cache,available,cpu_small_limit=8):
    """Disjoint static assignment; bounded tiny CPU batch plus known Dolby."""
    lanes={name:[] for name in available}
    lanes['cpu']=[]
    ordinary=[]
    for row in pending:
        if cpu_only_row(row,cache):
            lanes['cpu'].append(row)
        else:
            ordinary.append(row)
    tiny=sorted((r for r in ordinary if r['bytes']<=50*1024**2),key=lambda r:r['bytes'])[:cpu_small_limit] if available else []
    tiny_paths={r['path'] for r in tiny}
    lanes['cpu']=tiny+lanes['cpu']
    loads={key:0 for key in available}
    # Equal input-byte load is an initial scheduling heuristic, not a measured
    # assertion that different encoders or source formats have equal speed.
    for row in ordinary:
        if row['path'] in tiny_paths:
            continue
        if not available:
            lanes['cpu'].append(row)
        else:
            lane=min(available,key=lambda name:loads[name])
            lanes[lane].append(row)
            loads[lane]+=row['bytes']
    return {key:rows for key,rows in lanes.items() if rows}


def merge_receipts(baseline,lane_receipts):
    result=list(baseline)
    for records in lane_receipts:
        result.extend(records[len(baseline):])
    return {'records':result}


def reconcile_baseline(baseline,transactions):
    repaired=list(baseline)
    latest={r['phone_path']:r for r in repaired}
    for transaction in transactions:
        path=transaction['phone_path']
        if (transaction.get('status')=='complete_temporary_copies_removed'
                and all(transaction.get(key) is True for key in ('phone_hash_verified','media_index_updated','original_backup_deleted','compressed_pc_copy_deleted'))
                and isinstance(transaction.get('saved_bytes'),int) and not isinstance(transaction.get('saved_bytes'),bool)
                and latest.get(path,{}).get('status')!='completed'):
            receipt=deepcopy(transaction)
            receipt.update(status='completed',temporary_backup_deleted=True,recovered_from_transaction_journal=True)
            repaired.append(receipt)
            latest[path]=receipt
    return repaired


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    with file_mutex(OUT/'worker.lock',timeout=1):
        # Detect surviving lane processes BEFORE touching their state files.
        # The supervisor normally kills them on parent failure; these leases
        # also close the brief restart race before that cleanup has finished.
        with ExitStack() as leases:
            for key in ('nvenc','amf','cpu'):
                leases.enter_context(file_mutex(OUT/'parallel'/key/'worker.lock',timeout=1))
            return run(leases)


def run(startup_leases):
    started=time.monotonic()
    baseline=read(OUT/'validation-receipts.json',{'records':[]})['records']
    try:
        # Only the coordinator performs startup recovery, before any lane can
        # submit a new phone transaction.
        recover_pending()
    except Exception as error:
        save(OUT/'worker-status.json',{'status':'attention_required','phase':'Phone transaction recovery needs attention; backups retained','error':str(error),'updated_utc':datetime.now(timezone.utc).isoformat()})
        return 1
    queue=read(OUT/'queue.json',{'records':[]})['records']
    # Extend the persisted queue when the user authorizes saved Instagram media.
    inventory=read(ROOT/'work/media-audit-2026-10-14/all-files.json',[])
    known={row['path'] for row in queue}
    queue.extend(row for row in inventory if row['path'] not in known and eligible_video(row))
    save(OUT/'queue.json',{'scope':'Public videos >=25 MiB plus saved Instagram videos of any size; hidden/private excluded.','records':queue})
    if len({row['path'] for row in queue})!=len(queue):
        raise RuntimeError('Duplicate queue paths; refusing parallel dispatch')
    latest={r['phone_path']:r for r in baseline}
    transactions=read(OUT/'transactions.json',{'transactions':[]})['transactions']
    baseline=reconcile_baseline(baseline,transactions)
    save(OUT/'validation-receipts.json',{'records':baseline})
    latest={r['phone_path']:r for r in baseline}
    completed_before={t['phone_path'] for t in transactions if t.get('phone_hash_verified')}
    done=completed_before|{path for path,r in latest.items() if receipt_is_final(r)}
    pending=[r for r in queue if r['path'] not in done]
    pending.sort(key=lambda row:2 if latest.get(row['path'],{}).get('status','').startswith(('paused_','error_','pending_phone_transfer')) else 1 if latest.get(row['path'],{}).get('status','').startswith('skipped') else 0,reverse=True)
    cache=read(OUT/'preflight-cache.json',{'records':[]})
    cache={r['phone_path']:r for r in cache['records']}
    for receipt in baseline:
        metadata=receipt.get('source_metadata',{})
        if receipt['phone_path'] not in cache and metadata.get('duration'):
            cache[receipt['phone_path']]={'original_bytes':receipt['original_bytes'],'mtime':receipt['mtime'],'probe':{'streams':[metadata],'format':{'duration':metadata['duration']}}}
    available,capabilities=detect_encoders('C:/ProgramData/chocolatey/lib/ffmpeg/tools/ffmpeg/bin/ffmpeg.exe',WORK)
    save(OUT/'encoder-capabilities.json',capabilities)
    execution_config=read(OUT/'parallel-config.json',{})
    lanes,deferred=plan_video_work(pending,cache,available,execution_config,assign_lanes,with_cpu_backlog)
    save(OUT/'deferred-cpu-videos.json',{'reason':'CPU reserved for photos' if deferred else None,'records':deferred})
    save(OUT/'parallel-assignment.json',{'policy_version':VERSION,'created_utc':datetime.now(timezone.utc).isoformat(),'scope':'GPU assignments plus shared small-video CPU backlog; atomic per-run claims give each file one owner. One original per lane and one global phone mutation lock.','cpu_small_limit':8,'lanes':{key:{'files':len(rows),'original_bytes':sum(r['bytes'] for r in rows),'paths':[r['path'] for r in rows]} for key,rows in lanes.items()}})
    streams=[]
    children={}
    error=None
    active_wall=0.0
    last=time.monotonic()
    previous_active=True
    published={'status':'running','phase':'Starting parallel encoding lanes','updated_utc':datetime.now(timezone.utc).isoformat()}
    run_id=uuid.uuid4().hex
    for key,rows in lanes.items():
        folder=OUT/'parallel'/key
        folder.mkdir(parents=True,exist_ok=True)
        save(folder/'jobs.json',{'paths':[r['path'] for r in rows]})
        save(folder/'validation-receipts.json',{'records':baseline})
        save(folder/'worker-status.json',{'status':'running','phase':'initializing','current_file':None,'updated_utc':datetime.now(timezone.utc).isoformat()})
    save(OUT/'parallel-active-run.json',{'id':run_id,'coordinator_pid':os.getpid(),'created_utc':datetime.now(timezone.utc).isoformat()})
    save(OUT/'work-claims.json',{'run_id':run_id,'owners':{}})
    startup_leases.close()
    try:
        for key,rows in lanes.items():
            if read(OUT/'control.json',{}).get('command','run') in ('pause','stop'):
                break
            folder=OUT/'parallel'/key
            manifest=folder/'jobs.json'
            env={**os.environ,'PHONE_COMPRESSION_LANE':key,'PHONE_COMPRESSION_JOBS':str(manifest),'PHONE_COMPRESSION_RUN_ID':run_id,'PHONE_VIDEO_CPU_ALLOWED':'1' if execution_config.get('cpu_video_enabled',True) else '0'}
            stream=(folder/'worker.log').open('ab',buffering=0)
            streams.append(stream)
            children[key]=subprocess.Popen([sys.executable,str(Path(__file__).with_name('compress_phone_video_batches.py'))],cwd=ROOT,env=env,stdout=stream,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        while True:
            now=time.monotonic()
            if previous_active: active_wall+=now-last
            last=now
            statuses={key:read(OUT/'parallel'/key/'worker-status.json',{}) for key in children}
            receipts=merge_receipts(baseline,[read(OUT/'parallel'/key/'validation-receipts.json',{'records':baseline})['records'] for key in children])
            save(OUT/'validation-receipts.json',receipts)
            latest={r['phone_path']:r for r in receipts['records']}
            journal=read(OUT/'transactions.json',{'transactions':[]})['transactions']
            verified={t['phone_path'] for t in journal if t.get('phone_hash_verified')}
            final=verified|{path for path,r in latest.items() if receipt_is_final(r)}
            remaining=[r for r in queue if r['path'] not in final]
            completed_new=[t for t in journal if t.get('phone_hash_verified') and t['phone_path'] not in completed_before]
            new_bytes=sum(t['original_bytes'] for t in completed_new)
            eta_seconds=sum(r['bytes'] for r in remaining)*active_wall/new_bytes if len(completed_new)>=3 and new_bytes else None
            if eta_seconds is not None:
                tails=[s.get('eta',{}).get('phase',{}).get('remaining_seconds',0) for s in statuses.values() if isinstance(s.get('eta',{}).get('phase'),dict) and s.get('status')=='running']
                eta_seconds=max([eta_seconds,*tails])
            running=[key for key,child in children.items() if child.poll() is None]
            failed=[key for key,child in children.items() if child.poll() not in (None,0)]
            if failed and error is None:
                error='; '.join(key+': '+str(statuses[key].get('error') or 'worker exited '+str(children[key].returncode)+'; see parallel/'+key+'/worker.log') for key in failed)
                save(OUT/'control.json',{'command':'stop'})
            control=read(OUT/'control.json',{}).get('command','run')
            previous_active=any(statuses[key].get('status')=='running' for key in running)
            workers=[{'lane':key,'pid':children[key].pid,**statuses[key]} for key in children]
            primary=next((statuses[key] for key in running if statuses[key].get('phase','').startswith('Encoding ')),next((statuses[key] for key in running),{}))
            status='running' if running else 'attention_required' if error else 'paused' if control=='pause' else 'stopped' if control=='stop' else 'complete'
            global_eta=None if eta_seconds is None else {'remaining_seconds':round(eta_seconds),'low_seconds':round(eta_seconds*.5),'high_seconds':round(eta_seconds*2),'basis':'observed_parallel_wall_throughput','samples':len(completed_new)}
            skips=[r for r in latest.values() if r.get('status','').startswith('skipped') and receipt_is_final(r)]
            reasons={}
            for receipt in skips: reasons[receipt['status']]=reasons.get(receipt['status'],0)+1
            published={**primary,'status':status,'parallel_encoding_enabled':True,'pipeline_mode':'three_encoder_processes_serialized_phone','workers':workers,'active_lane_count':len(running),'phase':primary.get('phase','Parallel encoding finished' if not running else 'Starting parallel encoding lanes'),'error':error,'updated_utc':datetime.now(timezone.utc).isoformat(),'completed_files':len(verified),'saved_bytes':sum(t.get('saved_bytes',0) for t in journal if t.get('phone_hash_verified')),'total_candidates':len(queue),'total_original_bytes':sum(r['bytes'] for r in queue),'remaining_candidates':len(remaining),'remaining_original_bytes':sum(r['bytes'] for r in remaining),'processed_candidates':len(queue)-len(remaining),'processed_original_bytes':sum(r['bytes'] for r in queue if r['path'] in final),'skipped_files':len(skips),'skip_reasons':reasons,'pending_prior_skips':sum(r.get('status','').startswith('skipped') and not receipt_is_final(r) for r in latest.values()),'available_gpu_encoders':available,'phone_transfer':next((worker.get('phone_transfer') for worker in workers if worker.get('phone_transfer')),None),'backup_prefetch':None,'eta':{**primary.get('eta',{}),'global':global_eta,'suspended':status!='running','calibration_completed_files':len(completed_new),'calibration_processed_files':len(completed_new),'calibration_minimum_files':3,'regime':'parallel-nvenc-amf-cpu','reason':'new_regime_calibration' if global_eta is None else None},'parallel_active_seconds':round(active_wall,3)}
            deferred_paths={row['path'] for row in deferred}|{path for path,r in latest.items() if r.get('status')=='deferred_cpu_reserved_for_photos'}
            published['deferred_cpu_candidates']=len(deferred_paths)
            published['cpu_video_enabled']=execution_config.get('cpu_video_enabled',True)
            if not running and not error and control=='run' and deferred_paths:
                published['phase']='GPU video work complete; CPU-only videos deferred for photos'
            save(OUT/'worker-status.json',published)
            if not running: break
            time.sleep(1)
    except BaseException as failure:
        error=str(failure) or type(failure).__name__
        save(OUT/'control.json',{'command':'stop'})
        # Never terminate an in-flight replacement. Each child observes stop
        # and drains its started phone transaction before releasing its lease.
        while any(child.poll() is None for child in children.values()):
            time.sleep(.5)
        published.update(status='attention_required',error=error,phase='Parallel coordinator stopped; available backups retained',updated_utc=datetime.now(timezone.utc).isoformat())
        save(OUT/'worker-status.json',published)
    finally:
        for stream in streams: stream.close()
        save(OUT/'parallel-run-summary.json',{'ended_utc':datetime.now(timezone.utc).isoformat(),'wall_seconds':round(time.monotonic()-started,3),'active_seconds':round(active_wall,3),'error':error,'lanes':{key:{'pid':child.pid,'exit_code':child.poll()} for key,child in children.items()},'completed_files':published.get('completed_files'),'saved_bytes':published.get('saved_bytes')})
    return 1 if error else 0


if __name__=='__main__':
    raise SystemExit(main())
