"""Sample a disposable Windows desktop test and verify its entire process job exits.

Usage: python scripts/observe-native-test-windows.py --output NEW_DIR -- node scripts/test-chat-ownership-native.js --measure-peers
The independent hidden window answers WM_NULL without interacting with user windows.
"""
import argparse
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import shutil
import struct
import subprocess
import threading
import time
import uuid

import psutil
import win32api
import win32con
import win32gui
import win32job
import win32pdh


def job_pids(job):
    query = ctypes.windll.kernel32.QueryInformationJobObject
    buffer = ctypes.create_string_buffer(8 + 1024 * ctypes.sizeof(ctypes.c_size_t))
    if not query(wintypes.HANDLE(int(job)), 3, buffer, len(buffer), None):
        raise ctypes.WinError()
    count = struct.unpack_from('II', buffer)[1]
    return list((ctypes.c_size_t * count).from_buffer_copy(buffer, 8))


class WindowWitness:
    def __init__(self):
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self.pump, daemon=True)
        self.thread.start()
        if not self.ready.wait(5):
            raise RuntimeError('Windows message pump did not start')

    def pump(self):
        self.tid = win32api.GetCurrentThreadId()
        self.hwnd = win32gui.CreateWindow('STATIC', 'VARIANT-1 test observer', 0, 0, 0, 0, 0, -3, 0, 0, None)
        self.ready.set()
        try:
            win32gui.PumpMessages()
        finally:
            win32gui.DestroyWindow(self.hwnd)

    def sample(self):
        start = time.perf_counter()
        reply = ctypes.c_size_t()
        ok = ctypes.windll.user32.SendMessageTimeoutW(wintypes.HWND(self.hwnd), win32con.WM_NULL, 0, 0, 2, 500, ctypes.byref(reply))
        return {'at_ms': round(time.time()*1000), 'ms': round((time.perf_counter()-start)*1000, 3), 'answered': bool(ok)}

    def close(self):
        win32api.PostThreadMessage(self.tid, win32con.WM_QUIT, 0, 0)
        self.thread.join(2)


def observe(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    executable = shutil.which(command[0]) if command else None
    if not executable:
        raise ValueError('An executable test command is required')
    job = win32job.CreateJobObject(None, 'VARIANT1-test-'+uuid.uuid4().hex)
    limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    limits['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
    query = win32pdh.OpenQuery()
    counters = {}
    for name, counter in {'pages_in_per_s':r'\Memory\Pages Input/sec', 'commit_bytes':r'\Memory\Committed Bytes',
                          'disk_s_per_transfer':r'\PhysicalDisk(_Total)\Avg. Disk sec/Transfer'}.items():
        try:
            counters[name] = win32pdh.AddCounter(query, counter)
        except Exception:
            pass
    if counters:
        win32pdh.CollectQueryData(query)
    witness = WindowWitness()
    known = {}
    sample_file = output / 'samples.jsonl'
    identity = psutil.Process()
    result = {'observer': {'pid': identity.pid, 'birth': identity.create_time()}, 'host_logical_cpus':psutil.cpu_count(),
              'pdh_counters': list(counters), 'timed_out':False}
    proc = None
    assigned = False
    try:
        with (output/'test.log').open('xb') as log, sample_file.open('x', encoding='utf-8') as samples:
            proc = subprocess.Popen([executable,*command[1:]], cwd=args.cwd, stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW|0x4)
            root = psutil.Process(proc.pid)
            result['root'] = {'pid':proc.pid, 'birth':root.create_time(), 'name':root.name()}
            win32job.AssignProcessToJobObject(job, int(proc._handle))
            assigned = True
            root.resume()
            deadline = time.monotonic()+args.seconds
            sampled = 0
            psutil.cpu_percent()
            while proc.poll() is None:
                pulse = witness.sample()
                row = {'window_probe':pulse}
                metrics_due = time.monotonic()-sampled >= 0.9
                resources = []
                for pid in job_pids(job):
                    try:
                        process = psutil.Process(pid)
                        with process.oneshot():
                            birth = process.create_time()
                            if (pid,birth) not in known:
                                role = next((arg[7:] for arg in process.cmdline() if arg.startswith('--type=')), '')
                                known[pid,birth] = {'pid':pid, 'birth':birth, 'name':process.name(), 'parent':process.ppid(), 'type':role}
                            if metrics_due:
                                mem = process.memory_info()
                                io = process.io_counters()
                                cpu = process.cpu_times()
                                resources.append({'pid':pid, 'birth':birth, 'private_bytes':mem.private,
                                    'working_set':mem.rss, 'cpu_s':cpu.user+cpu.system,
                                    'read_bytes':io.read_bytes, 'write_bytes':io.write_bytes})
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                if metrics_due:
                    sampled = time.monotonic()
                    row.update({'processes':resources, 'host_cpu_percent':psutil.cpu_percent(), 'available_bytes':psutil.virtual_memory().available})
                    if counters:
                        try:
                            win32pdh.CollectQueryData(query)
                            for name, handle in counters.items():
                                row[name] = win32pdh.GetFormattedCounterValue(handle, win32pdh.PDH_FMT_DOUBLE)[1]
                        except Exception:
                            pass
                samples.write(json.dumps(row)+'\n')
                samples.flush()
                if time.monotonic() >= deadline:
                    result['timed_out'] = True
                    break
                time.sleep(.10)
    finally:
        if assigned:
            settle = time.monotonic()+2
            while job_pids(job) and time.monotonic()<settle and not result['timed_out']:
                time.sleep(.1)
            result['cleanup_pids'] = job_pids(job)
            if result['cleanup_pids']:
                win32job.TerminateJobObject(job, 1)
            settle = time.monotonic()+10
            while job_pids(job) and time.monotonic()<settle:
                time.sleep(.05)
            accounting = win32job.QueryInformationJobObject(job, win32job.JobObjectBasicAccountingInformation)
            result.update({'active_after_cleanup':accounting['ActiveProcesses'], 'total_processes':accounting['TotalProcesses']})
        elif proc and proc.poll() is None:
            proc.kill()
        if proc:
            result['exit_code'] = proc.wait(timeout=10)
        job.Close()
        witness.close()
        win32pdh.CloseQuery(query)
        result['observed_processes'] = list(known.values())
        (output/'ownership.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({k:result.get(k) for k in ('root','exit_code','total_processes','active_after_cleanup','timed_out')}, sort_keys=True))
    return 1 if result['timed_out'] or result.get('active_after_cleanup') else result['exit_code']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--cwd',type=Path,default=Path.cwd())
    parser.add_argument('--seconds',type=int,default=180)
    parser.add_argument('command',nargs=argparse.REMAINDER)
    raise SystemExit(observe(parser.parse_args()))
