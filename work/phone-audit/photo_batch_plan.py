"""Pure photo plan and exact-manifest approval guard; no phone access."""
from pathlib import PurePosixPath
import hashlib


def public_photo_path(path):
    prefix='/storage/emulated/0/'
    if not path.startswith(prefix):return False
    relative=path[len(prefix):]
    return (relative.startswith(('DCIM/','Pictures/','Download/','Movies/'))
            and PurePosixPath(relative).suffix.lower() in ('.jpg','.jpeg','.png')
            and not any(p.startswith('.') or p.casefold()=='private' for p in relative.split('/')))


def build_photo_plan(inventory,known_exclusions):
    rows=[r for r in inventory if public_photo_path(r['path']) and r['bytes']>0]
    existing={r['path'].casefold() for r in inventory}
    targets={}
    for row in rows:
        target=str(PurePosixPath(row['path']).with_suffix('.webp'))
        targets.setdefault(target.casefold(),[]).append(row['path'])
    planned=[];excluded=[]
    for row in rows:
        target=str(PurePosixPath(row['path']).with_suffix('.webp'))
        reason=('known_unsupported_metadata_or_hdr' if row['path'] in known_exclusions
                else 'existing_destination' if target.casefold() in existing
                else 'ambiguous_same_stem' if len(targets[target.casefold()])>1 else None)
        record={**row,'target_phone_path':target}
        if reason:excluded.append({**record,'reason':reason})
        else:planned.append(record)
    return {'records':planned,'excluded':excluded,'requires_per_file_metadata_validation':True}


def require_pilot_approval(authorization,manifest_bytes,records):
    digest=hashlib.sha256(manifest_bytes).hexdigest()
    if (authorization.get('approved') is not True
            or authorization.get('scope')!='publish_pilot_copies_only'
            or authorization.get('manifest_sha256')!=digest):
        raise PermissionError('Explicit approval of this exact photo pilot is required')
    if not records or len(records)>4:
        raise PermissionError('Photo pilot is limited to four exact files')
    for row in records:
        source=row['phone_path'];target=row['target_phone_path']
        if (not public_photo_path(source)
                or target!=str(PurePosixPath(source).with_suffix('.webp'))):
            raise PermissionError('Pilot target is outside the authorized public photo scope')
    if len({r['target_phone_path'].casefold() for r in records})!=len(records):
        raise PermissionError('Duplicate pilot target')
    return digest
