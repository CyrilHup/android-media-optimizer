"""Start the local application outside Codex's temporary process job."""
from pathlib import Path
import ctypes
from ctypes import wintypes
import base64
import json
import subprocess
import sys
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'outputs/phone-video-compression-batches'
URL='http://127.0.0.1:8878/api/status'
try:
    with urllib.request.urlopen(URL,timeout=2) as response:
        existing=json.load(response)
    if existing.get('service_pid'):
        print(json.dumps({'already_running':True,**existing}));raise SystemExit(0)
except OSError:
    pass
binary=Path(sys.executable).with_name('pythonw.exe')
# WMI creates the user process under its Windows provider, independently of
# this command's temporary job. pythonw keeps the background app invisible.
command_line=subprocess.list2cmdline([str(binary),str(Path(__file__).with_name('phone_compression_service.py'))])
quote=lambda value:"'"+str(value).replace("'","''")+"'"
script="$ErrorActionPreference='Stop'; Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine="+quote(command_line)+";CurrentDirectory="+quote(ROOT)+"} | ConvertTo-Json -Compress"
created=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',base64.b64encode(script.encode('utf-16-le')).decode()],capture_output=True,check=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
result=json.loads(created.stdout)
if result['ReturnValue']!=0:raise RuntimeError('Windows background launch failed: '+str(result))
child_pid=result['ProcessId']
kernel=ctypes.WinDLL('kernel32',use_last_error=True)
kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
kernel.OpenProcess.restype=wintypes.HANDLE
kernel.CloseHandle.argtypes=[wintypes.HANDLE]
handle=kernel.OpenProcess(0x1000,False,child_pid)
if not handle:raise ctypes.WinError(ctypes.get_last_error())
kernel.IsProcessInJob.argtypes=[wintypes.HANDLE,wintypes.HANDLE,ctypes.POINTER(wintypes.BOOL)]
inside=wintypes.BOOL()
if not kernel.IsProcessInJob(handle,None,ctypes.byref(inside)):
    raise ctypes.WinError(ctypes.get_last_error())
kernel.CloseHandle(handle)
if inside.value:
    raise RuntimeError('Windows launched the service in a process job; independence not verified.')
for _ in range(40):
    try:
        with urllib.request.urlopen(URL,timeout=1) as response:status=json.load(response)
        if status.get('service_pid')==child_pid:
            receipt={'detached_from_temporary_job':True,'launch_method':'Windows Win32_Process.Create (WMI)','service_pid':child_pid,'service_status':status}
            (OUT/'detached-launch-verification.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
            print(json.dumps(receipt));break
    except OSError: pass
    time.sleep(.25)
else:raise RuntimeError('Service did not become available.')
