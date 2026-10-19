import unittest
from network_recovery import retry_video_network_failure,fresh_worker_status
from photo_timing import PhotoTiming


class RecoveryTests(unittest.TestCase):
    def test_only_confirmed_network_failure_restarts(self):
        error='The previously verified Nothing phone is not connected.'
        self.assertTrue(retry_video_network_failure({'error':error}))
        self.assertTrue(retry_video_network_failure({'error':'device.PhoneUnavailableError: '+error}))
        self.assertTrue(retry_video_network_failure({'workers':[{'status':'attention_required','error':'Phone transfer failed; backups retained: '+error}]}))
        for message in ('Photo target hash mismatch','AssertionError','Command adb returned non-zero exit status 1',''):
            self.assertFalse(retry_video_network_failure({'error':message}))
        self.assertFalse(retry_video_network_failure({'workers':[{'status':'attention_required','error':error},{'status':'attention_required','error':'Original hash changed'}]}))

    def test_status_freshness_compares_instants_not_timezone_strings(self):
        self.assertFalse(fresh_worker_status({'updated_utc':'2026-10-18T21:00:00+00:00'},'2026-10-18T17:01:00-04:00'))
        self.assertTrue(fresh_worker_status({'updated_utc':'2026-10-18T21:02:00+00:00'},'2026-10-18T17:01:00-04:00'))
        self.assertFalse(fresh_worker_status({},''))

    def test_timing_stages_and_operations_are_separate(self):
        now=[0.0];timer=PhotoTiming(lambda:now[0])
        with timer.stage('download'):
            with timer.operation('read'):now[0]=2
            now[0]=3
        with timer.stage('wifi_wait'):now[0]=8
        result=timer.snapshot()
        self.assertEqual(result['wall_seconds'],8)
        self.assertEqual(result['stages_seconds'],{'download':3,'wifi_wait':5})
        self.assertEqual(result['adb_operations']['read'],{'calls':1,'seconds':2,'failures':0})
        with self.assertRaises(RuntimeError):
            with timer.operation('read'):now[0]=9;raise RuntimeError()
        self.assertEqual(timer.snapshot()['adb_operations']['read']['failures'],1)


if __name__=='__main__':unittest.main()
