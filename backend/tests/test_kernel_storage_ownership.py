import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from artifacts.store import ContentAddressedArtifactStore
from kernel_runtime.cell_ledger import KernelCellLedgerStore
from kernel_runtime.contracts import KernelUnavailable
from kernel_runtime.manager import KernelRuntimeManager
from kernel_runtime.ownership import owner_state, process_identity


def manager(tmp_path, monkeypatch, *, root=None, ledger=None, instance="owner"):
    monkeypatch.setattr("kernel_runtime.manager._restrict_path", lambda *a, **kw: None)
    return KernelRuntimeManager(
        root=str(root or tmp_path / "kernels"),
        cell_ledger_path=str(ledger or tmp_path / "ledger.sqlite3"),
        instance_id=instance, app_root=str(tmp_path), registry=None, broker=None,
        artifact_store=ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
    )


def admission(execution="pending", **extra):
    return dict(execution_id=execution, chat_id="chat", run_id="run", outer_tool_call_id="call",
                kernel_generation=1, workspace_revision=0, workspace_fingerprint="",
                workspace_root_ids=(), work_scope={}, source_ref="source", source_sha256="a" * 64,
                started_at=1.0, **extra)


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", ["root", "ledger"])
async def test_live_contender_cannot_sweep_or_reconcile(tmp_path, monkeypatch, shared):
    first = manager(tmp_path, monkeypatch)
    root = Path(first.root)
    generation = root / "chat" / "generation-live"
    generation.mkdir(parents=True)
    sentinel = generation / "sentinel"
    sentinel.write_text("retained")
    first.cell_ledger.admit(first.instance_id, admission(storage_owner=first._storage_owner.identity))
    other_root = root if shared == "root" else tmp_path / "different-scratch"
    other_ledger = first.cell_ledger.path if shared == "ledger" else tmp_path / "different-ledger.sqlite3"
    try:
        with pytest.raises(KernelUnavailable, match="already owned or inaccessible"):
            manager(tmp_path, monkeypatch, root=other_root, ledger=other_ledger, instance="contender")
        assert sentinel.read_text() == "retained"
        assert first.cell_ledger.latest("chat") is None
        assert len(first.cell_ledger.unsettled()) == 1
        if shared == "root":
            assert not Path(other_ledger).exists()
    finally:
        await first.shutdown()


@pytest.mark.asyncio
async def test_dead_owner_reclaimed_without_source_replay(tmp_path, monkeypatch):
    root, ledger = tmp_path / "kernels", tmp_path / "ledger.sqlite3"
    script = (
        "import os,json\nfrom pathlib import Path\n"
        "from kernel_runtime.ownership import KernelStorageOwnership\n"
        "from kernel_runtime.cell_ledger import KernelCellLedgerStore\n"
        f"owner=KernelStorageOwnership({str(root)!r},{str(ledger)!r},'dead-owner',restrict=lambda *a:None)\n"
        f"generation=Path({str(root)!r})/'chat'/'generation-dead';generation.mkdir(parents=True)\n"
        "(generation/'owner.json').write_text(json.dumps(owner.identity))\n"
        f"payload={admission()!r};payload['storage_owner']=owner.identity\n"
        f"KernelCellLedgerStore({str(ledger)!r}).admit('dead-owner',payload)\n"
        "os._exit(7)\n"
    )
    exited = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).parents[1],
                            timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert exited.returncode == 7
    second = manager(tmp_path, monkeypatch, root=root, ledger=ledger, instance="next-owner")
    try:
        result = second.cell_ledger.get("pending")
        assert result.status == "unknown_effect" and result.to_dict()["duration_ms"] is None
        assert not (root / "chat" / "generation-dead").exists()
        assert Path(str(root) + ".owner.lock").exists()
        second._recover_interrupted_cell_evidence()
        assert len(second.cell_ledger.list("chat")) == 1
    finally:
        await second.shutdown()


