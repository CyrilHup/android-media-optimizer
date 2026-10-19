"""Local-only dashboard and supervised, independently running compression worker."""
from pathlib import Path
from datetime import datetime,timezone
from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler
import argparse
import functools
import json
import msvcrt
import os
import secrets
import subprocess
import sys
import threading
import time
import ctypes
from ctypes import wintypes
from urllib.parse import urlsplit
from photo_supervisor import PhotoSupervisor
from network_recovery import retry_video_network_failure,fresh_worker_status
from device import phone_transport
from concurrent.futures import ThreadPoolExecutor

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'outputs/phone-video-compression-batches'
if sys.stdout is None:sys.stdout=(OUT/'service-output.log').open('a',encoding='utf-8',buffering=1)
if sys.stderr is None:sys.stderr=(OUT/'service-error.log').open('a',encoding='utf-8',buffering=1)
WORKER=Path(__file__).with_name('compress_phone_video_batches.py')
parser=argparse.ArgumentParser()
parser.add_argument('--port',type=int,default=8878)
parser.add_argument('--no-autostart',action='store_true')
options=parser.parse_args()
OUT.mkdir(parents=True,exist_ok=True)
dashboard_template=ROOT/'web/progress.html'
if dashboard_template.exists():
    import shutil
    shutil.copyfile(dashboard_template,OUT/'progress.html')
lock=(OUT/f'service-{options.port}.lock').open('a+b')
lock.seek(0);lock.write(b'1');lock.flush();lock.seek(0)
msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
token=secrets.token_urlsafe(32)
token_file=OUT/f'service-{options.port}.token'
token_file.write_text(token,encoding='ascii')
state={'state':'stopped','worker_pid':None,'service_pid':os.getpid(),'project_root':str(ROOT),'service_started_utc':datetime.now(timezone.utc).isoformat(),'error':None,'port':options.port}
mutex=threading.RLock()
process=None
output_stream=None
job=None
desired_run=False
connection_probe=None
next_probe=0
probe_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='phone-reconnect')

class Job:
    """Windows closes the entire worker/FFmpeg tree if the supervisor crashes."""
    def __init__(self):
        class Basic(ctypes.Structure):
            _fields_=[('process_time',ctypes.c_int64),('job_time',ctypes.c_int64),('flags',wintypes.DWORD),('min_working',ctypes.c_size_t),('max_working',ctypes.c_size_t),('active_processes',wintypes.DWORD),('affinity',ctypes.c_size_t),('priority',wintypes.DWORD),('scheduling',wintypes.DWORD)]
        class Io(ctypes.Structure):
            _fields_=[(name,ctypes.c_uint64) for name in ('read_count','write_count','other_count','read_bytes','write_bytes','other_bytes')]
        class Extended(ctypes.Structure):
            _fields_=[('basic',Basic),('io',Io),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),('peak_process_memory',ctypes.c_size_t),('peak_job_memory',ctypes.c_size_t)]
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype=wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
        self.kernel.TerminateJobObject.argtypes=[wintypes.HANDLE,wintypes.UINT]
        self.kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        self.handle=self.kernel.CreateJobObjectW(None,None)
        if not self.handle: raise ctypes.WinError(ctypes.get_last_error())
        settings=Extended();settings.basic.flags=0x2000  # KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle,9,ctypes.byref(settings),ctypes.sizeof(settings)):
            raise ctypes.WinError(ctypes.get_last_error())
    def assign(self,child):
        if not self.kernel.AssignProcessToJobObject(self.handle,wintypes.HANDLE(int(child._handle))):
            child.terminate();child.wait(timeout=10)
            raise ctypes.WinError(ctypes.get_last_error())
    def close(self):
        if self.handle:
            self.kernel.TerminateJobObject(self.handle,1)
            self.kernel.CloseHandle(self.handle)
            self.handle=None

def read_json(path,default):
    try: return json.loads(path.read_text(encoding='utf-8'))
    except (OSError,json.JSONDecodeError): return default

def save(path,value):
    temp=path.with_suffix('.service-new.json')
    temp.write_text(json.dumps(value,indent=2),encoding='utf-8')
    for _ in range(50):
        try:
            temp.replace(path)
            return
        except PermissionError: time.sleep(.1)
    raise RuntimeError('Unable to persist '+str(path))

def persist():
    state['updated_utc']=datetime.now(timezone.utc).isoformat()
    save(OUT/'service-status.json',state)

photos=PhotoSupervisor(ROOT,Job)

def combined_state():
    result={**state,**photos.snapshot(),'automatic_reconnect':True,'desired_run':desired_run}
    if photos.error:
        result.update(state='attention_required',error=photos.error)
    elif photos.alive() and result['state'] in ('complete','stopped','paused'):
        result['state']='running'
    return result

def start_worker():
    global process,output_stream,job,desired_run
    with mutex:
        desired_run=True
        photos.start()
        if process and process.poll() is None: return 'Le traitement est déjà en cours.'
        save(OUT/'control.json',{'command':'run'})
        state.update(state='starting',error=None,worker_started_utc=datetime.now(timezone.utc).isoformat())
        persist()
        output_stream=(OUT/'worker.log').open('ab',buffering=0)
        flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
        job=Job()
        process=subprocess.Popen([sys.executable.replace('pythonw.exe','python.exe'),str(WORKER)],cwd=ROOT,stdout=output_stream,stderr=subprocess.STDOUT,creationflags=flags)
        job.assign(process)
        state.update(state='running',worker_pid=process.pid)
        persist()
        return 'Traitement lancé. La reprise repart des reçus enregistrés.'

