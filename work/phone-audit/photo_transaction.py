"""Durable one-photo transaction. Fresh phone verification precedes every cleanup."""
from pathlib import Path,PurePosixPath
from datetime import datetime,timezone
import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from contextlib import nullcontext
from device import phone_transport,read_phone_shell
from parallel_resource_locks import file_mutex
from photo_batch_plan import public_photo_path
from shared_json import read_json,journal_io


def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def persist(path,data):
    path=Path(path);temporary=path.with_suffix('.photo-new.json')
    gate=journal_io(path,timeout=15) if path.name=='transactions.json' else file_mutex(path.with_suffix('.write.lock'),timeout=15)
    with gate:
        with temporary.open('w',encoding='utf-8') as stream:
            json.dump(data,stream,indent=2);stream.flush();os.fsync(stream.fileno())
        for attempt in range(60):
            try:temporary.replace(path);return
            except PermissionError:
                if attempt==59:raise
                time.sleep(.05)


def fields(row):
    return {k:int(v) for k,v in re.findall(r'(\b_id|_size|date_modified|datetaken|date_added|width|height|orientation)=(-?\d+)',row)}


class PhonePhoto:
    def __init__(self,adb,metrics=None):self.adb=Path(adb);self.metrics=metrics;self.transport=phone_transport(self.adb)
    def shell(self,command,read_only=True,timeout=90):
        category=('gallery_snapshot' if 'content query' in command else 'file_snapshot') if '__PHOTO_FILE__' in command else 'gallery' if command.startswith('content ') else 'scan' if command.startswith('am broadcast') else 'read' if read_only else 'mutation'
        with self.metrics.operation(category) if self.metrics else nullcontext():
            if read_only:
                value,self.transport=read_phone_shell(self.adb,self.transport,command,timeout=timeout);return value
            return subprocess.run([str(self.adb),'-s',self.transport,'shell',command],capture_output=True,check=True,timeout=timeout).stdout.decode().strip()
    def inspect(self,path,need_media=True):
        quoted=shlex.quote(path)
        command='echo __PHOTO_FILE__; if [ -e '+quoted+' ]; then stat -c %s,%Y '+quoted+' && sha256sum '+quoted+'; else echo missing; fi; echo __PHOTO_MEDIA__'
        if need_media:
            where=shlex.quote("_data='"+path.replace("'","''")+"'")
            command+='; content query --uri content://media/external/images/media --projection _id:_size:date_modified:datetaken:date_added:width:height:orientation:mime_type --where '+where
        raw=self.shell(command)
        file_part,media=raw.split('__PHOTO_MEDIA__',1)
        lines=file_part.split('__PHOTO_FILE__',1)[1].strip().splitlines()
        if lines==['missing']:return {'exists':False,'media':media.strip()}
        if len(lines)<2 or not re.fullmatch(r'[0-9a-f]{64}',lines[1].split()[0]):raise ValueError('Invalid photo inspection response')
        return {'exists':True,'stat':tuple(map(int,lines[0].split(','))),'sha':lines[1].split()[0],'media':media.strip()}
    def exists(self,path):return self.shell('if [ -e '+shlex.quote(path)+' ]; then echo exists; fi')=='exists'
    def stat(self,path):return tuple(map(int,self.shell('stat -c %s,%Y '+shlex.quote(path)).split(',')))
    def sha(self,path):return self.shell('sha256sum '+shlex.quote(path)).split()[0]
    def media(self,path):
        where=shlex.quote("_data='"+path.replace("'","''")+"'")
        return self.shell('content query --uri content://media/external/images/media --projection _id:_size:date_modified:datetaken:date_added:width:height:orientation:mime_type --where '+where)
    def scan(self,path):
        try:self.shell('am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d '+shlex.quote('file://'+path),False,30)
        except (subprocess.CalledProcessError,subprocess.TimeoutExpired):pass
    def push(self,source,target):
        with self.metrics.operation('upload') if self.metrics else nullcontext():
            subprocess.run([str(self.adb),'-s',self.transport,'push',str(source),target],capture_output=True,check=True,timeout=300)
    def move(self,source,target):self.shell('mv -n -- '+shlex.quote(source)+' '+shlex.quote(target),False)
    def remove(self,path):self.shell('rm -- '+shlex.quote(path),False)
    def touch(self,path,mtime):
        stamp=datetime.fromtimestamp(mtime,timezone.utc).strftime('%Y%m%d%H%M.%S')
        self.shell('TZ=UTC touch -m -t '+stamp+' '+shlex.quote(path),False)
    def free(self):return int(self.shell('df -k /data').splitlines()[1].split()[3])*1024
    def delete_stale_row(self,path,identifier):
        # The physical source has already been renamed to our verified hidden
        # backup. Never apply MediaStore deletion while that source exists.
        if self.exists(path):raise ValueError('Source reappeared before index cleanup')
        row=fields(self.media(path))
        if row and row.get('_id')!=identifier:raise ValueError('Source gallery ID changed')
        if row:
            self.shell('content delete --uri content://media/external/images/media/'+str(identifier),False)
            if fields(self.media(path)):raise ValueError('Old gallery entry cleanup unresolved')
        return True  # Absence is confirmed; the caller need not query again.