@pytest.mark.asyncio
async def test_live_external_owner_blocks_until_process_exit(tmp_path, monkeypatch):
    root, ledger, ready = tmp_path / "kernels", tmp_path / "ledger.sqlite3", tmp_path / "ready"
    script = ("import time\nfrom pathlib import Path\n"
              "from kernel_runtime.ownership import KernelStorageOwnership\n"
              f"owner=KernelStorageOwnership({str(root)!r},{str(ledger)!r},'external',restrict=lambda *a:None)\n"
              f"ready=Path({str(ready)!r})\n"
              "temporary=ready.with_suffix('.tmp')\n"
              "temporary.write_text('owned')\n"
              "temporary.replace(ready)\n"
              "while True: time.sleep(.1)\n")
    process = subprocess.Popen([sys.executable, "-c", script], cwd=Path(__file__).parents[1],
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            await asyncio.sleep(.02)
        assert ready.read_text() == "owned"
        with pytest.raises(KernelUnavailable):
            manager(tmp_path, monkeypatch, root=root, ledger=ledger)
    finally:
        process.terminate()
        process.wait(timeout=10)
    successor = manager(tmp_path, monkeypatch, root=root, ledger=ledger)
    await successor.shutdown()


@pytest.mark.asyncio
async def test_unattributed_legacy_data_and_live_pid_evidence_are_preserved(tmp_path, monkeypatch):
    root = tmp_path / "kernels"
    legacy = root / "chat" / "generation-legacy"
    legacy.mkdir(parents=True)
    (legacy / "sentinel").write_text("legacy")
    live = root / "chat" / "generation-live"
    live.mkdir()
    (live / "owner.json").write_text(json.dumps(process_identity("other-live-host")))
    ledger = KernelCellLedgerStore(str(tmp_path / "ledger.sqlite3"))
    ledger.admit("legacy-host", admission())
    ledger.admit("other-live-host", admission("live", storage_owner=process_identity("other-live-host")))
    current = manager(tmp_path, monkeypatch)
    try:
        assert legacy.exists() and live.exists()
        assert current.cell_ledger.latest("chat") is None
        assert len(current.cell_ledger.unsettled()) == 2
    finally:
        await current.shutdown()


def test_pid_reuse_and_unreadable_owner_are_distinct(monkeypatch):
    import psutil
    identity = process_identity("owner")
    monkeypatch.setattr(psutil, "Process", lambda _pid: SimpleNamespace(create_time=lambda: identity["created_at"] + 10))
    assert owner_state(identity) == "dead"
    monkeypatch.setattr(psutil, "Process", lambda _pid: (_ for _ in ()).throw(psutil.AccessDenied(_pid)))
    assert owner_state(identity) == "unknown"
    assert owner_state({"pid": os.getpid()}) == "unknown"


@pytest.mark.asyncio
async def test_failed_shutdown_keeps_fence_until_cleanup_succeeds(tmp_path, monkeypatch):
    current = manager(tmp_path, monkeypatch)
    current._leases["chat"] = SimpleNamespace(chat_id="chat")
    async def failed(*a, **kw):
        raise RuntimeError("owned worker still live")
    monkeypatch.setattr(current, "_close_lease_serialized", failed)
    assert not (await current.shutdown())["ok"]
    with pytest.raises(KernelUnavailable):
        manager(tmp_path, monkeypatch, instance="contender")
    async def settled(*a, **kw):
        return None
    monkeypatch.setattr(current, "_close_lease_serialized", settled)
    assert (await current.shutdown())["ok"]
    successor = manager(tmp_path, monkeypatch, instance="successor")
    await successor.shutdown()


@pytest.mark.asyncio
async def test_failed_constructor_releases_its_resource_fences(tmp_path, monkeypatch):
    original = KernelRuntimeManager._sweep_stale_roots
    monkeypatch.setattr(KernelRuntimeManager, "_sweep_stale_roots", lambda self: (_ for _ in ()).throw(RuntimeError("failed init")))
    with pytest.raises(RuntimeError, match="failed init"):
        manager(tmp_path, monkeypatch)
    monkeypatch.setattr(KernelRuntimeManager, "_sweep_stale_roots", original)
    current = manager(tmp_path, monkeypatch)
    await current.shutdown()


def test_ledger_upper_watermark_excludes_newer_cells(tmp_path):
    ledger = KernelCellLedgerStore(str(tmp_path / "ledger.sqlite3"))
    def append(index, chat="chat"):
        payload = admission(str(index))
        payload["chat_id"] = chat
        return ledger.append(**payload, result_ref="result", result_sha256="b" * 64,
                             status="ok", execution_count=index, completed_at=2,
                             duration_ms=1)
    first = append(1)
    boundary = append(2)
    append(3)
    append(4, "foreign")
    assert [r.execution_id for r in ledger.list("chat", through_sequence=boundary.sequence)] == ["1", "2"]
    assert [r.execution_id for r in ledger.list("chat", after_sequence=first.sequence,
                                               through_sequence=boundary.sequence)] == ["2"]
    assert [r.execution_id for r in ledger.list("chat")] == ["1", "2", "3"]
