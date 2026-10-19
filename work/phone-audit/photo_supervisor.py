"""Photo child lifecycle owned by the existing local dashboard supervisor."""
from pathlib import Path
import subprocess
import os
import sys
from project_config import local_config
from photo_transaction import persist
from shared_json import read_json


class PhotoSupervisor:
    def __init__(self,root,job_factory):
        self.root=Path(root);self.out=self.root/'outputs/phone-photo-compression-batches'
        self.job_factory=job_factory;self.process=None;self.job=None;self.log=None
        self.final_state='stopped';self.error=None

    def alive(self):return self.process is not None and self.process.poll() is None

    def start(self):
        self.tick()
        if not (self.out/'bulk-authorization.json').exists():return
        if self.alive():
            persist(self.out/'control.json',{'command':'run'});return
        status=read_json(self.out/'status.json',default={})
        if status.get('state')=='complete':self.final_state='complete';return
        persist(self.out/'control.json',{'command':'run'})
        interpreter=Path(local_config().get('photo_python') or sys.executable.replace('pythonw.exe','python.exe'))
        self.log=(self.out/'worker.log').open('ab',buffering=0)
        self.job=self.job_factory()
        self.process=subprocess.Popen([str(interpreter),str(self.root/'work/phone-audit/compress_phone_photos.py')],cwd=self.root,stdout=self.log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        self.job.assign(self.process);self.final_state='running';self.error=None

    def stop(self,command):
        if self.out.exists():persist(self.out/'control.json',{'command':command})
        if not self.alive():self.final_state='paused' if command=='pause' else 'stopped'

    def tick(self):
        if self.process is None or self.process.poll() is None:return
        code=self.process.returncode
        status=read_json(self.out/'status.json',default={})
        self.final_state=status.get('state','attention_required')
        if code!=0 or self.final_state not in ('complete','paused','stopped'):
            self.final_state='attention_required';self.error=status.get('error') or f'Photo worker exited: {code}'
        if self.job:self.job.close();self.job=None
        if self.log:self.log.close();self.log=None
        self.process=None

    def snapshot(self):
        return {'photo_worker_pid':self.process.pid if self.alive() else None,'photo_state':'running' if self.alive() else self.final_state,'photo_error':self.error}
