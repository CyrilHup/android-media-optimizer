"""Bounded, numeric photo experiments on PC copies; never modifies phone media."""
from pathlib import Path
import ctypes
import hashlib
import json
import math
import os
import shlex
import shutil
import subprocess
import time
from datetime import datetime, timezone
from collections import defaultdict
from PIL import Image, ImageChops, ImageStat, ImageOps
from device import phone_transport, read_phone_shell
from parallel_resource_locks import file_mutex
from webp_metadata import inject_metadata

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'outputs/phone-photo-analysis'
ADB=ROOT/'work/android-tools/platform-tools/adb.exe'
CWEBP=Path('C:/msys64/mingw64/bin/cwebp.exe')
FORMATS={'.jpg','.jpeg','.png','.webp','.heic','.heif','.dng','.raw','.arw','.cr2','.nef','.avif','.gif'}
state={'state':'running','phase':'Inventory','completed':0,'total':0,'phone_replacements':0,
       'cpu_limit':'one logical core, below-normal priority','records':[]}


def save():
    state['updated_utc']=datetime.now(timezone.utc).isoformat()
    temporary=OUT/'status.new.json'
    temporary.write_text(json.dumps(state,indent=2),encoding='utf-8')
    temporary.replace(OUT/'status.json')


def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def public_photo(row):
    p=row['path'];rel=p.removeprefix('/storage/emulated/0/')
    return (p.startswith('/storage/emulated/0/') and rel.startswith(('DCIM/','Pictures/','Download/','Movies/'))
            and Path(p).suffix.lower() in FORMATS
            and not any(x.startswith('.') or x.casefold()=='private' for x in rel.split('/')))


def choose_samples(rows):
    groups=defaultdict(list)
    for row in rows:
        ext=Path(row['path']).suffix.lower()
        if ext not in ('.jpg','.jpeg','.png'):continue
        kind='screenshot' if 'screenshot' in row['path'].casefold() else 'other'
        groups[(kind,'png' if ext=='.png' else 'jpeg')].append(row)
    chosen={}
    for items in groups.values():
        items=sorted(items,key=lambda r:r['bytes'])
        for fraction in (.25,.5,.75,1):
            row=items[min(len(items)-1,int((len(items)-1)*fraction))]
            if row['bytes']<=20*1024**2:chosen[row['path']]=row
    return list(chosen.values())


def compare(source,target):
    with Image.open(source) as a, Image.open(target) as b:
        a.load();b.load()
        normalize_exif=lambda data:data[6:] if data.startswith(b'Exif\x00\x00') else data
        metadata={'icc_profile':a.info.get('icc_profile',b'')==b.info.get('icc_profile',b''),
                  'exif':normalize_exif(a.info.get('exif',b''))==normalize_exif(b.info.get('exif',b''))}
        orientation_same=a.getexif().get(274,1)==b.getexif().get(274,1)
        # Opaque JPEG/WebP pairs need no alpha plane or EXIF copy when their
        # orientation is already normal. This keeps the same full-image metric
        # while avoiding several hundred MB of redundant 108 MP allocations.
        alpha=any(im.mode in ('RGBA','LA') or 'transparency' in im.info for im in (a,b))
        mode='RGBA' if alpha else 'RGB'
        def normalized(im):
            if im.getexif().get(274,1) not in (None,1):im=ImageOps.exif_transpose(im)
            return im if im.mode==mode else im.convert(mode)
        a=normalized(a);b=normalized(b)
        if a.size!=b.size:raise ValueError('Dimensions changed')
        difference=ImageChops.difference(a,b)
        identical=all(high==0 for low,high in difference.getextrema())
        rgb=difference.convert('RGB') if alpha else difference
        mse=sum(ImageStat.Stat(rgb).sum2)/(a.width*a.height*3)
        return {'dimensions':list(a.size),'pixels_identical':identical,
                'psnr_rgb_db':None if mse==0 else 10*math.log10(255**2/mse),
                'alpha_identical':not alpha or difference.getchannel('A').getextrema()==(0,0),
                'icc_preserved':metadata['icc_profile'],'exif_preserved':metadata['exif'],
                'orientation_preserved':orientation_same}


