"""Fake phone integration tests. No real ADB commands or personal media."""
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import publish_photo_pilot as publisher


class FakePhone:
    source='/storage/emulated/0/Pictures/a.jpg'
    target='/storage/emulated/0/Pictures/a.webp'
    def __init__(self):
        self.files={self.source:b'original'};self.times={self.source:100};self.pushes=0
        self.rows={self.source:{'_id':7,'_size':8,'date_modified':100,'datetaken':100000,'date_added':100,'width':10,'height':20,'orientation':0}}
        self.commands=[];self.fail_index=False

    def shell(self,command):
        self.commands.append(command);args=shlex.split(command)
        if args[0]=='if':return 'exists' if args[3] in self.files else ''
        if args[0]=='stat':return f'{len(self.files[args[-1]])},{self.times[args[-1]]}'
        if args[0]=='sha256sum':return hashlib.sha256(self.files[args[-1]]).hexdigest()+'  '+args[-1]
        if args[0]=='df':return 'Filesystem 1K-blocks Used Available Use% Mounted\n/data 9999999 1000 9000000 1% /data'
        if args[0]=='TZ=UTC':self.times[args[-1]]=100;return ''
        if args[0]=='mv':
            stage,target=args[-2:]
            if args[1]=='-f' or target not in self.files:
                self.files[target]=self.files.pop(stage);self.times[target]=self.times.pop(stage)
            return ''
        if args[:2]==['content','query']:
            where=args[args.index('--where')+1];path=re.search("_data='(.*)'",where).group(1)
            row=self.rows.get(path)
            if row is None:return 'No result found.'
            return 'Row: 0 '+', '.join(f'{k}={v}' for k,v in row.items())+', mime_type='+('image/webp' if path==self.target else 'image/jpeg')
        if args[:2]==['am','broadcast']:
            self.rows[self.target]={'_id':8,'_size':len(self.files[self.target]),'date_modified':100,'datetaken':999999,'date_added':999,'width':10,'height':20,'orientation':0}
            return 'Broadcast completed'
        if args[:2]==['content','update']:
            if self.fail_index:raise subprocess.CalledProcessError(1,command,stderr=b'permission denied')
            assert args[args.index('--uri')+1].endswith('/8')
            for i,arg in enumerate(args):
                if arg=='--bind':
                    key,_,value=args[i+1].split(':');self.rows[self.target][key]=int(value)
            return 'Updated 1 rows'
        raise AssertionError('Unexpected fake phone command '+command)

    def run(self,args,**kwargs):
        if args[3]=='push':
            self.files[args[-1]]=Path(args[-2]).read_bytes();self.times[args[-1]]=999;self.pushes+=1
            return SimpleNamespace(stdout=b'')
        assert args[3]=='shell',args
        return SimpleNamespace(stdout=self.shell(args[4]).encode())


