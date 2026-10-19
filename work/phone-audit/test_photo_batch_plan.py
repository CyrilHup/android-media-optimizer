import hashlib
import unittest
from photo_batch_plan import build_photo_plan,require_pilot_approval


class PhotoPlanTests(unittest.TestCase):
    def row(self,name):return {'path':'/storage/emulated/0/Pictures/'+name,'bytes':1000,'mtime':1}

    def test_collisions_hidden_and_known_hdr_excluded(self):
        rows=[self.row(s) for s in ('a.jpg','a.png','b.jpg','b.webp','.secret/c.jpg','private/c.jpg','hdr.jpg','good.jpg')]
        plan=build_photo_plan(rows,{rows[-2]['path']})
        self.assertEqual([r['path'] for r in plan['records']],[rows[-1]['path']])
        self.assertEqual(len(plan['excluded']),4)

    def test_only_exact_approved_pilot_can_be_published(self):
        row={'phone_path':self.row('a.jpg')['path'],'target_phone_path':self.row('a.webp')['path']}
        payload=b'exact manifest'
        authorization={'approved':True,'scope':'publish_pilot_copies_only','manifest_sha256':hashlib.sha256(payload).hexdigest()}
        self.assertEqual(require_pilot_approval(authorization,payload,[row]),authorization['manifest_sha256'])
        for changed in ({**authorization,'approved':False},{**authorization,'scope':'bulk'},authorization):
            with self.assertRaises(PermissionError):
                require_pilot_approval(changed,b'changed' if changed is authorization else payload,[row])
        with self.assertRaises(PermissionError):require_pilot_approval(authorization,payload,[row]*5)

    def test_cannot_redirect_target_or_publish_hidden_photo(self):
        payload=b'manifest';authorization={'approved':True,'scope':'publish_pilot_copies_only','manifest_sha256':hashlib.sha256(payload).hexdigest()}
        for source,target in (('a.jpg','elsewhere.webp'),('.private/a.jpg','.private/a.webp')):
            row={'phone_path':self.row(source)['path'],'target_phone_path':self.row(target)['path']}
            with self.assertRaises(PermissionError):require_pilot_approval(authorization,payload,[row])
