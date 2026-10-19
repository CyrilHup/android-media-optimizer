"""Approved, resumable one-photo batches; no image previews or thumbnails."""
from pathlib import Path,PurePosixPath
from datetime import datetime,timezone
from collections import Counter
import hashlib
import json
import os
import shutil
import subprocess
import time
import traceback
import threading
from contextlib import nullcontext
from PIL import Image,ImageChops
from analyze_phone_photos import limit_cpu,compare,CWEBP
from webp_metadata import metadata,inject_metadata
from hdr_metadata_policy import hdr_xmp_issues
from photo_transaction import PhonePhoto,apply_photo,persist,sha,fields,inspect_phone
from photo_timing import PhotoTiming
from jpeg_metadata import encode_preserving_jpeg
from photo_batch_plan import public_photo_path
from parallel_resource_locks import file_mutex,DiskReservations
from shared_json import read_json
from device import PhoneUnavailableError

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'outputs/phone-photo-compression-batches'
VIDEO=ROOT/'outputs/phone-video-compression-batches'
ADB=ROOT/'work/android-tools/platform-tools/adb.exe'
HDR=ROOT/'work/hdr-tools/build/uhdr_roundtrip.exe'
JPEGTRAN=Path('C:/msys64/mingw64/bin/jpegtran.exe')
state={};receipts={};queue=[];timing=None
state_guard=threading.RLock()
heartbeat_stop=threading.Event()
POLICY_VERSION=2
ADOBE_ISSUES={'unmapped_metadata:'+k for k in ('photoshop','adobe','adobe_transform','progressive','progression','comment')}


def measure(stage):return timing.stage(stage) if timing else nullcontext()


def measured(stage,function,*args,**kwargs):
    with measure(stage):return function(*args,**kwargs)


class KeepOriginal(Exception):pass
class Paused(Exception):pass


def command():return read_json(OUT/'control.json',default={'command':'pause'}).get('command','pause')


def update(phase=None,**changes):
    with state_guard:
        if phase is not None:changes['phase']=phase
        state.update(changes,updated_utc=datetime.now(timezone.utc).isoformat())
        if timing:state['current_timing']=timing.snapshot()
        persist(OUT/'status.json',state)


def heartbeat():
    while not heartbeat_stop.wait(2):
        if timing and state:
            try:update()
            except Exception:traceback.print_exc();return


def totals():
    values=list(receipts.values())
    state.update(completed=len(values),processed_original_bytes=sum(r['original_bytes'] for r in values),
                 phone_replacements=sum(r['status']=='complete' for r in values),
                 saved_bytes=sum(r.get('saved_bytes',0) for r in values),
                 skip_reasons=dict(Counter(r.get('reason','') for r in values if r['status']!='complete')))


def remember(row):
    row['policy_version']=POLICY_VERSION
    if timing:
        row['processing_timing']=timing.snapshot()
        state['last_timing']=row['processing_timing']
    receipts[row['phone_path']]=row
    persist(OUT/'receipts.json',{'records':receipts})
    totals();update()


