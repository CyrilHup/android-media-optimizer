"""Explicit WebP metadata muxing, without recompressing image payloads."""
from pathlib import Path
import json
import os
import struct
import subprocess
import tempfile
from datetime import datetime,timezone
from PIL import Image, ImageCms

WEBPMUX=Path('C:/msys64/mingw64/bin/webpmux.exe')


def raw_profile_exif(text):
    lines=text.strip().splitlines()
    if len(lines)<3 or lines[0].strip().casefold() not in ('app1','exif','generic profile'):
        raise ValueError('Unknown raw EXIF profile')
    expected=int(lines[1].strip())
    value=bytes.fromhex(''.join(lines[2:]))
    if len(value)!=expected:
        raise ValueError('Invalid raw EXIF profile')
    if value.startswith((b'II\x2a\x00',b'MM\x00\x2a')):
        value=b'Exif\x00\x00'+value
    if not value.startswith(b'Exif\x00\x00'):
        raise ValueError('Invalid raw EXIF profile')
    parsed=Image.Exif();parsed.load(value)
    return value


def metadata(path):
    with Image.open(path) as image:
        # JPEG APP metadata is available from its header. Unsupported Adobe
        # files can be kept without decoding e.g. a 108-megapixel bitmap.
        # PNG ancillary text can follow IDAT, so still fully load those files.
        if image.format not in ('JPEG','MPO'):image.load()
        info=image.info
        exif=info.get('exif',b'')
        if exif.startswith((b'II\x2a\x00',b'MM\x00\x2a')):
            exif=b'Exif\x00\x00'+exif
        raw=info.get('Raw profile type APP1') or info.get('Raw profile type exif')
        if raw:
            recovered=raw_profile_exif(raw)
            if exif and exif!=recovered:raise ValueError('Conflicting EXIF copies')
            exif=recovered
        xmp=info.get('xmp') or info.get('XML:com.adobe.xmp') or b''
        if isinstance(xmp,str):xmp=xmp.encode('utf-8')
        issues=[]
        if image.mode not in ('1','L','LA','P','RGB','RGBA'):
            issues.append('unvalidated_pixel_mode:'+image.mode)
        if getattr(image,'n_frames',1)!=1 or 'mp' in info or 'mpoffset' in info:
            issues.append('multiple_images_or_gain_map_not_preserved')
        if any(marker in xmp for marker in (b'hdrgm:',b'hdr-gain-map',b'GainMap')):
            issues.append('hdr_gain_map_not_preserved')
        known={'exif','icc_profile','xmp','XML:com.adobe.xmp','srgb','dpi',
               'jfif','jfif_version','jfif_unit','jfif_density','mp','mpoffset',
               'Raw profile type APP1','Raw profile type exif','transparency'}
        issues.extend('unmapped_metadata:'+key for key in info if key not in known)
        icc=info.get('icc_profile',b'')
        srgb_mapping=not icc and 'srgb' in info
        if srgb_mapping and info['srgb']!=0:
            issues.append('unvalidated_srgb_rendering_intent')
        if srgb_mapping:icc=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        return {'exif':exif,'icc':icc,'xmp':xmp,'srgb_mapping':srgb_mapping,
                'raw_profile_recovered':bool(raw),'issues':issues}


def chunks(path):
    data=Path(path).read_bytes()
    if data[:4]!=b'RIFF' or data[8:12]!=b'WEBP' or len(data)!=struct.unpack('<I',data[4:8])[0]+8:
        raise ValueError('Invalid WebP RIFF container')
    result=[];offset=12
    while offset<len(data):
        if offset+8>len(data):raise ValueError('Truncated WebP chunk header')
        size=struct.unpack('<I',data[offset+4:offset+8])[0]
        end=offset+8+size
        if end>len(data):raise ValueError('Truncated WebP chunk')
        result.append((data[offset:offset+4],data[offset+8:end]))
        offset=end+(size%2)
    if offset!=len(data):raise ValueError('Invalid WebP padding')
    return result


