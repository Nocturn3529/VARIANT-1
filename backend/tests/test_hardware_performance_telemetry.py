import sys
from pathlib import Path
from types import SimpleNamespace


BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from model_runtime import hardware


def test_performance_sampler_derives_variant1_and_disk_rates(monkeypatch):
    disks = iter([
        SimpleNamespace(read_bytes=1_000_000, write_bytes=2_000_000,
                        read_count=100, write_count=200, read_time=1000,
                        write_time=500),
        SimpleNamespace(read_bytes=3_000_000, write_bytes=3_000_000,
                        read_count=200, write_count=250, read_time=1400,
                        write_time=600),
    ])
    fake_psutil = SimpleNamespace(
        cpu_percent=lambda interval=None: 40.0,
        cpu_count=lambda logical=True: 4 if logical else 2,
        cpu_freq=lambda: SimpleNamespace(current=3600.0, max=4200.0),
        virtual_memory=lambda: SimpleNamespace(
            total=16 * 1024**3, used=8 * 1024**3,
            available=8 * 1024**3, percent=50.0),
        disk_io_counters=lambda: next(disks),
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    times = iter([100.0, 102.0])
    monkeypatch.setattr(hardware.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(hardware, "_cpu_temperature_c", lambda: None)
    monkeypatch.setattr(hardware, "_cpu_name", lambda: "Test CPU")
    hardware._PREVIOUS_SAMPLE = None

    hardware._sample_system_performance({"rss_mb": 512, "cpu_seconds": 10.0,
                                         "io_bytes": 1_000, "pids": {1}})
    result = hardware._sample_system_performance(
        {"rss_mb": 640, "cpu_seconds": 10.6, "io_bytes": 5_000, "pids": {1}})

    assert result["cpu"]["name"] == "Test CPU"
    assert result["cpu"]["variant1_utilization_pct"] == 7.5
    assert result["memory"]["variant1_used_mb"] == 640
    assert result["memory"]["variant1_utilization_pct"] == 3.91
    assert result["disk"]["utilization_pct"] == 20.0
    assert result["disk"]["read_bytes_per_second"] == 1_000_000.0
    assert result["disk"]["write_bytes_per_second"] == 500_000.0
    assert result["disk"]["response_time_ms"] == 3.333


def test_telemetry_keeps_legacy_memory_fields_and_adds_gpu_share(monkeypatch):
    process = {"pids": {42}, "rss_mb": 768, "cpu_seconds": 0, "io_bytes": 0,
               "process_count": 4, "processes": [{"pid": 42, "name": "VARIANT-1", "rss_mb": 100}]}
    performance = {
        "cpu": {"utilization_pct": 20},
        "memory": {"total_mb": 16000, "available_mb": 6000,
                   "variant1_used_mb": 768},
        "disk": {"utilization_pct": 5},
    }
    monkeypatch.setattr(hardware, "_variant1_process_snapshot", lambda: process)
    monkeypatch.setattr(hardware, "_sample_system_performance", lambda snapshot: performance)
    monkeypatch.setattr(hardware, "_nvidia_devices", lambda: [
        {"index": 0, "utilization_pct": 35, "name": "Test GPU"}])
    monkeypatch.setattr(hardware, "_nvidia_process_utilization", lambda pids: {0: 12})

    result = hardware.telemetry()

    assert result["ram_total_mb"] == 16000
    assert result["ram_available_mb"] == 6000
    assert result["variant1_rss_mb"] == 768
    assert result["variant1_process_count"] == 4
    assert result["variant1_processes"][0]["name"] == "VARIANT-1"
    assert result["gpus"][0]["variant1_utilization_pct"] == 12.0