def run(args,timeout=240):
    # Inherited two-core affinity bounds both codec and numerical verification.
    stage=('backup_download' if Path(args[0]).resolve()==ADB.resolve() else 'quality_validation' if len(args)>1 and args[1]=='compare' else 'hdr_processing' if Path(args[0])==HDR else 'encoding')
    try:
      with measure(stage):
        child=subprocess.Popen([str(a) for a in args],stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
        started=time.monotonic()
        try:
            while True:
                try:
                    stdout,stderr=child.communicate(timeout=min(2,max(.1,timeout-(time.monotonic()-started))))
                    if child.returncode:raise subprocess.CalledProcessError(child.returncode,args,stdout,stderr)
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic()-started>=timeout:raise
                    extra={}
                    if stage=='backup_download' and Path(args[-1]).exists():
                        received=Path(args[-1]).stat().st_size
                        extra={'transfer_received_bytes':received,'transfer_expected_bytes':state.get('current_original_bytes',0)}
                    update(**extra)
        finally:
            if child.poll() is None:child.kill();child.communicate()
    except subprocess.CalledProcessError as error:
        if Path(args[0]).resolve()!=ADB.resolve():raise KeepOriginal('encoder_rejected_source:'+Path(args[0]).name) from error
        raise
    return stdout.decode(errors='replace').strip()


def require_approval():
    raw=(OUT/'queue.json').read_bytes();approval=read_json(OUT/'bulk-authorization.json',default={})
    records=json.loads(raw)['records']
    if (approval.get('approved') is not True or approval.get('scope')!='replace_eligible_public_photos'
            or approval.get('queue_sha256')!=hashlib.sha256(raw).hexdigest()):
        raise PermissionError('This exact bulk photo queue is not approved')
    if len({r['path'] for r in records})!=len(records):raise ValueError('Duplicate photo source')
    if any(not public_photo_path(r['path']) for r in records):raise ValueError('Photo outside approved public scope')
    if any(r['target_phone_path']!=str(PurePosixPath(r['path']).with_suffix('.webp')) for r in records):
        raise ValueError('Photo destination outside approved scope')
    return records


def prepare(item,phone,pilot):
    source=item['path'];token=hashlib.sha256(source.encode()).hexdigest()[:16]
    folder=OUT/'temporary'/token;folder.mkdir(parents=True,exist_ok=True)
    backup=folder/('source'+PurePosixPath(source).suffix.lower());raw=folder/'raw.webp';target=folder/'candidate.webp'
    cached=read_json(folder/'prepared.json',default={})
    if cached:
        with measure('candidate_reuse'):
            candidate=Path(cached['compressed_path'])
            if (cached.get('preparation_policy_version')!=POLICY_VERSION or cached['phone_path']!=source
                    or cached['original_bytes']!=item['bytes'] or cached['mtime']!=item['mtime']
                    or Path(cached['backup_path']).resolve()!=backup.resolve()
                    or candidate.resolve().parent!=folder.resolve() or candidate.name not in ('candidate.webp','candidate.jpg')):
                raise ValueError('Prepared photo checkpoint does not match this file')
            if sha(backup)!=cached['original_sha256'] or sha(candidate)!=cached['compressed_sha256']:
                raise ValueError('Prepared photo checkpoint hash changed')
        update('Copie compressée vérifiée réutilisée',phase_percent=75)
        return cached
    snapshot=measured('phone_preflight',inspect_phone,phone,source)
    if not snapshot['exists']:raise KeepOriginal('original_absent')
    if snapshot['stat']!=(item['bytes'],item['mtime']):raise KeepOriginal('original_changed_since_inventory')
    source_hash=snapshot['sha']
    media=snapshot['media'];original=fields(media)
    if original.get('_size')!=item['bytes'] or not original.get('width') or not original.get('height'):
        raise KeepOriginal('source_gallery_index_incomplete')
    update('Téléchargement de la sauvegarde',phase_percent=10)
    if not backup.exists() or sha(backup)!=source_hash:
        run([ADB,'-s',phone.transport,'pull',source,backup],300)
    with measure('backup_verification'):
        if sha(backup)!=source_hash or phone.sha(source)!=source_hash:raise ValueError('Photo backup hash mismatch')
    update('Sauvegarde vérifiée',transfer_received_bytes=item['bytes'])
    base={'phone_path':source,'target_phone_path':item['target_phone_path'],'mtime':item['mtime'],
          'original_bytes':item['bytes'],'original_sha256':source_hash,'backup_path':str(backup),
          'source_media_row':media,'output_resolution':[original['width'],original['height']]}
    # Adopt only previously published, exact-hash pilot outputs. Other collisions stay untouched.
    if pilot and source_hash==pilot['original_sha256'] and sha(pilot['compressed_path'])==pilot['compressed_sha256']:
        measured('candidate_reuse',shutil.copyfile,pilot['compressed_path'],target)
        base.update(approved_pilot_copy=True,metadata_checks=pilot['metadata_checks'],quality_checks=pilot['quality_checks'],output_mime='image/webp')
    else:
        if phone.exists(item['target_phone_path']):raise KeepOriginal('existing_destination')
        update('Analyse des métadonnées',phase_percent=20)
        meta=measured('metadata_analysis',metadata,backup)
        hdr=any('gain_map' in issue for issue in meta['issues'])
        if hdr:
            # Keep JPEG container, original HDR gain map, EXIF and ICC. No HDR-to-WebP.
            if hdr_xmp_issues(meta['xmp']):raise KeepOriginal('unsupported_hdr_metadata')
            update('JPEG HDR sans perte',phase_percent=35)
            try:
                run([HDR,'extract',backup,folder])
                optimized=folder/'optimized.jpg';target=folder/'candidate.jpg'
                run([JPEGTRAN,'-copy','all','-optimize','-progressive','-outfile',optimized,folder/'base.jpg'])
                with Image.open(folder/'base.jpg') as a,Image.open(optimized) as b:
                    if a.size!=b.size or any(high for low,high in ImageChops.difference(a.convert('RGB'),b.convert('RGB')).getextrema()):
                        raise KeepOriginal('hdr_sdr_pixels_changed')
                run([HDR,'pack',backup,optimized,target])
                check=json.loads(run([HDR,'compare',backup,target]))
            except subprocess.CalledProcessError as error:raise KeepOriginal('unsupported_hdr_structure') from error
            with Image.open(target) as image:legacy=bool(image.info.get('xmp'))
            if not (legacy and all(check[k] for k in ('full_hdr_decode_passed','linear_fp16_pixels_identical','exif_identical','icc_identical','gainmap_colour_space_flag_identical')) and check['max_gainmap_metadata_absolute_difference']<1e-6):
                raise KeepOriginal('hdr_validation_failed')
            base.update(target_phone_path=source,output_mime='image/jpeg',quality_checks=check,metadata_checks={'hdr_lossless_verified':True})
        else:
            png=backup.suffix=='.png'
            if meta['issues'] and not png and set(meta['issues'])<=ADOBE_ISSUES:
                target=folder/'candidate.jpg'
                for level in (90,93):
                    update('JPEG · métadonnées intégrales · qualité '+str(level),phase_percent=35)
                    try:checks=measured('encoding',encode_preserving_jpeg,backup,target,quality=level)
                    except (OSError,ValueError) as error:raise KeepOriginal('jpeg_metadata_fallback_unsupported:'+str(error)) from error
                    quality=measured('quality_validation',compare,backup,target)
                    if quality['psnr_rgb_db'] is None or quality['psnr_rgb_db']>=38:break
                base.update(target_phone_path=source,output_mime='image/jpeg',method='jpeg_all_metadata')
            else:
                if meta['issues']:raise KeepOriginal(';'.join(meta['issues']))
                for level in ((85,) if png else (85,90)):
                    update('Encodage WebP'+(' sans perte' if png else ' · qualité '+str(level)),phase_percent=35)
                    args=[CWEBP,'-quiet','-m','4','-mt','-metadata','none']+(['-lossless','-exact'] if png else ['-q',str(level)])
                    run(args+[backup,'-o',raw])
                    checks=measured('metadata_mux',inject_metadata,backup,raw,target,mtime=item['mtime'],gallery_taken_ms=original.get('datetaken'),source_metadata=meta)
                    update('Vérification complète',phase_percent=65)
                    quality=measured('quality_validation',compare,backup,target)
                    if png or quality['psnr_rgb_db'] is None or quality['psnr_rgb_db']>=38:break
                if checks['unsupported_features']:raise KeepOriginal('unsupported_metadata')
                base.update(output_mime='image/webp',method='webp_lossless' if png else 'webp_q'+str(level))
            if not quality['alpha_identical'] or not quality['orientation_preserved']:raise KeepOriginal('alpha_or_orientation_failed')
            if png and not quality['pixels_identical']:raise KeepOriginal('lossless_pixels_changed')
            if not png and quality['psnr_rgb_db'] is not None and quality['psnr_rgb_db']<38:raise KeepOriginal('numeric_quality_below_38db')
            base.update(metadata_checks=checks,quality_checks=quality)
    if target.stat().st_size>item['bytes']*.9:raise KeepOriginal('gain_below_10_percent')
    base.update(compressed_bytes=target.stat().st_size,compressed_sha256=sha(target),compressed_path=str(target))
    # Dimensions in MediaStore are raw encoded dimensions; orientation is preserved separately.
    with measure('decode_validation'):
      with Image.open(target) as image:
          if list(image.size)!=base['output_resolution']:raise KeepOriginal('gallery_dimensions_not_preservable')
    if sha(backup)!=source_hash:raise ValueError('Photo backup changed after encoding')
    base['preparation_policy_version']=POLICY_VERSION
    persist(folder/'prepared.json',base)
    return base


def cleanup_skipped(item,phone):
    folder=OUT/'temporary'/hashlib.sha256(item['path'].encode()).hexdigest()[:16]
    # No recursive deletion: only explicitly named files belonging to this photo.
    backup=folder/('source'+PurePosixPath(item['path']).suffix.lower())
    if backup.exists() and (not phone.exists(item['path']) or sha(backup)!=phone.sha(item['path'])):
        raise ValueError('Skipped photo original changed; local backup retained')
    for name in ('prepared.json','raw.webp','candidate.webp','candidate.jpg','base.jpg','gainmap.jpg','optimized.jpg',backup.name):
        (folder/name).unlink(missing_ok=True)


def main():
    global state,queue,receipts,timing
    queue=require_approval();limit_cpu(max_cores=2)
    old=read_json(OUT/'status.json',default={})
    state={k:old[k] for k in ('inventory_files','outside_queue_formats','exclusion_reasons','excluded_known') if k in old}
    exclusions=read_json(OUT/'queue.json',default={}).get('excluded',[])
    state.update(state='running',total_candidates=len(queue),total_original_bytes=sum(r['bytes'] for r in queue),
                 excluded_known=len(exclusions),exclusion_reasons=dict(Counter(r['reason'] for r in exclusions)),
                 approval_required=False,cpu_reserved_for_photos=True,worker_pid=os.getpid(),phase_percent=0)
    receipts=read_json(OUT/'receipts.json',default={'records':{}})['records']
    retry={path:row for path,row in receipts.items() if row.get('status')=='kept_original'
           and row.get('policy_version',1)<POLICY_VERSION and (row.get('reason')=='numeric_quality_below_38db'
           or bool(row.get('reason')) and set(row['reason'].split(';'))<=ADOBE_ISSUES)}
    if retry:
        history=read_json(OUT/'previous-policy-receipts.json',default={'records':{}})
        history['records'].update(retry);persist(OUT/'previous-policy-receipts.json',history)
        for path in retry:del receipts[path]
        persist(OUT/'receipts.json',{'records':receipts})
    state['retrying_previous_refusals']=len(retry)
    totals();update('Démarrage')
    threading.Thread(target=heartbeat,daemon=True,name='photo-progress').start()
    pilots={r['phone_path']:r for r in read_json(OUT/'pilot-manifest.json',default={'records':[]})['records']}
    reserve=DiskReservations(VIDEO/'disk-reservations.json',ROOT)
    try:
        for item in queue:
            source=item['path']
            if source in receipts:continue
            if command()!='run':raise Paused()
            started=time.monotonic();token=hashlib.sha256(source.encode()).hexdigest()[:16]
            timing=PhotoTiming()
            folder=OUT/'temporary'/token
            own=[folder/name for name in ('source'+PurePosixPath(source).suffix.lower(),'raw.webp','candidate.webp','candidate.jpg','base.jpg','optimized.jpg','gainmap.jpg')]
            update('En attente d’espace local',current_file=source,current_original_bytes=item['bytes'],state='running',error=None,phase_percent=0,transfer_received_bytes=0,transfer_expected_bytes=item['bytes'])
            with measure('disk_wait'):
                if not reserve.acquire('photos',item['bytes'],own,lambda:command()!='run'):raise Paused()
            try:
                while True:
                    if command()!='run':raise Paused()
                    try:
                        phone=measured('connection',PhonePhoto,ADB,metrics=timing)
                        update(state='running',error=None)
                        pending=read_json(OUT/'transactions.json',default={'records':{}})['records'].get(source)
                        if pending:
                            update('Reprise du remplacement vérifié',state='running',phase_percent=80,error=None)
                            result=apply_photo(ROOT,pending,phone,metrics=timing)
                        else:
                            try:row=prepare(item,phone,pilots.get(source))
                            except (KeepOriginal,Image.DecompressionBombError,subprocess.TimeoutExpired) as error:
                                measured('kept_original_cleanup',cleanup_skipped,item,phone)
                                remember({'phone_path':source,'original_bytes':item['bytes'],'status':'kept_original','reason':str(error),'seconds':round(time.monotonic()-started,2)})
                                break
                            if command()!='run':raise Paused()
                            update('Transfert et index de la galerie',phase_percent=80)
                            result=apply_photo(ROOT,row,phone,metrics=timing)
                        remember({**result,'seconds':round(time.monotonic()-started,2)})
                        for name in ('prepared.json','raw.webp','base.jpg','gainmap.jpg','optimized.jpg'):
                            (folder/name).unlink(missing_ok=True)
                        update('Terminé',phase_percent=100)
                        break
                    except (PhoneUnavailableError,subprocess.CalledProcessError,subprocess.TimeoutExpired) as error:
                        # A mutation is resumed from the durable journal, never blindly replayed.
                        update('Connexion au téléphone en attente',state='waiting_for_phone',error=str(error)[-500:])
                        with measure('wifi_wait'):
                            for _ in range(15):
                                if command()!='run':raise Paused()
                                time.sleep(1)
                                update()
            finally:reserve.release('photos')
        update('Terminé',state='complete',current_file=None,phase_percent=100,error=None)
    except Paused:
        update('Pause au point sûr',state='paused' if command()=='pause' else 'stopped',error=None)


if __name__=='__main__':
    OUT.mkdir(parents=True,exist_ok=True)
    with file_mutex(OUT/'worker.lock',timeout=1):
        try:main()
        except Exception as error:
            update('Arrêt de sécurité · sauvegardes conservées',state='attention_required',error=f'{type(error).__name__}: {error}')
            traceback.print_exc();raise
        finally:heartbeat_stop.set()
