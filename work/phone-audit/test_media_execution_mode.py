import unittest
from unittest.mock import patch
from media_execution_mode import plan_video_work,permitted_video_encoders
from parallel_compression_coordinator import assign_lanes
from compression_work_claims import with_cpu_backlog


class MediaExecutionModeTests(unittest.TestCase):
    def test_gpu_only_has_no_cpu_lane_even_for_small_files(self):
        rows=[{'path':str(i),'bytes':1000,'mtime':1} for i in range(20)]
        lanes,deferred=plan_video_work(rows,{},['nvenc','amf'],{'cpu_video_enabled':False},assign_lanes,with_cpu_backlog)
        self.assertEqual(set(lanes),{'nvenc','amf'})
        self.assertEqual(sum(map(len,lanes.values())),20)
        self.assertEqual(deferred,[])

    def test_known_dolby_is_deferred_not_sent_to_gpu(self):
        rows=[{'path':'dolby','bytes':1000,'mtime':1}]
        with patch('media_execution_mode.cpu_only_row',return_value=True):
            lanes,deferred=plan_video_work(rows,{},['nvenc'],{'cpu_video_enabled':False},assign_lanes,with_cpu_backlog)
        self.assertEqual(deferred,rows)
        self.assertTrue(all(not items for items in lanes.values()))

    def test_default_retains_current_running_policy(self):
        rows=[{'path':str(i),'bytes':1000,'mtime':1} for i in range(20)]
        lanes,_=plan_video_work(rows,{},['nvenc','amf'],{},assign_lanes,with_cpu_backlog)
        self.assertEqual(len(lanes['cpu']),20)

    def test_gpu_mode_cannot_silently_use_cpu(self):
        self.assertEqual(permitted_video_encoders(['nvenc','amf'],'amf',False,False),['amf','nvenc'])
        self.assertEqual(permitted_video_encoders(['nvenc'],'nvenc',True,False),[])
        with self.assertRaisesRegex(RuntimeError,'no GPU'):
            plan_video_work([],{},[],{'cpu_video_enabled':False},assign_lanes,with_cpu_backlog)
