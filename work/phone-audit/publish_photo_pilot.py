"""Approval-gated pilot publication. Adds up to four copies; NEVER removes originals."""
from pathlib import Path
from datetime import datetime,timezone
import hashlib
import json
import re
import shlex
import subprocess
import time
from photo_batch_plan import require_pilot_approval
from parallel_resource_locks import file_mutex
from device import phone_transport,read_phone_shell

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'outputs/phone-photo-compression-batches'
ADB=ROOT/'work/android-tools/platform-tools/adb.exe'


def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def save(path,data):
    temporary=path.with_suffix('.publishing-new.json')
    with temporary.open('w',encoding='utf-8') as stream:
        json.dump(data,stream,indent=2);stream.flush()
        import os
        os.fsync(stream.fileno())
    temporary.replace(path)


def fields(row):
    return {k:int(v) for k,v in re.findall(r'(\b_id|_size|date_modified|datetaken|date_added|width|height|orientation)=(-?\d+)',row)}


def main():
    payload=(OUT/'pilot-manifest.json').read_bytes()
    manifest=json.loads(payload)
    authorization=json.loads((OUT/'pilot-authorization.json').read_text(encoding='utf-8'))
    digest=require_pilot_approval(authorization,payload,manifest['records'])
    status=json.loads((OUT/'status.json').read_text(encoding='utf-8'))
    status.update(state='publishing_pilot',error=None,published_pilot=0)
    save(OUT/'status.json',status)
    # Approval is verified before any phone discovery, ADB command or publication.
    journal_path=OUT/'pilot-publication.json'
    journal=json.loads(journal_path.read_text(encoding='utf-8')) if journal_path.exists() else {'manifest_sha256':digest,'records':{}}
    if journal['manifest_sha256']!=digest:raise RuntimeError('Pilot publication belongs to another manifest')
    transport=phone_transport(ADB)
    def shell(command,read_only=False,timeout=60):
        nonlocal transport
        if read_only:
            result,transport=read_phone_shell(ADB,transport,command,timeout=timeout);return result
        return subprocess.run([str(ADB),'-s',transport,'shell',command],capture_output=True,check=True,timeout=timeout).stdout.decode().strip()
    def exists(path):return shell('if [ -e '+shlex.quote(path)+' ]; then echo exists; fi',True)=='exists'
    def remote_sha(path):return shell('sha256sum '+shlex.quote(path),True).split()[0]
    def stat(path):return tuple(map(int,shell('stat -c %s,%Y '+shlex.quote(path),True).split(',')))
    def media(path):
        where=shlex.quote("_data='"+path.replace("'","''")+"'")
        return shell('content query --uri content://media/external/images/media --projection _id:_size:date_modified:datetaken:date_added:width:height:orientation:mime_type --where '+where,True)
    with file_mutex(OUT/'pilot-publisher.lock',timeout=1):
        for row in manifest['records']:
            status['current_file']=Path(row['phone_path']).name;save(OUT/'status.json',status)
            with file_mutex(ROOT/'outputs/phone-video-compression-batches/phone-transaction.lock'):
                source=row['phone_path'];target=row['target_phone_path']
                backup=Path(row['backup_path']).resolve();compressed=Path(row['compressed_path']).resolve()
                if not backup.is_relative_to((ROOT/'outputs/phone-photo-analysis/samples').resolve()):raise ValueError('Unexpected backup location')
                if not compressed.is_relative_to((OUT/'pilot').resolve()):raise ValueError('Unexpected output location')
                if sha(backup)!=row['original_sha256'] or sha(compressed)!=row['compressed_sha256']:raise ValueError('Prepared copies changed')
                if stat(source)!=(row['original_bytes'],row['mtime']) or remote_sha(source)!=row['original_sha256']:raise ValueError('Phone original changed')
                current_source=fields(media(source));expected_source=fields(row['source_media_row'])
                if not current_source.get('_id') or current_source!=expected_source:raise ValueError('Original gallery entry changed or missing')
                previous=journal['records'].get(source)
                if exists(target) and previous is None:raise ValueError('Pilot target collision; existing file preserved')
                token=hashlib.sha256(source.encode()).hexdigest()[:12]
                stage=str(Path(target).parent).replace('\\','/')+'/.codex-photo-pilot-'+token+'.webp'
                if previous is None:
                    if exists(stage):raise ValueError('Unexpected staged file; refusing overwrite')
                    previous={'source':source,'target':target,'stage':stage,'status':'upload_started','original_untouched':True}
                    journal['records'][source]=previous;save(journal_path,journal)
                refresh=exists(target) and remote_sha(target)!=row['compressed_sha256']
                if refresh and (previous.get('approved_refresh_compressed_sha256')!=row['compressed_sha256']
                                or remote_sha(target)!=previous.get('previous_compressed_sha256')):
                    raise ValueError('Existing pilot changed; refusing refresh')
                if not exists(target) or refresh:
                    if not exists(stage):
                        free=int(shell('df -k /data',True).splitlines()[1].split()[3])*1024
                        if free<row['compressed_bytes']+1024**3:raise RuntimeError('Insufficient phone space for pilot')
                        subprocess.run([str(ADB),'-s',transport,'push',str(compressed),stage],capture_output=True,check=True,timeout=180)
                    if remote_sha(stage)!=row['compressed_sha256']:raise ValueError('Staged pilot copy hash mismatch; preserved for inspection')
                    timestamp=datetime.fromtimestamp(row['mtime'],timezone.utc).strftime('%Y%m%d%H%M.%S')
                    shell('TZ=UTC touch -m -t '+timestamp+' '+shlex.quote(stage))
                    if stat(stage)!=(row['compressed_bytes'],row['mtime']):raise ValueError('Staged date mismatch')
                    previous['status']='stage_verified';save(journal_path,journal)
                    # -f is restricted to our own previously hashed pilot copy,
                    # when the approved manifest explicitly requests its refresh.
                    shell(('mv -f -- ' if refresh else 'mv -n -- ')+shlex.quote(stage)+' '+shlex.quote(target))
                if remote_sha(target)!=row['compressed_sha256'] or stat(target)!=(row['compressed_bytes'],row['mtime']):raise ValueError('Published pilot hash/date mismatch')
                previous['status']='phone_copy_verified_index_pending';save(journal_path,journal)
                after={}
                for poll in range(20):
                    after=fields(media(target))
                    if after.get('_id') and after.get('_size')==row['compressed_bytes']:break
                    if poll in (0,7,14):shell('am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d '+shlex.quote('file://'+target),timeout=30)
                    time.sleep(1)
                if not after.get('_id'):raise ValueError('Pilot gallery indexing unresolved; originals and backups preserved')
                binds=[]
                # A pilot is an additional item with a new media ID. DATE_ADDED
                # is provider-owned; it is not the file/recording date.
                for key in ('datetaken',):
                    if key in current_source and after.get(key)!=current_source[key]:binds+=['--bind',f'{key}:l:{current_source[key]}']
                if binds:
                    shell('content update --uri content://media/external/images/media/'+str(after['_id'])+' '+shlex.join(binds))
                after_row=media(target);after=fields(after_row)
                if (after.get('_size')!=row['compressed_bytes'] or after.get('date_modified')!=row['mtime']
                        or after.get('datetaken')!=current_source.get('datetaken')
                        or 'mime_type=image/webp' not in after_row):raise ValueError('Pilot gallery metadata mismatch; no originals removed')
                if remote_sha(source)!=row['original_sha256']:raise ValueError('Original changed during pilot')
                previous.update(status='pilot_copy_verified_awaiting_user_gallery_review',media_before=current_source,media_after=after,
                                new_media_id=after.get('_id')!=current_source.get('_id'),
                                date_added_differs_for_new_copy=after.get('date_added')!=current_source.get('date_added'),
                                gallery_chronology_requires_user_review=True)
                save(journal_path,journal)
                status['published_pilot']=sum(r['status']=='pilot_copy_verified_awaiting_user_gallery_review' for r in journal['records'].values())
                save(OUT/'status.json',status)
        status=json.loads((OUT/'status.json').read_text(encoding='utf-8'))
        status.update(state='awaiting_gallery_review',published_pilot=len(journal['records']),phone_replacements=0,saved_bytes=0,current_file=None,
                      records=[{**row,'publication_status':journal['records'][row['phone_path']]['status']} for row in manifest['records']],
                      manifest_sha256=digest,phase='Four pilot copies on phone; original photos retained; awaiting chronology review')
        save(OUT/'status.json',status)


if __name__=='__main__':
    try:main()
    except PermissionError:raise
    except Exception as error:
        path=OUT/'status.json'
        if path.exists():
            status=json.loads(path.read_text(encoding='utf-8'))
            status.update(state='attention_required',error=str(error))
            save(path,status)
        raise
