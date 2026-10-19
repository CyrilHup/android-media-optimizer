"""Disjoint dispatch/receipt merge guards; no coordinator or phone startup."""
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from parallel_compression_coordinator import assign_lanes,merge_receipts,reconcile_baseline,main


class CoordinatorTests(unittest.TestCase):
    def test_every_path_has_exactly_one_lane_with_bounded_tiny_cpu_work(self):
        rows=[{'path':str(i),'bytes':30*1024**2,'mtime':1} for i in range(20)]
        rows+=[{'path':'large','bytes':500*1024**2,'mtime':1}]
        lanes=assign_lanes(rows,{},['nvenc','amf'])
        paths=[r['path'] for values in lanes.values() for r in values]
        self.assertEqual(len(paths),len(set(paths)))
        self.assertEqual(set(paths),{r['path'] for r in rows})
        self.assertEqual(len(lanes['cpu']),8)
        self.assertNotIn('large',[r['path'] for r in lanes['cpu']])
        self.assertTrue(lanes['nvenc'] and lanes['amf'])

    def test_cpu_fallback_covers_all_paths_without_a_gpu(self):
        rows=[{'path':'A','bytes':99,'mtime':1}]
        self.assertEqual(assign_lanes(rows,{},[]),{'cpu':rows})

    def test_known_dolby_stays_on_cpu_even_when_large(self):
        rows=[{'path':'DV','bytes':5_000_000_000,'mtime':1},{'path':'SDR','bytes':500_000_000,'mtime':1}]
        with patch('parallel_compression_coordinator.cpu_only_row',side_effect=lambda row,_:row['path']=='DV'):
            lanes=assign_lanes(rows,{},['nvenc','amf'])
        self.assertEqual([r['path'] for r in lanes['cpu']],['DV'])
        self.assertEqual([r['path'] for name,rs in lanes.items() if name!='cpu' for r in rs],['SDR'])

    def test_receipt_merge_never_duplicates_seed_history(self):
        baseline=[{'phone_path':'old','status':'completed'}]
        a=baseline+[{'phone_path':'A','status':'completed'}]
        b=baseline+[{'phone_path':'B','status':'paused_backup_retained'}]
        result=merge_receipts(baseline,[a,b])
        self.assertEqual([r['phone_path'] for r in result['records']],['old','A','B'])
        self.assertEqual(len(baseline),1)

    def test_parent_reconciles_completed_receipt_before_lane_assignment(self):
        transaction={'phone_path':'A','status':'complete_temporary_copies_removed','saved_bytes':10,
                     'phone_hash_verified':True,'media_index_updated':True,'original_backup_deleted':True,'compressed_pc_copy_deleted':True}
        old=[{'phone_path':'A','status':'pending_phone_transfer'}]
        repaired=reconcile_baseline(old,[transaction])
        self.assertEqual(repaired[-1]['status'],'completed')
        self.assertEqual(len(reconcile_baseline(repaired,[transaction])),2)
        self.assertEqual(old[0]['status'],'pending_phone_transfer')
        self.assertEqual(transaction['status'],'complete_temporary_copies_removed')
        self.assertEqual(reconcile_baseline([],[{**transaction,'media_index_updated':False}]),[])

    def test_surviving_lane_blocks_restart_before_state_can_be_seeded(self):
        @contextmanager
        def lock(path,**_):
            if str(path).replace('\\','/').endswith('parallel/amf/worker.lock'):
                raise TimeoutError('old lane still owns lease')
            yield
        with patch('parallel_compression_coordinator.file_mutex',lock),patch('parallel_compression_coordinator.run') as run:
            with self.assertRaises(TimeoutError):main()
            run.assert_not_called()


if __name__=='__main__':unittest.main()
