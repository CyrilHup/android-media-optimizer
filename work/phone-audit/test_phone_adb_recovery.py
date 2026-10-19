"""Mocked phone failure paths; no real ADB or personal media operations."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import device
import phone_video_transaction as transaction


class ReadRetryTests(unittest.TestCase):
    def test_read_retries_on_refreshed_verified_transport(self):
        failure=subprocess.CalledProcessError(1,['adb'],stderr=b'offline')
        with patch.object(device.subprocess,'run',side_effect=[failure,SimpleNamespace(stdout=b'ok\n')]) as run,patch.object(device,'phone_transport',return_value='verified-new'),patch.object(device.time,'sleep'):
            self.assertEqual(device.read_phone_shell('adb','verified-old','df -k /data'),('ok','verified-new'))
            self.assertEqual(run.call_args_list[1].args[0][2],'verified-new')

    def test_read_failures_are_bounded(self):
        failure=subprocess.CalledProcessError(1,['adb'],stderr=b'offline')
        with patch.object(device.subprocess,'run',side_effect=failure) as run,patch.object(device,'phone_transport',side_effect=RuntimeError('offline')),patch.object(device.time,'sleep'):
            with self.assertRaises(subprocess.CalledProcessError):device.read_phone_shell('adb','old','stat x')
            self.assertEqual(run.call_count,3)

    def test_resolver_skips_one_broken_transport(self):
        devices=SimpleNamespace(stdout=b'List of devices attached\nbroken device\ngood device\n')
        failed=subprocess.CalledProcessError(1,['adb'])
        with patch.object(device.subprocess,'run',side_effect=[devices,failed,SimpleNamespace(stdout=b'TESTPHONE123\n')]):
            self.assertEqual(device.phone_transport('adb',hardware_serial='TESTPHONE123'),'good')


class TransactionRecoveryTests(unittest.TestCase):
    def recover(self,corrupt_phone=False,scan_failure=False,index_never_updates=False):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            out=root/'outputs/phone-video-compression-batches'
            folder=out/'temporary'
            folder.mkdir(parents=True)
            original=folder/'source.mp4';original.write_bytes(b'original')
            candidate=folder/'candidate.mp4';candidate.write_bytes(b'copy')
            phone='/storage/emulated/0/DCIM/Camera/test.mp4'
            original_hash=hashlib.sha256(b'original').hexdigest()
            compressed_hash=hashlib.sha256(b'copy').hexdigest()
            row={'phone_path':phone,'backup_path':str(original),'compressed_path':str(candidate),
                 'original_sha256':original_hash,'compressed_sha256':compressed_hash,'original_bytes':8,'compressed_bytes':4,'mtime':1000,
                 'phone_stat_before':[8,1000],'phone_stage_path':'/storage/emulated/0/DCIM/Camera/.codex-compress-test.mp4',
                 'output_resolution':[1920,1080],'free_before_bytes':10_000_000,
                 'media_before':'_id=1, _size=8, date_modified=1000, datetaken=500, width=3840, height=2160',
                 'status':'replaced_verifying','original_backup_deleted':False,'compressed_pc_copy_deleted':False}
            journal=out/'transactions.json'
            journal.write_text(json.dumps({'transactions':[row]}),encoding='utf-8')
            telemetry=[]
            queries=[]
            scans=[]
            def read(_adb,transport,command,**_):
                if command.startswith('sha256sum '):return ('wrong' if corrupt_phone else compressed_hash)+'  file',transport
                if command.startswith('stat '):return '4,1000',transport
                if command.startswith('content query '):
                    queries.append(command)
                    if index_never_updates or (scan_failure and len(queries)<3):
                        return row['media_before'],transport
                    return '_id=1, _size=4, date_modified=1000, datetaken=500, width=1920, height=1080',transport
                if command=='df -k /data':
                    persisted=json.loads(journal.read_text(encoding='utf-8'))['transactions'][0]
                    self.assertEqual(persisted['status'],'verified_phone_replacement')
                    self.assertTrue(persisted['phone_hash_verified'])
                    self.assertEqual(persisted['media_index_updated'],not index_never_updates)
                    telemetry.append('failed-after-durable-verification')
                    raise subprocess.CalledProcessError(1,['adb'],stderr=b'transient df failure')
                raise AssertionError('Unexpected read '+command)
            def mutate(args,**_):
                self.assertTrue(args[-1].startswith('am broadcast '),'No upload or replacement is needed in this recovery')
                scans.append(args[-1])
                if scan_failure:
                    persisted=json.loads(journal.read_text(encoding='utf-8'))['transactions'][0]
                    self.assertTrue(persisted['phone_hash_verified'])
                    raise subprocess.CalledProcessError(1,args,stderr=b'transient scan failure')
                return SimpleNamespace(stdout=b'Broadcast completed')
            with patch.object(transaction,'ROOT',root),patch.object(transaction,'OUT',out),patch.object(transaction,'JOURNAL',journal),patch.object(transaction,'phone_transport',return_value='verified'),patch.object(transaction,'read_phone_shell',side_effect=read),patch.object(transaction.subprocess,'run',side_effect=mutate),patch.object(transaction.time,'sleep'),patch('builtins.print'):
                if corrupt_phone:
                    with self.assertRaises(AssertionError):transaction.apply(row,resume_index=0)
                    self.assertTrue(original.exists() and candidate.exists())
                    self.assertEqual(telemetry,[])
                else:
                    result=transaction.apply(row,resume_index=0)
                    self.assertEqual(result['status'],'verified_phone_replacement' if index_never_updates else 'complete_temporary_copies_removed')
                    self.assertIsNone(result['free_after_bytes'])
                    self.assertIn('transient df failure',result['free_after_measurement_error']['stderr'])
                    self.assertEqual(original.exists(),index_never_updates)
                    self.assertEqual(candidate.exists(),index_never_updates)
                    self.assertEqual(telemetry,['failed-after-durable-verification'])
                    self.assertEqual(len(scans),3 if index_never_updates else 1 if scan_failure else 0)
                    if scan_failure:self.assertIn('transient scan failure',result['scan_broadcast_errors'][0]['stderr'])

    def test_optional_df_failure_does_not_invalidate_verified_recovery(self):self.recover()
    def test_critical_hash_mismatch_still_preserves_both_backups(self):self.recover(corrupt_phone=True)
    def test_failed_scan_is_not_fatal_when_actual_index_becomes_correct(self):self.recover(scan_failure=True)
    def test_failed_scans_never_allow_cleanup_when_index_stays_wrong(self):self.recover(scan_failure=True,index_never_updates=True)

    def test_ambiguous_adb_failure_resumes_from_existing_journal_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            journal=Path(folder)/'transactions.json'
            journal.write_text(json.dumps({'transactions':[{'phone_path':'A','status':'replaced_verifying'}]}),encoding='utf-8')
            failure=subprocess.CalledProcessError(1,['adb','mv'],stderr=b'lost acknowledgement')
            with patch.object(transaction,'OUT',Path(folder)),patch.object(transaction,'JOURNAL',journal),patch.object(transaction,'_apply_locked',side_effect=[failure,{'ok':True}]) as apply_locked,patch.object(transaction.time,'sleep'),patch('builtins.print'):
                self.assertEqual(transaction.apply({'phone_path':'A'}),{'ok':True})
                self.assertEqual(apply_locked.call_args_list[1].args[2],0)

    def test_logical_integrity_failure_is_never_automatically_retried(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(transaction,'OUT',Path(folder)),patch.object(transaction,'_apply_locked',side_effect=AssertionError('hash mismatch')) as apply_locked:
            with self.assertRaises(AssertionError):transaction.apply({'phone_path':'A'})
            self.assertEqual(apply_locked.call_count,1)


if __name__=='__main__':unittest.main()
