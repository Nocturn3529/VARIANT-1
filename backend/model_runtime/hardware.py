"""
VARIANT-1 hardware detection — Phase 1, Step 6 (Layer 3, spec 3.3)

Detects the GPU (NVIDIA via nvidia-smi) and total system RAM, then assigns the
machine to one of three tiers with the first-run message shown to the user.
Result is persisted to llm_config.json (hardware{}) so it only runs once.

``telemetry()`` is the live per-device contract (hardware:telemetry over WS):
every NVIDIA GPU with used/free VRAM, utilization, temperature, and power,
plus system RAM total/available and VARIANT-1's own memory share. Unlike
``detect_hardware()`` it is re-queried on demand, never persisted.
"""

import os
import json
import platform
import shutil
import subprocess
import threading
import time

TIER_MESSAGES = {
    1: "Your device is great for running me locally - fast and private.",
    2: "Your device can run me locally, just a bit slower for harder tasks. "
       "I recommend adding a cloud API key for complex requests.",
    3: "For the best experience on your device, I will use cloud mode. "
       "Add an API key in Settings to get started.",
}

_SAMPLE_LOCK = threading.Lock()
_PREVIOUS_SAMPLE = None


def _total_ram_mb() -> int:
    try:
        if platform.system() == "Windows":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(stat)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return int(stat.ullTotalPhys / (1024 * 1024))
        else:
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if line.startswith("MemTotal"):
                        return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0


