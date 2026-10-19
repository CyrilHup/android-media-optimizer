import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from compression_work_claims import claim_file, with_cpu_backlog


def competing_claim(folder, lane, start, results):
    start.wait(10)
    results.put(claim_file(folder, 'run', lane, 'same-video'))


class ClaimTests(unittest.TestCase):
    def initialize(self, folder):
        (folder/'parallel-active-run.json').write_text(json.dumps({'id':'run'}))
        (folder/'work-claims.json').write_text(json.dumps({'run_id':'run','owners':{}}))

    def test_three_processes_cannot_claim_same_video(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            self.initialize(folder)
            context=multiprocessing.get_context('spawn')
            start=context.Event()
            results=context.Queue()
            processes=[context.Process(target=competing_claim,args=(temporary,lane,start,results))
                       for lane in ('cpu','nvenc','amf')]
            try:
                for process in processes:process.start()
                start.set()
                answers=[results.get(timeout=15) for _ in processes]
                for process in processes:
                    process.join(15)
                    self.assertEqual(process.exitcode,0)
                self.assertEqual(sum(answers),1)
                self.assertEqual(len(json.loads((folder/'work-claims.json').read_text())['owners']),1)
            finally:
                for process in processes:
                    if process.is_alive():process.terminate();process.join()
                results.close()

    def test_claim_cannot_be_replayed_even_by_same_lane(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            self.initialize(folder)
            self.assertTrue(claim_file(folder,'run','cpu','video'))
            self.assertFalse(claim_file(folder,'run','cpu','video'))
            self.assertFalse(claim_file(folder,'run','nvenc','video'))
            self.assertTrue(claim_file(folder,'run','amf','other'))

    def test_old_worker_rejected_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            self.initialize(folder)
            with self.assertRaisesRegex(RuntimeError,'Superseded'):
                claim_file(folder,'old-run','cpu','video')
            self.assertEqual(json.loads((folder/'work-claims.json').read_text())['owners'],{})

    def test_cpu_receives_more_than_eight_small_candidates_without_large_files(self):
        rows=[{'path':str(i),'bytes':i+1000} for i in range(30)]
        large={'path':'large','bytes':100*1024**2}
        lanes={'cpu':rows[:8],'nvenc':rows[8:]+[large]}
        result=with_cpu_backlog(lanes,rows+[large])
        self.assertEqual(len(result['cpu']),30)
        self.assertNotIn(large,result['cpu'])
        self.assertEqual(len(lanes['cpu']),8)
        self.assertEqual(result['nvenc'],lanes['nvenc'])


if __name__=='__main__':unittest.main()