def inspect_phone(phone,path,need_media=True):
    if hasattr(phone,'inspect'):return phone.inspect(path,need_media)
    if not phone.exists(path):return {'exists':False,'media':phone.media(path) if need_media else ''}
    return {'exists':True,'stat':phone.stat(path),'sha':phone.sha(path),'media':phone.media(path) if need_media else ''}


def verify_target_file(phone,row,snapshot=None):
    if snapshot is None:snapshot=inspect_phone(phone,row['target_phone_path'],need_media=False)
    if snapshot.get('sha')!=row['compressed_sha256'] or snapshot.get('stat')!=(row['compressed_bytes'],row['mtime']):
        raise ValueError('Photo target hash or file date mismatch')
    return snapshot


def verify_target(phone,row,attempts=20):
    path=row['target_phone_path']
    snapshot=verify_target_file(phone,row,inspect_phone(phone,path))
    expected=fields(row['source_media_row'])
    for attempt in range(attempts):
        raw=snapshot['media'] if attempt==0 else phone.media(path);current=fields(raw)
        if (current.get('_size')==row['compressed_bytes'] and current.get('date_modified')==row['mtime']
                and current.get('datetaken')==expected.get('datetaken')
                and ('orientation' not in expected or current.get('orientation')==expected['orientation'])
                and [current.get('width'),current.get('height')]==row['output_resolution']
                and 'mime_type='+row['output_mime'] in raw):return current
        if attempt in (0,7,14):phone.scan(path)
        time.sleep(.5)
    raise ValueError('Photo gallery size/date/dimensions validation failed; backups retained')