class PilotPublicationTests(unittest.TestCase):
    def setup_files(self,root,approved):
        out=root/'outputs/phone-photo-compression-batches';out.mkdir(parents=True)
        backup=root/'outputs/phone-photo-analysis/samples/abc/source.jpg';backup.parent.mkdir(parents=True);backup.write_bytes(b'original')
        compressed=out/'pilot/abc.webp';compressed.parent.mkdir();compressed.write_bytes(b'webp')
        phone=FakePhone()
        before=phone.shell("content query --where \"_data='"+phone.source+"'\"")
        row={'phone_path':phone.source,'target_phone_path':phone.target,'backup_path':str(backup),
             'compressed_path':str(compressed),'original_sha256':publisher.sha(backup),
             'compressed_sha256':publisher.sha(compressed),'original_bytes':8,'compressed_bytes':4,
             'mtime':100,'source_media_row':before}
        payload=json.dumps({'records':[row]}).encode();(out/'pilot-manifest.json').write_bytes(payload)
        (out/'pilot-authorization.json').write_text(json.dumps({'approved':approved,'scope':'publish_pilot_copies_only','manifest_sha256':hashlib.sha256(payload).hexdigest()}))
        (out/'status.json').write_text('{}')
        return out,phone,backup,compressed

    def execute(self,root,out,phone):
        with patch.object(publisher,'ROOT',root),patch.object(publisher,'OUT',out),patch.object(publisher,'phone_transport',return_value='fake'),patch.object(publisher,'read_phone_shell',side_effect=lambda adb,transport,command,**kw:(phone.shell(command),transport)),patch.object(publisher.subprocess,'run',side_effect=phone.run),patch.object(publisher.time,'sleep'):
            publisher.main()

    def test_no_authorization_means_no_phone_access(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);out,phone,_,_=self.setup_files(root,False)
            with patch.object(publisher,'OUT',out),patch.object(publisher,'phone_transport') as discover:
                with self.assertRaises(PermissionError):publisher.main()
                discover.assert_not_called()

    def test_pilot_preserves_original_and_dates_and_is_resumable(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);out,phone,backup,compressed=self.setup_files(root,True)
            self.execute(root,out,phone);self.execute(root,out,phone)
            self.assertEqual(phone.files[phone.source],b'original')
            self.assertEqual(phone.files[phone.target],b'webp')
            self.assertEqual(phone.rows[phone.target]['datetaken'],100000)
            self.assertEqual(phone.rows[phone.target]['date_added'],999)
            self.assertEqual(phone.rows[phone.source]['date_added'],100)
            journal=json.loads((out/'pilot-publication.json').read_text())
            self.assertTrue(journal['records'][phone.source]['date_added_differs_for_new_copy'])
            self.assertEqual(phone.rows[phone.source]['_id'],7)
            self.assertEqual(phone.pushes,1)
            self.assertTrue(backup.exists() and compressed.exists())
            self.assertFalse(any(c.startswith('rm ') for c in phone.commands))
            self.assertEqual(json.loads((out/'status.json').read_text())['state'],'awaiting_gallery_review')

    def test_index_failure_keeps_every_original_and_resume_does_not_reupload(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);out,phone,backup,compressed=self.setup_files(root,True)
            phone.fail_index=True
            with self.assertRaises(subprocess.CalledProcessError):self.execute(root,out,phone)
            self.assertEqual(phone.files[phone.source],b'original')
            self.assertTrue(backup.exists() and compressed.exists())
            phone.fail_index=False;self.execute(root,out,phone)
            self.assertEqual(phone.pushes,1)

    def test_existing_destination_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);out,phone,_,_=self.setup_files(root,True)
            phone.files[phone.target]=b'unrelated'
            with self.assertRaisesRegex(ValueError,'collision'):self.execute(root,out,phone)
            self.assertEqual(phone.files[phone.target],b'unrelated')
            self.assertEqual(phone.pushes,0)

    def test_only_approved_owned_pilot_copy_can_be_refreshed(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);out,phone,_,compressed=self.setup_files(root,True)
            self.execute(root,out,phone)
            old=publisher.sha(compressed);compressed.write_bytes(b'new')
            manifest=json.loads((out/'pilot-manifest.json').read_text());row=manifest['records'][0]
            row.update(compressed_bytes=3,compressed_sha256=publisher.sha(compressed))
            payload=json.dumps(manifest).encode();(out/'pilot-manifest.json').write_bytes(payload)
            digest=hashlib.sha256(payload).hexdigest()
            auth=json.loads((out/'pilot-authorization.json').read_text());auth['manifest_sha256']=digest
            (out/'pilot-authorization.json').write_text(json.dumps(auth))
            journal=json.loads((out/'pilot-publication.json').read_text());journal['manifest_sha256']=digest
            entry=journal['records'][phone.source];entry.update(previous_compressed_sha256=old,approved_refresh_compressed_sha256=row['compressed_sha256'])
            (out/'pilot-publication.json').write_text(json.dumps(journal))
            self.execute(root,out,phone)
            self.assertEqual(phone.files[phone.target],b'new')
            self.assertEqual(phone.files[phone.source],b'original')
            self.assertEqual(phone.pushes,2)
