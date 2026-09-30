"""Read-only resource observations for an owned persistent Python generation."""

from __future__ import annotations

import copy
import time
from typing import Any, Protocol

from .capsules import KernelCapsuleLimits
from .contracts import KernelLimits


class WorkerProcess(Protocol):
    pid: int
    def poll(self) -> int | None: ...


class ProcessOwner(Protocol):
    def contains_pid(self, pid: int) -> bool: ...
    def member_pids(self) -> list[int]: ...


class ResourceLease(Protocol):
    process: WorkerProcess | None
    job: ProcessOwner | None
    worker_pid: int | None
    _resource_snapshot: dict[str, Any]
    _last_output_pressure: dict[str, Any]


def resource_status(lease: ResourceLease, limits: KernelLimits,
                    capsule_limits: KernelCapsuleLimits) -> dict[str, Any]:
    snapshot = copy.deepcopy(lease._resource_snapshot or {})
    process = snapshot.get("process") if isinstance(snapshot.get("process"), dict) else {}
    source = "worker_snapshot" if process else "unavailable"
    sampled_at = snapshot.get("received_at")
    tree = {"rss_bytes": None, "processes": None, "complete": False}
    if lease.process is not None and lease.process.poll() is None:
        try:
            import psutil

            # A Windows venv Popen PID can be a waiting launcher. The
            # authenticated readiness frame names the actual interpreter.
            pid = getattr(lease, "worker_pid", None) or int(lease.process.pid)
            job = getattr(lease, "job", None)
            if job is not None and not job.contains_pid(pid):
                raise ProcessLookupError("interpreter no longer belongs to this generation")
            live = psutil.Process(pid)
            memory, cpu = live.memory_info(), live.cpu_times()
            process.update(pid=pid, rss_bytes=int(memory.rss), virtual_bytes=int(memory.vms),
                           cpu_user_s=float(cpu.user), cpu_system_s=float(cpu.system),
                           threads=int(live.num_threads()))
            source = "live_interpreter" if getattr(lease, "worker_pid", None) else "live_launcher"
            sampled_at = time.time()
            if job is not None:
                member_pids = set(job.member_pids())
            else:
                member_pids = {pid, *(child.pid for child in live.children(recursive=True)), int(lease.process.pid)}
            total, count, complete = 0, 0, job is not None
            for member_pid in member_pids:
                try:
                    if job is not None and not job.contains_pid(member_pid):
                        complete = False
                        continue
                    total += int(psutil.Process(member_pid).memory_info().rss)
                    count += 1
                except psutil.Error:
                    complete = False
            if job is not None and set(job.member_pids()) != member_pids:
                complete = False
            tree = {"rss_bytes": total, "processes": count, "complete": complete}
        except Exception:
            # Failed observation never changes the generation's lifecycle.
            pass
    rss_value = process.get("rss_bytes")
    rss = max(0, int(rss_value)) if rss_value is not None else None
    configured_limit = int(limits.process_memory_bytes)
    memory_limit = configured_limit if configured_limit > 0 else None
    namespace = snapshot.get("namespace") if isinstance(snapshot.get("namespace"), dict) else {
        "values": 0, "estimated_bytes": 0, "contributors": [], "contributors_truncated": False}
    namespace_bytes = max(0, int(namespace.get("estimated_bytes") or 0))
    capsule_limit = max(1, int(capsule_limits.max_total_value_bytes))
    return {
        "schema": "variant1.kernel-resource-status.v1", "process": process,
        "namespace": namespace, "tree": tree,
        "measurement_source": source, "sampled_at": sampled_at,
        "output": dict(lease._last_output_pressure),
        "pressure": {
            "process_memory_ratio": round(min(1.0, rss / memory_limit), 6)
            if memory_limit is not None and rss is not None else None,
            "process_memory_limit_bytes": memory_limit,
            "capsule_estimate_ratio": round(min(1.0, namespace_bytes / capsule_limit), 6),
            "capsule_limit_bytes": capsule_limit,
        },
        "snapshot_received_at": snapshot.get("received_at"),
    }