def limit_cpu(max_cores=1):
    # Children inherit affinity/priority. cwebp is single-threaded without -mt.
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetCurrentProcess.restype=ctypes.c_void_p
    handle=kernel.GetCurrentProcess()
    kernel.SetPriorityClass.argtypes=[ctypes.c_void_p,ctypes.c_ulong]
    kernel.SetProcessAffinityMask.argtypes=[ctypes.c_void_p,ctypes.c_size_t]
    kernel.GetProcessAffinityMask.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_size_t),ctypes.POINTER(ctypes.c_size_t)]
    current=ctypes.c_size_t();system=ctypes.c_size_t()
    if not kernel.GetProcessAffinityMask(handle,ctypes.byref(current),ctypes.byref(system)):raise ctypes.WinError()
    available=current.value;mask=0
    for _ in range(max_cores):
        bit=available & -available
        mask|=bit;available-=bit
    if not kernel.SetProcessAffinityMask(handle,mask):raise ctypes.WinError()
    if not kernel.SetPriorityClass(handle,0x4000):raise ctypes.WinError()
    return True


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    state['cpu_limit_verified']=limit_cpu()
    rows=[r for r in json.loads((ROOT/'work/media-audit-2026-10-14/all-files.json').read_text(encoding='utf-8')) if public_photo(r)]
    totals=defaultdict(lambda:{'files':0,'bytes':0})
    for row in rows:
        entry=totals[Path(row['path']).suffix.lower()];entry['files']+=1;entry['bytes']+=row['bytes']
    samples=choose_samples(rows)
    state.update(inventory_date='2026-10-14',inventory_formats=dict(totals),inventory_files=len(rows),inventory_bytes=sum(r['bytes'] for r in rows),total=len(samples),sample_original_bytes=sum(r['bytes'] for r in samples),sampling='Size quartiles within screenshot/other and JPEG/PNG strata; not a random or collection-wide quality estimate',raw_policy='Inventory only. No RAW development or deletion.',tool_versions={'cwebp':subprocess.check_output([str(CWEBP),'-version']).decode().strip()})
    save()
    for row in samples:
        token=hashlib.sha256(row['path'].encode()).hexdigest()[:12]
        folder=OUT/'samples'/token;folder.mkdir(parents=True,exist_ok=True)
        source=folder/('source'+Path(row['path']).suffix)
        record={'phone_path':row['path'],'original_bytes':row['bytes'],'tests':[]}
        state.update(phase='Copying sample',current_file=Path(row['path']).name);save()
        try:
            if shutil.disk_usage(ROOT).free<8*1024**3:raise RuntimeError('Preserving 8 GiB free disk reserve')
            # Serialize short ADB reads/copies with video replacement; no phone mutation.
            with file_mutex(ROOT/'outputs/phone-video-compression-batches/phone-transaction.lock',timeout=120):
                transport=phone_transport(ADB)
                value,transport=read_phone_shell(ADB,transport,'stat -c %s,%Y '+shlex.quote(row['path']))
                if tuple(map(int,value.split(',')))!=(row['bytes'],row['mtime']):raise ValueError('Source changed since inventory')
                subprocess.run([str(ADB),'-s',transport,'pull',row['path'],str(source)],capture_output=True,check=True,timeout=90)
                remote,transport=read_phone_shell(ADB,transport,'sha256sum '+shlex.quote(row['path']))
                if sha(source)!=remote.split()[0]:raise ValueError('Backup hash mismatch')
            record['original_sha256']=sha(source)
            is_png=source.suffix.lower()=='.png'
            trials=[('webp_lossless',None)] if is_png else [('jpeg_q90',90),('jpeg_q85',85),('webp_q85',85)]
            for name,quality in trials:
                state.update(phase='Testing '+name);save()
                target=folder/(name+('.jpg' if name.startswith('jpeg') else '.webp'))
                started=time.monotonic()
                if name.startswith('jpeg'):
                    with Image.open(source) as image:
                        image.save(target,format='JPEG',quality=quality,optimize=True,
                                   exif=image.info.get('exif',b''),icc_profile=image.info.get('icc_profile'))
                else:
                    args=[str(CWEBP),'-quiet','-metadata','all','-m','4']
                    args+=['-lossless','-exact'] if quality is None else ['-q',str(quality)]
                    subprocess.run(args+[str(source),'-o',str(target)],capture_output=True,check=True,timeout=90)
                    muxed=target.with_suffix('.metadata.webp')
                    record.setdefault('webp_metadata_checks',{})[name]=inject_metadata(source,target,muxed,mtime=row['mtime'])
                    muxed.replace(target)
                checks=compare(source,target)
                record['tests'].append({'method':name,'bytes':target.stat().st_size,
                    'saved_fraction':1-target.stat().st_size/row['bytes'],
                    'seconds':round(time.monotonic()-started,3),'checks':checks,
                    'lossless_pixels_verified':quality is None and checks['pixels_identical'],
                    'local_copy_only':True})
            record['status']='tested'
        except Exception as error:
            record.update(status='error_original_untouched',error=str(error))
        state['records'].append(record);state['completed']+=1;save()
    state.update(state='complete',phase='Tests complete; originals unchanged',current_file=None);save()


if __name__=='__main__':
    try:
        with file_mutex(OUT/'analysis.lock',timeout=1):main()
    except Exception as error:
        if OUT.exists():state.update(state='attention_required',error=str(error));save()
        raise