def inject_metadata(source,encoded,target,mtime=None,gallery_taken_ms=None,source_metadata=None):
    source,encoded,target=map(Path,(source,encoded,target))
    if target.resolve() in (source.resolve(),encoded.resolve()):
        raise ValueError('Metadata output must be a new local copy')
    values=dict(source_metadata) if source_metadata is not None else metadata(source)
    date_enriched=False
    if gallery_taken_ms is not None and gallery_taken_ms>0:
        exif=Image.Exif()
        if values['exif']:exif.load(values['exif'])
        camera=dict(exif.get_ifd(34665)) if 34665 in exif else {}
        if not camera.get(36867):
            if 37500 in camera:
                raise ValueError('Cannot safely enrich date in an EXIF MakerNote container')
            taken=datetime.fromtimestamp(gallery_taken_ms/1000,timezone.utc)
            camera.update({36867:taken.strftime('%Y:%m:%d %H:%M:%S'),
                           36881:'+00:00',37521:f'{gallery_taken_ms%1000:03d}'})
            exif[34665]=camera
            values['exif']=exif.tobytes();date_enriched=True
    # WebP EXIF chunks carry a TIFF payload, without JPEG's six-byte APP1
    # identifier. Pillow accepts both; Android's framework reader expects TIFF.
    wire=dict(values)
    if wire['exif'].startswith(b'Exif\x00\x00'):wire['exif']=wire['exif'][6:]
    original_pixels=[(kind,data) for kind,data in chunks(encoded) if kind in (b'VP8 ',b'VP8L',b'ALPH')]
    if not original_pixels:raise ValueError('Missing WebP image payload')
    target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='webp-metadata-',dir=target.parent) as folder:
        current=encoded
        for kind in ('exif','icc','xmp'):
            if not wire[kind]:continue
            payload=Path(folder)/(kind+'.bin');payload.write_bytes(wire[kind])
            output=Path(folder)/(kind+'.webp')
            subprocess.run([str(WEBPMUX),'-set',kind,str(payload),str(current),'-o',str(output)],
                           capture_output=True,check=True,timeout=30)
            current=output
        result=Path(folder)/'final.webp'
        result.write_bytes(current.read_bytes())
        after=chunks(result)
        if original_pixels!=[(kind,data) for kind,data in after if kind in (b'VP8 ',b'VP8L',b'ALPH')]:
            raise ValueError('Metadata mux changed encoded image payload')
        by_kind={kind:data for kind,data in after}
        checks={kind:by_kind.get(tag,b'')==wire[kind]
                for kind,tag in (('exif',b'EXIF'),('icc',b'ICCP'),('xmp',b'XMP '))}
        if not all(checks.values()):raise ValueError('WebP metadata chunk mismatch')
        result.replace(target)
    if mtime is not None:
        os.utime(target,(mtime,mtime))
        if int(target.stat().st_mtime)!=mtime:raise ValueError('Modification date changed')
    # Check that an independent image reader can parse the original EXIF bytes.
    with Image.open(target) as image:
        if values['exif']:
            expected=Image.Exif();expected.load(values['exif'])
            if dict(image.getexif())!=dict(expected):raise ValueError('Decoded EXIF values differ')
    return {'exif_bytes_identical':checks['exif'],'icc_bytes_identical':checks['icc'],
            'xmp_bytes_identical':checks['xmp'],'encoded_image_payload_identical':True,
            'mtime_preserved':mtime is not None,'srgb_mapped_to_icc':values['srgb_mapping'],
            'raw_profile_exif_recovered':values['raw_profile_recovered'],
            'capture_date_enriched_from_original_mediastore':date_enriched,
            'webp_exif_uses_tiff_header':not wire['exif'] or wire['exif'].startswith((b'II\x2a\x00',b'MM\x00\x2a')),
            'unsupported_features':values['issues']}
