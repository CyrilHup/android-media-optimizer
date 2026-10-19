"""JPEG fallback retaining APP/COM payloads; no geometric or colour conversion."""
from pathlib import Path
import struct
from PIL import Image


def jpeg_parts(data):
    if data[:2]!=b'\xff\xd8':raise ValueError('Not a JPEG')
    position=2;start=2;parts=[];metadata=[]
    while position<len(data):
        if data[position]!=255:raise ValueError('Invalid JPEG marker')
        marker_start=position
        while position<len(data) and data[position]==255:position+=1
        if position==len(data):raise ValueError('Truncated JPEG marker')
        marker=data[position];position+=1
        if marker==217:
            parts.append(data[start:position])
            if position!=len(data):raise ValueError('JPEG trailing payload must be preserved separately')
            return parts,metadata
        if marker in (0,216) or 208<=marker<=215:raise ValueError('Unexpected JPEG marker')
        if position+2>len(data):raise ValueError('Truncated JPEG segment')
        size=struct.unpack('>H',data[position:position+2])[0]
        if size<2 or position+size>len(data):raise ValueError('Invalid JPEG segment length')
        end=position+size
        if 224<=marker<=239 or marker==254:
            segment=data[marker_start:end]
            if marker==226 and data[position+2:end].startswith(b'MPF\0'):raise ValueError('Multiple JPEG images require dedicated preservation')
            parts.append(data[start:marker_start]);metadata.append(segment);start=end
        position=end
        if marker==218:
            # Scan entropy bytes until a non-stuffed, non-restart marker.
            while True:
                position=data.find(b'\xff',position)
                if position<0:raise ValueError('Missing JPEG end marker')
                next_byte=position+1
                while next_byte<len(data) and data[next_byte]==255:next_byte+=1
                if next_byte>=len(data):raise ValueError('Truncated JPEG scan')
                code=data[next_byte]
                if code==0 or 208<=code<=215:position=next_byte+1;continue
                break
    raise ValueError('Missing JPEG end marker')


def encode_preserving_jpeg(source,target,quality=90):
    source=Path(source);target=Path(target)
    _,original_metadata=jpeg_parts(source.read_bytes())
    with Image.open(source) as image:
        if image.format!='JPEG' or image.mode not in ('RGB','L') or getattr(image,'n_frames',1)!=1:
            raise ValueError('Unsupported JPEG pixel layout')
        xmp=image.info.get('xmp',b'')
        if any(marker in xmp for marker in (b'MotionPhoto',b'MicroVideo',b'GainMap',b'hdrgm:')):
            raise ValueError('JPEG contains motion or HDR metadata')
        # Match Adobe RGB versus YCbCr coding semantics. APP14 is preserved.
        adobe=image.info.get('adobe_transform')
        if adobe not in (None,0,1):raise ValueError('Unsupported Adobe colour transform')
        image.load()
        image.save(target,format='JPEG',quality=quality,optimize=True,progressive=False,keep_rgb=adobe==0,subsampling=0 if adobe==0 else 'keep')
    encoded=target.read_bytes();payload,_=jpeg_parts(encoded)
    with Image.open(target) as image:
        transform=image.info.get('adobe_transform')
        if adobe==0 and transform!=0:raise ValueError('JPEG RGB coding was not retained')
        if adobe in (None,1) and transform not in (None,1):raise ValueError('JPEG colour transform mismatch')
    target.write_bytes(b'\xff\xd8'+b''.join(original_metadata)+b''.join(payload))
    after_payload,after_metadata=jpeg_parts(target.read_bytes())
    if after_metadata!=original_metadata:raise ValueError('JPEG APP/COM metadata changed')
    if b''.join(after_payload)!=b''.join(payload):raise ValueError('JPEG encoded payload changed during metadata injection')
    # The worker's single full quality comparison checks actual decoded colour.
    return {'jpeg_app_com_segments_identical':True,'jpeg_encoded_payload_identical':True,'quality':quality}
