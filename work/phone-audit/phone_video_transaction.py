"""Verified replacement of one authorized phone video; durable journal before mutation."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import shlex
import subprocess
import time
import re
import os
from parallel_resource_locks import file_mutex
from device import phone_transport,read_phone_shell,PhoneUnavailableError
from shared_json import read_json,journal_io
from compression_scope import public_video_path

ROOT = Path(__file__).resolve().parents[2]
ADB = ROOT/'work/android-tools/platform-tools/adb.exe'
OUT = ROOT/'outputs/phone-video-compression-batches'
OUT.mkdir(exist_ok=True)
JOURNAL = OUT/'transactions.json'

def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def apply(record, remove_temporary=True, resume_index=None):
    # Read the journal only AFTER this lock: independent encoder processes
    # must never overwrite another transaction's journal snapshot.
    with file_mutex(OUT/'phone-transaction.lock'):
        for attempt in range(3):
            try:
                return _apply_locked(record,remove_temporary,resume_index)
            except (subprocess.CalledProcessError,subprocess.TimeoutExpired,PhoneUnavailableError) as error:
                if attempt==2:raise
                # Resume from durable state rather than replaying the failed
                # command. In particular, an uncertain mv is resolved by the
                # live target hash before any upload/replacement is repeated.
                journal=read_json(JOURNAL,default={'transactions':[]})
                if resume_index is None:
                    resume_index=next((i for i in range(len(journal['transactions'])-1,-1,-1)
                                       if journal['transactions'][i]['phone_path']==record['phone_path']
                                       and journal['transactions'][i].get('status')!='complete_temporary_copies_removed'),None)
                detail=getattr(error,'stderr',b'') or b''
                if isinstance(detail,bytes):detail=detail.decode('utf-8',errors='replace')
                print(json.dumps({'phone_transaction_recovery_attempt':attempt+1,'phone_path':record['phone_path'],'error':str(error),'stderr':str(detail)[-1000:]}),flush=True)
                time.sleep(attempt+1)


def _apply_locked(record, remove_temporary=True, resume_index=None):
    journal = read_json(JOURNAL,default={'authorization':'User requested batch compression to 1080p, validation, replacement and deletion of temporary backups after verification. Public saved Instagram videos also authorized.','transactions':[]})
    transaction=journal['transactions'][resume_index] if resume_index is not None else None
    if transaction is not None:
        record=dict(transaction)
    transport = phone_transport(ADB)
    def shell(command, timeout=180,read_only=False):
        nonlocal transport
        if read_only:
            output,transport=read_phone_shell(ADB,transport,command,timeout=timeout)
            return output
        return subprocess.run([str(ADB),'-s',transport,'shell',command],capture_output=True,check=True,timeout=timeout).stdout.decode('utf-8').strip()
    def stat(path):
        return tuple(map(int,shell('stat -c %s,%Y '+shlex.quote(path),read_only=True).split(',')))
    def phone_sha(path):
        return shell('sha256sum '+shlex.quote(path),read_only=True).split()[0]
    def media_row():
        aliases={record['phone_path'],record.get('media_store_path',record['phone_path'])}
        where = shlex.quote(' OR '.join("_data='"+p.replace("'","''")+"'" for p in sorted(aliases)))
        return shell('content query --uri content://media/external/video/media --projection _id:_size:date_modified:datetaken:width:height --where '+where,read_only=True)
    def media_fields(row):
        return {key:int(value) for key,value in re.findall(r'(\b_id|_size|date_modified|datetaken|width|height)=(-?\d+)',row)}
    original = Path(record['backup_path'])
    compressed = Path(record['compressed_path'])
    path = record['phone_path']
    assert public_video_path(path)
    live_hash=phone_sha(path)
    already_replaced=resume_index is not None and live_hash==record['compressed_sha256']
    if original.exists(): assert sha(original)==record['original_sha256']
    else: assert already_replaced and transaction.get('phone_hash_verified')
    if compressed.exists(): assert sha(compressed)==record['compressed_sha256']
    else: assert already_replaced and transaction.get('phone_hash_verified')
    before_stat=tuple(transaction['phone_stat_before']) if transaction else stat(path)
    if already_replaced:
        assert stat(path)==(record['compressed_bytes'],record['mtime'])
    else:
        assert stat(path)==before_stat==(record['original_bytes'],record['mtime'])
        assert live_hash==record['original_sha256']
    free_before = transaction.get('free_before_bytes') if already_replaced else int(shell('df -k /data',read_only=True).splitlines()[1].split()[3])*1024
    assert already_replaced or free_before > record['compressed_bytes']+1024**3
    stage = str(Path(path).parent).replace('\\','/')+'/.codex-compress-'+Path(path).name
    # A leftover stage is preserved for inspection rather than silently replaced.
    if transaction:
        assert transaction['phone_stage_path']==stage
        transaction['recovery_started_utc']=datetime.now(timezone.utc).isoformat()
    else:
        assert shell('if [ -e '+shlex.quote(stage)+' ]; then echo exists; fi',read_only=True) != 'exists'
        transaction = {**record, 'status':'staging','started_utc':datetime.now(timezone.utc).isoformat(),'phone_stage_path':stage,'phone_stat_before':before_stat,'media_before':media_row(),'free_before_bytes':free_before,'original_backup_deleted':False,'compressed_pc_copy_deleted':False}
        journal['transactions'].append(transaction)
    def save():
        temporary=JOURNAL.with_suffix('.transaction-new.json')
        with temporary.open('w',encoding='utf-8') as stream:
            stream.write(json.dumps(journal,indent=2));stream.flush();os.fsync(stream.fileno())
        with journal_io(JOURNAL,timeout=15):
            for _ in range(50):
                try:
                    temporary.replace(JOURNAL)
                    return
                except PermissionError: time.sleep(.1)
            temporary.replace(JOURNAL)
    save()
    if not already_replaced:
        print(json.dumps({'staging':path,'bytes':record['compressed_bytes']}),flush=True)
        subprocess.run([str(ADB),'-s',transport,'push',str(compressed),stage],capture_output=True,check=True,timeout=900)
        assert phone_sha(stage) == record['compressed_sha256']
        timestamp=datetime.fromtimestamp(record['mtime'],timezone.utc).strftime('%Y%m%d%H%M.%S')
        shell('TZ=UTC touch -m -t '+timestamp+' '+shlex.quote(stage))
        assert stat(stage)==(record['compressed_bytes'],record['mtime'])
        assert stat(path)==before_stat and phone_sha(path)==record['original_sha256']
        transaction['status']='stage_verified_replacing';save()
        shell('mv -f -- '+shlex.quote(stage)+' '+shlex.quote(path))
        transaction['status']='replaced_verifying';save()
    assert phone_sha(path)==record['compressed_sha256']
    assert stat(path)==(record['compressed_bytes'],record['mtime'])
    transaction.update(phone_hash_verified=True,saved_bytes=record['original_bytes']-record['compressed_bytes'],media_index_updated=False,status='phone_file_verified_index_pending')
    save()
    def index_matches(row):
        fields=media_fields(row)
        before_fields=media_fields(transaction['media_before'])
        expected_dimensions=record.get('output_resolution',[1920,1080])
        return fields.get('_size')==record['compressed_bytes'] and [fields.get('width'),fields.get('height')]==expected_dimensions and fields.get('date_modified')==record['mtime'] and fields.get('datetaken')==before_fields.get('datetaken') and fields.get('_id')==before_fields.get('_id')
    row=media_row()
    for poll in range(20):
        if index_matches(row):
            break
        if poll in (0,7,14):
            try:
                # Re-requesting an index scan is idempotent; it never replaces
                # media. Its return code is not proof of index correctness.
                transaction['scan_broadcast']=shell('am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d '+shlex.quote('file://'+path),timeout=30)
            except (subprocess.CalledProcessError,subprocess.TimeoutExpired) as error:
                detail=getattr(error,'stderr',b'') or b''
                if isinstance(detail,bytes):detail=detail.decode('utf-8',errors='replace')
                transaction.setdefault('scan_broadcast_errors',[]).append({'message':str(error),'stderr':str(detail)[-1000:]})
                save()
                try:
                    transport=phone_transport(ADB)
                except (PhoneUnavailableError,subprocess.SubprocessError):
                    pass
        time.sleep(2)
        row=media_row()
    index_ok=index_matches(row)
    transaction['media_after']=row
    transaction['media_index_updated']=index_ok
    transaction['phone_hash_verified']=True
    transaction['saved_bytes']=record['original_bytes']-record['compressed_bytes']
    transaction['status']='verified_phone_replacement'
    # Persist the critical verification BEFORE optional free-space telemetry.
    save()
    try:
        transaction['free_after_bytes']=int(shell('df -k /data',read_only=True).splitlines()[1].split()[3])*1024
        transaction.pop('free_after_measurement_error',None)
    except Exception as error:
        transaction['free_after_bytes']=None
        detail=getattr(error,'stderr',b'') or b''
        if isinstance(detail,bytes):detail=detail.decode('utf-8',errors='replace')
        transaction['free_after_measurement_error']={'message':str(error),'stderr':str(detail)[-1000:]}
    save()
    # Keep the backup if indexing is unresolved. Byte-identical phone upload plus
    # full local decode and quality validation establishes the replacement data.
    if remove_temporary and transaction['media_index_updated']:
        assert phone_sha(path)==record['compressed_sha256']
        allowed_originals=[ROOT/'outputs/phone-camera-compression-batch/originals',OUT/'temporary']
        assert any(original.resolve().is_relative_to(folder.resolve()) for folder in allowed_originals)
        assert compressed.resolve().is_relative_to((ROOT/'outputs').resolve())
        if original.exists():
            assert sha(original)==record['original_sha256']
            original.unlink()
        transaction['original_backup_deleted']=True
        save()
        if compressed.exists():
            assert sha(compressed)==record['compressed_sha256']
            compressed.unlink()
        transaction['compressed_pc_copy_deleted']=True
        transaction['status']='complete_temporary_copies_removed'
        save()
    print(json.dumps({'replaced':path,'saved_bytes':transaction['saved_bytes'],'media_index_updated':transaction['media_index_updated'],'temporary_backup_deleted':transaction['original_backup_deleted']}),flush=True)
    return transaction

def recover_pending():
    if not JOURNAL.exists(): return
    journal=read_json(JOURNAL)
    for index,transaction in enumerate(journal['transactions']):
        if transaction.get('status')=='complete_temporary_copies_removed': continue
        recovered=apply(transaction,resume_index=index)
        if not recovered['media_index_updated']:
            raise RuntimeError('Recovered video is intact but gallery metadata is unresolved; backup retained.')

if __name__=='__main__':
    import sys
    apply(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')))
