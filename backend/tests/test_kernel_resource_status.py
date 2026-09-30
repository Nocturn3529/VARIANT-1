"""Resource observations do not invent enforcement quotas or measurements."""

from types import SimpleNamespace

import pytest

from kernel_runtime.capsules import KernelCapsuleLimits
from kernel_runtime.contracts import KernelLimits
from kernel_runtime.manager import KernelRuntimeManager


def status(limit, process):
    manager = KernelRuntimeManager.__new__(KernelRuntimeManager)
    manager.limits = KernelLimits(process_memory_bytes=limit)
    manager.capsule_limits = KernelCapsuleLimits()
    lease = SimpleNamespace(process=None, _resource_snapshot={"process": process},
                            _last_output_pressure={})
    return manager._lease_resource_status(lease)


@pytest.mark.parametrize("limit", [0, -1])
def test_disabled_memory_quota_has_no_pressure_ratio(limit):
    result = status(limit, {"rss_bytes": 64 * 1024 * 1024})
    assert result["process"]["rss_bytes"] == 64 * 1024 * 1024
    assert result["pressure"]["process_memory_limit_bytes"] is None
    assert result["pressure"]["process_memory_ratio"] is None


@pytest.mark.parametrize("rss,ratio", [(0, 0), (50, 0.5), (100, 1), (200, 1)])
def test_configured_memory_quota_reports_bounded_pressure(rss, ratio):
    result = status(100, {"rss_bytes": rss})
    assert result["pressure"]["process_memory_limit_bytes"] == 100
    assert result["pressure"]["process_memory_ratio"] == ratio


def test_unmeasured_memory_is_not_reported_as_zero_pressure():
    result = status(100, {})
    assert "rss_bytes" not in result["process"]
    assert result["pressure"]["process_memory_ratio"] is None


def test_live_sample_uses_owned_interpreter_and_excludes_foreign_children(monkeypatch):
    import psutil
    from kernel_runtime.resources import resource_status

    class Process:
        def __init__(self, pid, rss):
            self.pid, self.rss = pid, rss
        def memory_info(self):
            return SimpleNamespace(rss=self.rss, vms=self.rss * 2)
        def cpu_times(self):
            return SimpleNamespace(user=2, system=1)
        def num_threads(self):
            return 3
        def children(self, recursive=True):
            return [processes[3], processes[99]]
    processes = {pid: Process(pid, rss) for pid, rss in [(1, 5), (2, 60), (3, 30), (4, 20), (99, 900)]}
    monkeypatch.setattr(psutil, "Process", lambda pid: processes[pid])
    lease = SimpleNamespace(process=SimpleNamespace(pid=1, poll=lambda: None), worker_pid=2,
        job=SimpleNamespace(contains_pid=lambda pid: pid in {1, 2, 3, 4}, member_pids=lambda: [1, 2, 3, 4, 99]),
        _resource_snapshot={}, _last_output_pressure={})
    result = resource_status(lease, KernelLimits(), KernelCapsuleLimits())
    assert result["process"]["pid"] == 2
    assert result["process"]["rss_bytes"] == 60
    # PID 4 is an owned, reparented sibling; PID 99 is a foreign/stale membership entry.
    assert result["tree"] == {"rss_bytes": 115, "processes": 4, "complete": False}
    assert result["measurement_source"] == "live_interpreter"


def test_unowned_pid_does_not_replace_worker_snapshot(monkeypatch):
    import psutil
    from kernel_runtime.resources import resource_status

    monkeypatch.setattr(psutil, "Process", lambda pid: pytest.fail("foreign PID sampled"))
    lease = SimpleNamespace(process=SimpleNamespace(pid=1, poll=lambda: None), worker_pid=2,
        job=SimpleNamespace(contains_pid=lambda pid: False),
        _resource_snapshot={"process": {"rss_bytes": 64}, "received_at": 123}, _last_output_pressure={})
    result = resource_status(lease, KernelLimits(), KernelCapsuleLimits())
    assert result["process"]["rss_bytes"] == 64
    assert result["measurement_source"] == "worker_snapshot"
    assert result["sampled_at"] == 123
