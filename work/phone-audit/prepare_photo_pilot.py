"""Prepare four fresh local WebP outputs and a reviewable plan; never writes to phone."""
from pathlib import Path
from collections import Counter
from datetime import datetime,timezone
import hashlib
import json
import shutil
import subprocess
import shlex
import time
from PIL import Image
from analyze_phone_photos import limit_cpu,sha,compare,CWEBP
from webp_metadata import inject_metadata
from photo_batch_plan import build_photo_plan
from device import phone_transport,read_phone_shell
from parallel_resource_locks import file_mutex

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'outputs/phone-photo-compression-batches'
BASE=ROOT/'outputs/phone-photo-analysis'
ADB=ROOT/'work/android-tools/platform-tools/adb.exe'
state={'state':'preparing','phone_replacements':0,'saved_bytes':0,'completed':0,'prepared_pilot':0,
       'pilot_total':4,'records':[],'approval_required':True,'cpu_reserved_for_photos':False}


def write(name,value):
    path=OUT/name;temporary=path.with_suffix('.new.json')
    temporary.write_text(json.dumps(value,indent=2),encoding='utf-8');temporary.replace(path)


def save():
    state['updated_utc']=datetime.now(timezone.utc).isoformat();write('status.json',state)


def main():
    OUT.mkdir(parents=True,exist_ok=True);limit_cpu()
    inventory=json.loads((ROOT/'work/media-audit-2026-10-14/all-files.json').read_text(encoding='utf-8'))
    trials=json.loads((BASE/'webp-microbatches/status.json').read_text(encoding='utf-8'))
    old=json.loads((BASE/'status.json').read_text(encoding='utf-8'))
    original={row['phone_path']:row for row in old['records']}
    blocked={row['phone_path'] for row in trials['records'] if row['status']!='local_validation_passed'}
    plan=build_photo_plan(inventory,blocked);write('queue.proposed.json',plan)
    rows={r['path']:r for r in plan['records']}
    state.update(total_candidates=len(rows),total_original_bytes=sum(r['bytes'] for r in rows.values()),
                 excluded_known=len(plan['excluded']),exclusion_reasons=dict(Counter(r['reason'] for r in plan['excluded'])),
                 inventory_files=old['inventory_files'],processed_original_bytes=0,
                 outside_queue_formats={k:v for k,v in old['inventory_formats'].items() if k not in ('.jpg','.jpeg','.png')})
    selected=[]
    for method in ('webp_q85','webp_lossless'):
        selected.extend([r for r in trials['records'] if r['status']=='local_validation_passed'
                         and r['phone_path'] in rows and r['tests'][0]['method']==method][:2])
    state['pilot_total']=len(selected);save()
    for i,item in enumerate(selected):
        path=item['phone_path'];row=rows[path];token=hashlib.sha256(path.encode()).hexdigest()[:12]
        source=next((BASE/'samples'/token).glob('source*'))
        raw=OUT/'pilot'/(token+'-raw.webp');target=OUT/'pilot'/(token+'.webp')
        raw.parent.mkdir(parents=True,exist_ok=True)
        state.update(current_file=Path(path).name,phase=f'Preparing pilot {i+1}/{len(selected)}');save()
        if shutil.disk_usage(ROOT).free<8*1024**3:raise RuntimeError('8 GiB disk reserve required')
        if sha(source)!=original[path]['original_sha256']:raise ValueError('Local source changed')
        started=time.monotonic();is_png=source.suffix.casefold()=='.png'
        args=[str(CWEBP),'-quiet','-m','4','-metadata','none']
        args+=['-lossless','-exact'] if is_png else ['-q','85']
        subprocess.run(args+[str(source),'-o',str(raw)],capture_output=True,check=True,timeout=120)
        meta=inject_metadata(source,raw,target,mtime=row['mtime'])
        if meta['unsupported_features']:raise ValueError('Unsupported metadata in selected pilot')
        quality=compare(source,target)
        if is_png and not quality['pixels_identical']:raise ValueError('Lossless pixels changed')
        if not is_png and (quality['psnr_rgb_db'] is not None and quality['psnr_rgb_db']<38):
            raise ValueError('JPEG pilot failed numeric quality screen')
        if not quality['alpha_identical']:raise ValueError('Transparency changed')
        if target.stat().st_size>row['bytes']*.9:raise ValueError('Less than 10 percent gain')
        transport=phone_transport(ADB)
        value,transport=read_phone_shell(ADB,transport,'stat -c %s,%Y '+shlex.quote(path))
        if tuple(map(int,value.split(',')))!=(row['bytes'],row['mtime']):raise ValueError('Phone source changed')
        remote,transport=read_phone_shell(ADB,transport,'sha256sum '+shlex.quote(path))
        if remote.split()[0]!=sha(source):raise ValueError('Phone original no longer matches backup')
        where=shlex.quote("_data='"+path.replace("'","''")+"'")
        media,transport=read_phone_shell(ADB,transport,'content query --uri content://media/external/images/media --projection _id:_size:date_modified:datetaken:date_added:width:height:orientation:mime_type --where '+where)
        target_exists,transport=read_phone_shell(ADB,transport,'if [ -e '+shlex.quote(row['target_phone_path'])+' ]; then echo exists; fi')
        if target_exists:raise ValueError('Destination now exists on phone')
        with Image.open(target) as image:dimensions=list(image.size)
        record={'phone_path':path,'target_phone_path':row['target_phone_path'],'mtime':row['mtime'],
                'original_bytes':row['bytes'],'compressed_bytes':target.stat().st_size,
                'backup_path':str(source),'compressed_path':str(target),'original_sha256':sha(source),
                'compressed_sha256':sha(target),'output_resolution':dimensions,'source_media_row':media,
                'metadata_checks':meta,'quality_checks':quality,'status':'prepared_local_only',
                'seconds':round(time.monotonic()-started,3)}
        state['records'].append(record);state['prepared_pilot']+=1;save()
    manifest={'scope':'Publish four pilot WebP copies alongside originals; never delete originals',
              'phone_replacements':0,'records':state['records']}
    write('pilot-manifest.json',manifest)
    digest=hashlib.sha256((OUT/'pilot-manifest.json').read_bytes()).hexdigest()
    if not (OUT/'pilot-authorization.json').exists():
        write('pilot-authorization.json',{'approved':False,'scope':'publish_pilot_copies_only','manifest_sha256':digest})
    active=json.loads((ROOT/'outputs/phone-video-compression-batches/parallel-config.json').read_text(encoding='utf-8'))
    write('parallel-config.proposed.json',{**active,'cpu_video_enabled':False,
                                         'mode':'two_gpu_video_lanes_cpu_photos','preferred_encoders':['nvenc','amf']})
    state.update(state='awaiting_approval',phase='Four local pilot outputs ready; phone unchanged',
                 current_file=None,manifest_sha256=digest)
    save()


if __name__=='__main__':
    OUT.mkdir(parents=True,exist_ok=True)
    with file_mutex(OUT/'prepare.lock',timeout=1):
        try:main()
        except Exception as error:state.update(state='attention_required',error=str(error));save();raise
