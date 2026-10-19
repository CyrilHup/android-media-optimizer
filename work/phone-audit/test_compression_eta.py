"""ETA regressions: real progress, regime isolation, pauses and fast rejects."""
import unittest
from compression_eta import CompressionETA,progress_eta,phase_key


class Clock:
    now=0.0
    def __call__(self):return self.now


def history(status='completed',regime='nvenc+amf',version=5):
    return [{'policy_version':version,'regime':regime,'original_bytes':1000,'active_seconds':100,'status':status,'phase_seconds':{'preflight':10,'encode':50,'decode':20,'replace':20}} for _ in range(3)]


class CompressionETATests(unittest.TestCase):
    def test_progress_does_not_invent_eta_before_start(self):
        self.assertIsNone(progress_eta(30,None))
        self.assertIsNone(progress_eta(30,0))
        self.assertIsNone(progress_eta(30,1))
        self.assertEqual(progress_eta(30,25),90)
        self.assertEqual(progress_eta(30,100),0)

    def test_old_policy_and_different_gpu_do_not_calibrate_new_regime(self):
        clock=Clock()
        tracker=CompressionETA(history(version=4)+history(regime='amf'),5,'nvenc+amf',clock)
        result=tracker.snapshot({'status':'running','remaining_candidates':10,'remaining_original_bytes':10000})
        self.assertIsNone(result['global'])
        self.assertEqual(result['calibration_processed_files'],0)

    def test_skips_alone_do_not_produce_falsely_fast_global_eta(self):
        tracker=CompressionETA(history(status='skipped_quality'),5,'nvenc+amf',Clock())
        self.assertIsNone(tracker.snapshot({'status':'running','remaining_candidates':10,'remaining_original_bytes':10000})['global'])

    def test_disconnection_time_is_excluded_from_active_processing(self):
        clock=Clock();tracker=CompressionETA([],5,'amf',clock)
        tracker.begin_file('a.mp4',1000);tracker.observe('Encoding 1080p copy','running')
        clock.now=10;tracker.observe('Waiting for wireless ADB','waiting_for_phone')
        clock.now=110;tracker.observe('Encoding 1080p copy','running')
        clock.now=120;record=tracker.finish_file('completed')
        self.assertEqual(record['active_seconds'],20)
        self.assertNotIn('waiting',record['phase_seconds'])

    def test_phase_uses_samples_then_live_progress_and_file_includes_tail(self):
        clock=Clock();tracker=CompressionETA(history(),5,'nvenc+amf',clock)
        tracker.begin_file('a.mp4',1000);tracker.selected_encode(120,'nvenc-bps123')
        tracker.observe('Encoding 1080p copy','running')
        clock.now=20;tracker.observe('Encoding 1080p copy','running')
        state={'status':'running','remaining_candidates':10,'remaining_original_bytes':10000}
        sample=tracker.snapshot(state)
        self.assertEqual(sample['phase']['remaining_seconds'],100)
        self.assertEqual(sample['phase']['basis'],'selected_samples')
        live=tracker.snapshot({**state,'phase_progress_percent':50})
        self.assertEqual(live['phase']['remaining_seconds'],20)
        self.assertEqual(live['file']['remaining_seconds'],60)
        self.assertGreaterEqual(live['global']['remaining_seconds'],live['file']['remaining_seconds'])

    def test_completed_work_is_not_subtracted_twice_from_remaining_queue(self):
        clock=Clock();tracker=CompressionETA(history(),5,'nvenc+amf',clock)
        tracker.begin_file('a.mp4',1000);tracker.observe('Encoding 1080p copy','running')
        clock.now=100;tracker.finish_file('completed')
        self.assertEqual(tracker.active_seconds,0)
        result=tracker.snapshot({'status':'running','remaining_candidates':2,'remaining_original_bytes':2000})
        self.assertGreater(result['global']['remaining_seconds'],150)
        self.assertIsNone(result['file'])

    def test_paused_estimate_is_explicitly_suspended(self):
        tracker=CompressionETA(history(),5,'nvenc+amf',Clock())
        self.assertTrue(tracker.snapshot({'status':'paused'})['suspended'])
        self.assertEqual(tracker.snapshot({'status':'complete'})['global']['remaining_seconds'],0)

    def test_sample_quality_is_not_confused_with_final_quality(self):
        self.assertEqual(phase_key('Quality screening section 1/3','foo-sample-0-quality-0'),'preflight')
        self.assertEqual(phase_key('Quality screening section 1/3','foo-quality-0'),'quality')

    def test_phase_without_percent_uses_comparable_measured_occurrences(self):
        records=history()
        for row in records:
            row['phase_seconds']['verify']=16
            row['phase_counts']={'verify':2}
        clock=Clock();tracker=CompressionETA(records,5,'nvenc+amf',clock)
        tracker.begin_file('a.mp4',1000);tracker.observe('Verifying original backup','running')
        clock.now=4;tracker.observe('Verifying original backup','running')
        phase=tracker.snapshot({'status':'running'})['phase']
        self.assertEqual(phase['remaining_seconds'],4)
        self.assertEqual(phase['basis'],'historical_phase')

    def test_background_completion_keeps_next_file_timing_intact(self):
        clock=Clock();tracker=CompressionETA([],6,'nvenc+pipeline',clock)
        tracker.begin_file('a.mp4',1000);tracker.observe('Encoding 1080p copy','running')
        clock.now=10;first=tracker.finish_file('pending_phone_transfer',defer_transfer=True)
        self.assertEqual(tracker.history,[])
        tracker.begin_file('b.mp4',2000);tracker.observe('Encoding 1080p copy','running')
        clock.now=12;tracker.observe('Encoding 1080p copy','running')
        completed=tracker.complete_transfer(first,5)
        self.assertEqual(tracker.path,'b.mp4')
        self.assertEqual(tracker.active_seconds,2)
        self.assertEqual(completed['active_seconds'],15)
        self.assertEqual(tracker.history[0]['status'],'completed')

    def test_pipeline_global_eta_counts_overlapping_wall_time_once(self):
        clock=Clock();tracker=CompressionETA([],6,'nvenc+pipeline',clock)
        previous=None
        for index in range(3):
            tracker.begin_file(str(index),1000);tracker.observe('Encoding 1080p copy','running')
            clock.now=(index+1)*10
            timing=tracker.finish_file('pending_phone_transfer',defer_transfer=True)
            if previous:tracker.complete_transfer(previous,10)
            previous=timing
        tracker.observe('Finishing in-flight phone transfer safely','running')
        clock.now=40;tracker.observe('Finishing in-flight phone transfer safely','running')
        tracker.complete_transfer(previous,10)
        result=tracker.snapshot({'status':'running','remaining_candidates':3,'remaining_original_bytes':3000})
        self.assertEqual(result['global']['remaining_seconds'],40)
        self.assertEqual(result['global']['basis'],'observed_pipeline_throughput')


if __name__=='__main__':unittest.main()