def _nvidia_gpu():
    """Return (name, vram_mb) for the first NVIDIA GPU, or None."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.check_output(
            [exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            text=True, timeout=10, stderr=subprocess.DEVNULL,
        )
        line = out.strip().splitlines()[0]
        name, vram = line.split(",")
        return name.strip(), int(float(vram.strip()))
    except Exception:
        return None


def _avail_ram_mb() -> int:
    try:
        if platform.system() == "Windows":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(stat)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return int(stat.ullAvailPhys / (1024 * 1024))
        else:
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if line.startswith("MemAvailable"):
                        return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0


def _nvidia_devices() -> list:
    """One dict per NVIDIA GPU with live memory/utilization/thermal/power
    numbers, [] when nvidia-smi is missing. Fields that a GPU/driver doesn't
    report come back as None rather than fake zeros."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    fields = ("index,name,memory.total,memory.used,memory.free,"
              "utilization.gpu,temperature.gpu,power.draw,power.limit")
    try:
        out = subprocess.check_output(
            [exe, f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
            text=True, timeout=10, stderr=subprocess.DEVNULL,
        )
    except Exception:
        return []

    def _num(raw, cast):
        raw = raw.strip()
        try:
            return cast(float(raw))
        except (TypeError, ValueError):
            return None   # "[N/A]" and friends

    devices = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 9:
            continue
        devices.append({
            "index": _num(parts[0], int) or 0,
            "name": parts[1],
            "vram_total_mb": _num(parts[2], int),
            "vram_used_mb": _num(parts[3], int),
            "vram_free_mb": _num(parts[4], int),
            "utilization_pct": _num(parts[5], int),
            "temperature_c": _num(parts[6], int),
            "power_draw_w": _num(parts[7], float),
            "power_limit_w": _num(parts[8], float),
        })
    return devices


def _process_tree_rss_mb() -> int:
    """VARIANT-1's resource share: this process plus children (llama-server etc.).
    psutil when present; otherwise just our own RSS via ctypes on Windows."""
    try:
        import psutil
        me = psutil.Process(os.getpid())
        total = me.memory_info().rss
        for child in me.children(recursive=True):
            try:
                total += child.memory_info().rss
            except Exception:
                pass
        return int(total / (1024 * 1024))
    except Exception:
        pass
    try:
        if platform.system() == "Windows":
            import ctypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(counters)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(
                    handle, ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize / (1024 * 1024))
    except Exception:
        pass
    return 0


def _variant1_process_snapshot() -> dict:
    """Resource counters for the complete VARIANT-1 application tree.

    Electron passes its PID in ``VARIANT1_UI_PID``. Starting from that root
    includes the Main Deck renderers, this backend, and llama-server. Direct
    backend/test launches fall back to this process and its children.
    """
    try:
        import psutil
        root_pid = int(os.environ.get("VARIANT1_UI_PID", "0") or 0) or os.getpid()
        try:
            root = psutil.Process(root_pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            root = psutil.Process(os.getpid())
        processes = [root, *root.children(recursive=True)]
        rss = 0
        cpu_seconds = 0.0
        io_bytes = 0
        pids = set()
        details = []
        for process in processes:
            try:
                pids.add(process.pid)
                process_rss = process.memory_info().rss
                rss += process_rss
                cpu = process.cpu_times()
                cpu_seconds += float(cpu.user) + float(cpu.system)
                io = process.io_counters()
                io_bytes += int(io.read_bytes) + int(io.write_bytes)
                details.append({"pid": process.pid, "name": process.name(),
                                "rss_mb": round(process_rss / (1024 * 1024), 1)})
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        return {
            "pids": pids,
            "rss_mb": int(rss / (1024 * 1024)),
            "cpu_seconds": cpu_seconds,
            "io_bytes": io_bytes,
            "process_count": len(details),
            "processes": sorted(details, key=lambda item: item["rss_mb"], reverse=True),
        }
    except Exception:
        return {"pids": {os.getpid()}, "rss_mb": _process_tree_rss_mb(),
                "cpu_seconds": 0.0, "io_bytes": 0, "process_count": 1,
                "processes": []}


def _runtime_backends() -> list:
    """Inference backends physically present in this installation."""
    try:
        from paths import APP_ROOT
        binary_dir = os.path.join(APP_ROOT, "bin")
    except Exception:
        binary_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin"))
    backends = ["cpu"]
    if os.path.isfile(os.path.join(binary_dir, "ggml-cuda.dll")):
        backends.insert(0, "cuda")
    if os.path.isfile(os.path.join(binary_dir, "ggml-vulkan.dll")):
        backends.insert(0, "vulkan")
    return backends


def _windows_display_adapters() -> list:
    """Cheap one-shot fallback for AMD/Intel adapters absent from nvidia-smi."""
    if platform.system() != "Windows":
        return []
    # The registry's 64-bit qwMemorySize avoids Win32_VideoController's common
    # 4-GiB AdapterRAM ceiling. CIM remains the fallback for unusual drivers.
    script = (
        "$rows=@(Get-ChildItem 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Video\\*\\0000' "
        "-ErrorAction SilentlyContinue | ForEach-Object {$p=Get-ItemProperty $_.PSPath; "
        "if($p.DriverDesc){[pscustomobject]@{Name=$p.DriverDesc;"
        "AdapterRAM=$p.'HardwareInformation.qwMemorySize';DriverVersion=$p.DriverVersion}}});"
        "if($rows.Count -eq 0){$rows=@(Get-CimInstance Win32_VideoController | "
        "Select-Object Name,AdapterRAM,DriverVersion)};$rows | ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            text=True, timeout=8, stderr=subprocess.DEVNULL,
        ).strip()
        data = json.loads(out) if out else []
        rows = data if isinstance(data, list) else [data]
        result = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("Name"):
                continue
            result.append({
                "name": str(row["Name"]),
                "vram_mb": max(0, int(row.get("AdapterRAM") or 0) // (1024 * 1024)),
                "driver": str(row.get("DriverVersion") or ""),
            })
        return result
    except Exception:
        return []


def _nvidia_process_utilization(pids: set) -> dict:
    """Return GPU-index -> SM utilization for processes in VARIANT-1's tree."""
    exe = shutil.which("nvidia-smi")
    if not exe or not pids:
        return {}
    try:
        out = subprocess.check_output(
            [exe, "pmon", "-c", "1", "-s", "u"], text=True, timeout=5,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return {}
    utilization = {}
    for line in out.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            gpu_index = int(parts[0])
            pid = int(parts[1])
            sm = float(parts[3])
        except (TypeError, ValueError):
            continue
        if pid in pids:
            utilization[gpu_index] = utilization.get(gpu_index, 0.0) + sm
    return {index: min(100.0, value) for index, value in utilization.items()}


def _cpu_temperature_c():
    try:
        import psutil
        groups = psutil.sensors_temperatures() or {}
        for name in ("coretemp", "k10temp", "cpu_thermal", "acpitz"):
            readings = groups.get(name) or []
            values = [float(item.current) for item in readings
                      if getattr(item, "current", None) is not None]
            if values:
                return round(max(values), 1)
    except Exception:
        pass
    return None


def _cpu_name() -> str:
    if platform.system() == "Windows":
        try:
            import winreg
            path = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except Exception:
            pass
    return platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER") or "CPU"


def _sample_system_performance(process_snapshot: dict) -> dict:
    """Sample system totals and derive VARIANT-1 rates from counter deltas."""
    global _PREVIOUS_SAMPLE
    try:
        import psutil
        now = time.monotonic()
        cpu_total = float(psutil.cpu_percent(interval=None))
        logical = int(psutil.cpu_count(logical=True) or 1)
        physical = int(psutil.cpu_count(logical=False) or 0)
        frequency = psutil.cpu_freq()
        memory = psutil.virtual_memory()
        disk = psutil.disk_io_counters()
        current = {
            "time": now,
            "process_cpu": float(process_snapshot.get("cpu_seconds") or 0),
            "process_io": int(process_snapshot.get("io_bytes") or 0),
            "disk_read_bytes": int(getattr(disk, "read_bytes", 0) or 0),
            "disk_write_bytes": int(getattr(disk, "write_bytes", 0) or 0),
            "disk_read_count": int(getattr(disk, "read_count", 0) or 0),
            "disk_write_count": int(getattr(disk, "write_count", 0) or 0),
            "disk_read_time": int(getattr(disk, "read_time", 0) or 0),
            "disk_write_time": int(getattr(disk, "write_time", 0) or 0),
            "disk_busy_time": getattr(disk, "busy_time", None),
        }
        rates = {"variant1_cpu_pct": 0.0, "disk_utilization_pct": 0.0,
                 "variant1_disk_pct": 0.0, "disk_read_bps": 0.0,
                 "disk_write_bps": 0.0, "disk_response_ms": 0.0}
        with _SAMPLE_LOCK:
            previous = _PREVIOUS_SAMPLE
            _PREVIOUS_SAMPLE = current
        if previous:
            elapsed = max(0.001, now - previous["time"])
            process_cpu_delta = max(0.0, current["process_cpu"] - previous["process_cpu"])
            rates["variant1_cpu_pct"] = min(100.0, process_cpu_delta / elapsed / logical * 100.0)
            read_delta = max(0, current["disk_read_bytes"] - previous["disk_read_bytes"])
            write_delta = max(0, current["disk_write_bytes"] - previous["disk_write_bytes"])
            system_io_delta = read_delta + write_delta
            process_io_delta = max(0, current["process_io"] - previous["process_io"])
            rates["disk_read_bps"] = read_delta / elapsed
            rates["disk_write_bps"] = write_delta / elapsed
            if current["disk_busy_time"] is not None and previous["disk_busy_time"] is not None:
                busy_delta = max(0, current["disk_busy_time"] - previous["disk_busy_time"])
            else:
                # Windows does not expose busy_time through every psutil build;
                # read/write service time is the measured fallback.
                busy_delta = max(
                    max(0, current["disk_read_time"] - previous["disk_read_time"]),
                    max(0, current["disk_write_time"] - previous["disk_write_time"]),
                )
            active = min(100.0, busy_delta / (elapsed * 1000.0) * 100.0)
            rates["disk_utilization_pct"] = active
            if system_io_delta:
                rates["variant1_disk_pct"] = min(active, active * process_io_delta / system_io_delta)
            operation_delta = max(0, current["disk_read_count"] - previous["disk_read_count"]) + \
                max(0, current["disk_write_count"] - previous["disk_write_count"])
            service_delta = max(0, current["disk_read_time"] - previous["disk_read_time"]) + \
                max(0, current["disk_write_time"] - previous["disk_write_time"])
            if operation_delta:
                rates["disk_response_ms"] = service_delta / operation_delta
        total_mb = int(memory.total / (1024 * 1024))
        used_mb = int(memory.used / (1024 * 1024))
        available_mb = int(memory.available / (1024 * 1024))
        rss_mb = int(process_snapshot.get("rss_mb") or 0)
        cpu_name = _cpu_name()
        root = os.path.splitdrive(os.path.abspath(os.sep))[0] or os.path.abspath(os.sep)
        return {
            "cpu": {
                "name": cpu_name,
                "utilization_pct": round(cpu_total, 2),
                "variant1_utilization_pct": round(rates["variant1_cpu_pct"], 2),
                "logical_processors": logical,
                "physical_cores": physical,
                "speed_mhz": round(float(frequency.current), 1) if frequency else None,
                "max_speed_mhz": round(float(frequency.max), 1) if frequency else None,
                "temperature_c": _cpu_temperature_c(),
                "power_draw_w": None,
            },
            "memory": {
                "total_mb": total_mb,
                "used_mb": used_mb,
                "available_mb": available_mb,
                "utilization_pct": round(float(memory.percent), 2),
                "variant1_used_mb": rss_mb,
                "variant1_utilization_pct": round(rss_mb / total_mb * 100.0, 2) if total_mb else 0.0,
            },
            "disk": {
                "name": f"Disk ({root})" if root != "/" else "Disk (/)" ,
                "utilization_pct": round(rates["disk_utilization_pct"], 2),
                "variant1_utilization_pct": round(rates["variant1_disk_pct"], 2),
                "read_bytes_per_second": round(rates["disk_read_bps"], 1),
                "write_bytes_per_second": round(rates["disk_write_bps"], 1),
                "response_time_ms": round(rates["disk_response_ms"], 3),
            },
        }
    except Exception:
        return {
            "cpu": {},
            "memory": {
                "total_mb": _total_ram_mb(), "available_mb": _avail_ram_mb(),
                "variant1_used_mb": int(process_snapshot.get("rss_mb") or 0),
            },
            "disk": {},
        }


def telemetry() -> dict:
    """Live per-device snapshot (the hardware:telemetry contract)."""
    process_snapshot = _variant1_process_snapshot()
    performance = _sample_system_performance(process_snapshot)
    gpus = _nvidia_devices()
    gpu_process = _nvidia_process_utilization(process_snapshot["pids"])
    for gpu in gpus:
        gpu["variant1_utilization_pct"] = round(
            min(float(gpu.get("utilization_pct") or 0),
                float(gpu_process.get(gpu.get("index"), 0.0))), 2)
    return {
        **performance,
        "gpus": gpus,
        # Compact compatibility fields retained for Overview telemetry.
        "ram_total_mb": performance["memory"].get("total_mb", 0),
        "ram_available_mb": performance["memory"].get("available_mb", 0),
        "variant1_rss_mb": performance["memory"].get("variant1_used_mb", 0),
        "variant1_process_count": int(process_snapshot.get("process_count") or 0),
        "variant1_processes": process_snapshot.get("processes") or [],
        "ts": time.time(),
    }


def detect_hardware() -> dict:
    ram = _total_ram_mb()
    gpu = _nvidia_gpu()
    gpu_name = gpu[0] if gpu else None
    vram = gpu[1] if gpu else 0
    adapters = _windows_display_adapters()
    if not gpu_name and adapters:
        best = max(adapters, key=lambda item: item.get("vram_mb") or 0)
        gpu_name = best.get("name")
        vram = int(best.get("vram_mb") or 0)
    backends = _runtime_backends()
    vendor = ("nvidia" if gpu_name and "nvidia" in gpu_name.lower()
              else "amd" if gpu_name and ("amd" in gpu_name.lower() or "radeon" in gpu_name.lower())
              else "intel" if gpu_name and "intel" in gpu_name.lower() else "unknown")
    recommended_backend = ("cuda" if vendor == "nvidia" and "cuda" in backends
                           else "vulkan" if vendor in {"amd", "intel"} and "vulkan" in backends
                           else "cpu")
    accelerator_supported = recommended_backend != "cpu"

    # Tiering: strong NVIDIA GPU -> 1; modest GPU or roomy RAM -> 2; else cloud-only -> 3.
    if accelerator_supported and vram >= 8000:
        tier = 1
    elif (accelerator_supported and vram >= 4000) or ram >= 16000:
        tier = 2
    else:
        tier = 3

    return {
        "tier": tier,
        "tier_message": TIER_MESSAGES[tier],
        "gpu": gpu_name,
        "vram_mb": vram,
        "ram_mb": ram,
        "gpu_vendor": vendor,
        "display_adapters": adapters,
        "available_backends": backends,
        "recommended_backend": recommended_backend,
        "accelerator_supported": accelerator_supported,
        "detected_at": time.time(),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(detect_hardware(), indent=2))