def request_stop(command):
    global desired_run
    with mutex:
        desired_run=False
        photos.stop(command)
        save(OUT/'control.json',{'command':command})
        if (process and process.poll() is None) or photos.alive():
            state['state']='stopping'
            message='Demande enregistrée. Arrêt au prochain point sûr ; un remplacement déjà commencé sera vérifié jusqu’au bout.'
        else:
            state['state']='paused' if command=='pause' else 'stopped'
            message='Traitement en pause.' if command=='pause' else 'Traitement arrêté.'
        persist()
        return message

def monitor():
    global process,output_stream,job,connection_probe,next_probe
    while True:
        time.sleep(1)
        with mutex:
            photos.tick()
            if desired_run and not process and state['state']=='waiting_for_phone':
                if connection_probe is None and time.monotonic()>=next_probe:
                    connection_probe=probe_executor.submit(phone_transport,ROOT/'work/android-tools/platform-tools/adb.exe')
                elif connection_probe is not None and connection_probe.done():
                    future=connection_probe;connection_probe=None;next_probe=time.monotonic()+15
                    try:future.result()
                    except Exception as error:
                        state['connection_error']=str(error);persist()
                    else:
                        state['connection_error']=None
                        start_worker()
            if state['state']=='stopping' and not process and not photos.alive():
                state['state']='paused' if read_json(OUT/'control.json',{}).get('command')=='pause' else 'stopped'
                persist()
            if not process: continue
            code=process.poll()
            if code is None: continue
            if job: job.close();job=None
            worker=read_json(OUT/'worker-status.json',{})
            status=worker.get('status')
            if code==0 and status in ('paused','stopped','complete'):
                state['state']=status
                state['error']=None
            else:
                state['state']='attention_required'
                fresh=fresh_worker_status(worker,state.get('worker_started_utc',''))
                lines=(OUT/'worker.log').read_text(encoding='utf-8',errors='replace').strip().splitlines()
                detail=(worker.get('error') if fresh else None) or (lines[-1] if lines else f'code {code}')
                state['error']=f'Traitement arrêté : {detail}. Les sauvegardes disponibles sont conservées.'
                recovery_status=worker if fresh else {'error':lines[-1] if lines else None}
                if desired_run and retry_video_network_failure(recovery_status):
                    state.update(state='waiting_for_phone',error=None,connection_error=detail)
                    next_probe=time.monotonic()+5
            state.update(worker_pid=None,last_exit_code=code)
            process=None
            if output_stream:
                output_stream.close();output_stream=None
            persist()

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,directory=str(ROOT/'outputs'),**kwargs)
    def log_message(self,*args):
        pass
    def end_headers(self):
        self.send_header('Cache-Control','no-store')
        super().end_headers()
    def response(self,code,data):
        body=json.dumps(data,ensure_ascii=False).encode('utf-8')
        self.send_response(code);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def valid_host(self):
        return self.headers.get('Host') in {f'127.0.0.1:{options.port}',f'localhost:{options.port}'}
    def do_GET(self):
        if not self.valid_host():
            self.response(403,{'error':'Accès local uniquement.'});return
        if urlsplit(self.path).path=='/api/status':
            with mutex: self.response(200,combined_state())
        else: super().do_GET()
    def do_POST(self):
        if not self.valid_host():
            self.response(403,{'error':'Accès local uniquement.'});return
        origin=self.headers.get('Origin')
        allowed={f'http://127.0.0.1:{options.port}',f'http://localhost:{options.port}'}
        if origin not in allowed and not (origin is None and self.headers.get('X-Phone-Compression-Control')==token):
            self.response(403,{'error':'Commande locale non autorisée.'});return
        route=urlsplit(self.path).path
        try:
            if route=='/api/resume': message=start_worker()
            elif route=='/api/pause': message=request_stop('pause')
            elif route=='/api/stop': message=request_stop('stop')
            elif route=='/api/shutdown':
                if (process and process.poll() is None) or photos.alive():
                    self.response(409,{'error':'Arrêtez d’abord le traitement et attendez le point sûr.'});return
                self.response(200,{'message':'Programme fermé.'})
                threading.Thread(target=self.server.shutdown,daemon=True).start();return
            else: self.response(404,{'error':'Commande inconnue.'});return
            self.response(200,{'message':message,'state':state['state']})
        except Exception as error:
            self.response(500,{'error':str(error)})

server=ThreadingHTTPServer(('127.0.0.1',options.port),Handler)
# Start ADB under the detached supervisor, outside each short-lived worker job.
subprocess.run([str(ROOT/'work/android-tools/platform-tools/adb.exe'),'start-server'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
threading.Thread(target=monitor,daemon=True).start()
persist()
if not options.no_autostart: start_worker()
try:
    server.serve_forever(poll_interval=.5)
finally:
    server.server_close()
    probe_executor.shutdown(wait=False,cancel_futures=True)
    # Controlled shutdown is allowed only once the child has stopped. A crash
    # leaves the persisted worker lock/journal to prevent concurrent replacement.
    token_file.unlink(missing_ok=True)
