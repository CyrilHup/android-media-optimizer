"""Windows shared snapshots and isolated progress regression; no phone actions."""
import ast
from datetime import datetime,timezone
import io
import json
import multiprocessing
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import shared_json


def replace_from_child(path,attempting):
    target=Path(path)
    temporary=target.with_suffix('.new')
    temporary.write_text('{"version":1}',encoding='utf-8')
    attempting.set()
    with shared_json.journal_io(target,timeout=5):
        temporary.replace(target)


class SharedJSONTests(unittest.TestCase):
    def test_journal_reader_serializes_another_process_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'transactions.json'
            target.write_text('{"version":0}',encoding='utf-8')
            context=multiprocessing.get_context('spawn')
            attempting=context.Event()
            with shared_json.journal_io(target,timeout=5),shared_json._open_shared(target) as previous:
                child=context.Process(target=replace_from_child,args=(str(target),attempting))
                child.start()
                self.assertTrue(attempting.wait(5))
                child.join(.1)
                self.assertTrue(child.is_alive(),'writer must wait until the reader closes')
                self.assertEqual(json.load(previous),{'version':0})
            child.join(10)
            if child.is_alive():child.terminate();child.join();self.fail('replacement did not resume after reader released lock')
            self.assertEqual(child.exitcode,0)
            self.assertEqual(shared_json.read_json(target),{'version':1})

    def test_permission_error_is_retried(self):
        with patch.object(shared_json,'_open_shared',side_effect=[PermissionError('busy'),PermissionError('busy'),io.StringIO('{"ok":true}')]) as opened,patch.object(shared_json.time,'sleep'):
            self.assertEqual(shared_json.read_json('fake',attempts=3),{'ok':True})
            self.assertEqual(opened.call_count,3)

    def test_strict_read_never_substitutes_empty_data_on_blocked_or_corrupt_file(self):
        with patch.object(shared_json,'_open_shared',side_effect=PermissionError('busy')),patch.object(shared_json.time,'sleep'):
            with self.assertRaises(PermissionError):shared_json.read_json('fake',default={},attempts=2)
        with patch.object(shared_json,'_open_shared',side_effect=lambda _:io.StringIO('broken')),patch.object(shared_json.time,'sleep'):
            with self.assertRaises(json.JSONDecodeError):shared_json.read_json('fake',default={},attempts=2)

    def test_only_progress_may_use_last_valid_snapshot(self):
        source=Path(__file__).with_name('compress_phone_video_batches.py')
        tree=ast.parse(source.read_text(encoding='utf-8'))
        definition=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='update')
        snapshot={'transactions':[{'phone_path':'A','phone_hash_verified':True,'saved_bytes':5}]}
        namespace={'time':time,'datetime':datetime,'timezone':timezone,'json':json,'state':{'phase':'encoding'},'receipts':{'records':[]},
                   '_phase_started':0,'_last_journal_snapshot':snapshot,'GLOBAL_OUT':Path('unused'),'eta_tracker':None,
                   'transfer_record':None,'prefetch_row':None,'STATUS':Path('unused'),'write_json':lambda *_:None,
                   'read_json':lambda *_,**__:(_ for _ in ()).throw(PermissionError('busy'))}
        exec(compile(ast.Module(body=[definition],type_ignores=[]),str(source),'exec'),namespace)
        namespace['update']()
        self.assertEqual(namespace['state']['completed_files'],1)
        self.assertTrue(namespace['state']['journal_snapshot_stale'])
        namespace['_last_journal_snapshot']=None
        with self.assertRaises(PermissionError):namespace['update']()


if __name__=='__main__':unittest.main()
