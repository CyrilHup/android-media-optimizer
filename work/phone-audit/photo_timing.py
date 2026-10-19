"""Per-file wall-clock stages and separate ADB operation measurements."""
from contextlib import contextmanager
import time
import threading


class PhotoTiming:
    def __init__(self,clock=time.monotonic):
        self.clock=clock;self.started=clock();self.stages={};self.operations={};self.transaction_operations={};self.active=None;self.guard=threading.RLock()

    @contextmanager
    def stage(self,name):
        with self.guard:
            if self.active is not None:raise ValueError('Photo stages must not overlap')
            started=self.clock();self.active=(name,started)
        try:yield
        finally:
            with self.guard:self.stages[name]=self.stages.get(name,0)+self.clock()-started;self.active=None

    @contextmanager
    def operation(self,name):
        started=self.clock();ok=False
        with self.guard:transaction=bool(self.active and self.active[0]=='phone_transaction')
        try:yield;ok=True
        finally:
            with self.guard:
                row=self.operations.setdefault(name,{'calls':0,'seconds':0,'failures':0})
                row['calls']+=1;row['seconds']+=self.clock()-started;row['failures']+=not ok
                if transaction:
                    specific=self.transaction_operations.setdefault(name,{'calls':0,'seconds':0,'failures':0})
                    specific['calls']+=1;specific['seconds']+=self.clock()-started;specific['failures']+=not ok

    def snapshot(self):
      with self.guard:
        stages=dict(self.stages)
        if self.active:
            name,started=self.active;stages[name]=stages.get(name,0)+self.clock()-started
        return {'wall_seconds':round(self.clock()-self.started,3),
                'stages_seconds':{k:round(v,3) for k,v in stages.items()},
                'adb_operations':{k:{**v,'seconds':round(v['seconds'],3)} for k,v in self.operations.items()},
                'transaction_operations':{k:{**v,'seconds':round(v['seconds'],3)} for k,v in self.transaction_operations.items()},
                'active_stage':self.active[0] if self.active else None}