def apply_photo(root,row,phone=None,metrics=None):
    root=Path(root);out=root/'outputs/phone-photo-compression-batches';journal_path=out/'transactions.json'
    source=row['phone_path'];target=row['target_phone_path']
    if not public_photo_path(source):raise ValueError('Unauthorized source photo path')
    if target not in (source,str(PurePosixPath(source).with_suffix('.webp'))):raise ValueError('Unauthorized photo target')
    token=hashlib.sha256(source.encode()).hexdigest()[:16]
    parent=str(PurePosixPath(source).parent)
    stage=parent+'/.codex-photo-stage-'+token
    hidden=parent+'/.codex-photo-original-'+token+PurePosixPath(source).suffix
    # Measure serialized-phone waiting separately from the actual transaction.
    with (metrics.stage('phone_lock_wait') if metrics else nullcontext()):
        gate=file_mutex(root/'outputs/phone-video-compression-batches/phone-transaction.lock')
        gate.__enter__()
    try:
      with metrics.stage('phone_transaction') if metrics else nullcontext():
        phone=phone or PhonePhoto(root/'work/android-tools/platform-tools/adb.exe')
        journal=read_json(journal_path,default={'records':{}})
        previous=journal['records'].get(source)
        fresh=previous is None
        pilot_installed=False
        if previous:
            if (previous['phone_path']!=source or previous['target_phone_path']!=target
                    or previous['stage']!=stage or previous['hidden_original']!=hidden):
                raise ValueError('Photo recovery manifest paths changed')
            row=previous
        else:
            original=inspect_phone(phone,source,need_media=False)
            if original.get('stat')!=(row['original_bytes'],row['mtime']) or original.get('sha')!=row['original_sha256']:
                raise ValueError('Original changed before photo transaction')
            if phone.exists(stage) or phone.exists(hidden):raise ValueError('Unknown photo staging file; refusing overwrite')
            if target!=source and phone.exists(target):
                if not row.get('approved_pilot_copy') or phone.sha(target)!=row['compressed_sha256']:
                    raise ValueError('Existing photo destination preserved')
                pilot_installed=True
            row={**row,'stage':stage,'hidden_original':hidden,'status':'uploading','phone_original_deleted':False}
            journal['records'][source]=row;persist(journal_path,journal)
        # Resume uses the exact durable manifest; local copies may disappear only
        # after the final phone verification was persisted.
        if row['status']=='complete':return row
        row['transaction_policy_version']=3
        backup=Path(row['backup_path']);compressed=Path(row['compressed_path'])
        installed=pilot_installed if fresh else phone.exists(target) and phone.sha(target)==row['compressed_sha256']
        if (row['status']=='ready_to_remove_hidden_original' and not phone.exists(hidden) and installed
                and (target==source or not phone.exists(source)) and backup.exists() and sha(backup)==row['original_sha256']):
            verify_target(phone,row)
            row.update(phone_original_deleted=True,saved_bytes=row['original_bytes']-row['compressed_bytes'],status='phone_verified_cleanup_pending')
            persist(journal_path,journal)
        for path,wanted in ((backup,row['original_sha256']),(compressed,row['compressed_sha256'])):
            if not path.exists():
                if row['phone_original_deleted'] and installed:continue
                raise ValueError('Verified local photo copy missing')
            if sha(path)!=wanted:raise ValueError('Verified local photo copy changed')
        def save(status):row['status']=status;persist(journal_path,journal)
        hidden_present=False if fresh else phone.exists(hidden)
        hidden_verified=False
        if hidden_present:
            if phone.sha(hidden)!=row['original_sha256']:raise ValueError('Hidden original hash changed')
            hidden_verified=True
        elif not row['phone_original_deleted'] and not fresh:
            if phone.stat(source)!=(row['original_bytes'],row['mtime']) or phone.sha(source)!=row['original_sha256']:
                raise ValueError('Original changed during photo recovery')
        if not installed:
            if not fresh and target!=source and phone.exists(target):raise ValueError('Unexpected photo target during recovery')
            if phone.free()<row['compressed_bytes']+1024**3:raise RuntimeError('Phone free space insufficient')
            stage_present=False if fresh else phone.exists(stage)
            if stage_present and phone.sha(stage)!=row['compressed_sha256']:
                # This path was absent before our durable uploading record.
                phone.remove(stage)
                stage_present=False
            if not stage_present:phone.push(compressed,stage)
            if phone.sha(stage)!=row['compressed_sha256']:raise ValueError('Photo staging hash mismatch')
            phone.touch(stage,row['mtime'])
            if phone.stat(stage)!=(row['compressed_bytes'],row['mtime']):raise ValueError('Photo staging stat mismatch')
            save('staged')
            if target==source and not hidden_present:
                save('hiding_original');phone.move(source,hidden)
                if phone.exists(source) or phone.sha(hidden)!=row['original_sha256']:raise ValueError('Original photo rename unresolved')
                hidden_present=True;hidden_verified=True
            phone.move(stage,target)
            save('target_installed')
            # Request indexing immediately: avoid first querying a new path
            # that is known not to have been scanned yet.
            phone.scan(target)
        after=verify_target(phone,row)
        row['media_after']=after;save('target_verified')
        if target!=source and not hidden_present and not row['phone_original_deleted']:
            if phone.sha(source)!=row['original_sha256']:raise ValueError('Original changed before final rename')
            save('hiding_original');phone.move(source,hidden)
            if phone.exists(source) or phone.sha(hidden)!=row['original_sha256']:raise ValueError('Original photo rename unresolved')
            hidden_present=True;hidden_verified=True
        if target!=source:
            old_id=fields(row['source_media_row']).get('_id')
            if not old_id or old_id==after.get('_id'):raise ValueError('Gallery identity conflict')
            confirmed=phone.delete_stale_row(source,old_id)
            if confirmed is not True and fields(phone.media(source)):raise ValueError('Old gallery entry cleanup unresolved')
        if not row['phone_original_deleted']:
            if (not hidden_verified and phone.sha(hidden)!=row['original_sha256']) or sha(backup)!=row['original_sha256']:
                raise ValueError('Original backup changed before cleanup')
            save('ready_to_remove_hidden_original')
            phone.remove(hidden)
            if phone.exists(hidden):raise ValueError('Hidden original removal unresolved')
            row['phone_original_deleted']=True
            row['saved_bytes']=row['original_bytes']-row['compressed_bytes']
            save('phone_verified_cleanup_pending')
        # No subsequent operation changes target pixels, its date or its
        # distinct gallery ID. Recheck transferred bytes, without repeating
        # the full MediaStore validation already completed above.
        verify_target_file(phone,row)
        row['final_file_verified']=True
        # Delete only this worker's exact temporary files. Pilot evidence stays.
        for path,wanted in ((backup,row['original_sha256']),(compressed,row['compressed_sha256'])):
            if path.resolve().is_relative_to((out/'temporary').resolve()) and path.exists():
                if sha(path)!=wanted:raise ValueError('Temporary photo copy changed')
                path.unlink()
        save('complete')
        return row
    finally:gate.__exit__(None,None,None)
