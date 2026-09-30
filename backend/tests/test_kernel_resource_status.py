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
