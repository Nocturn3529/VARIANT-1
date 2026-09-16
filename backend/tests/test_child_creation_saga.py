"""Child creation must not leave unowned durable runtimes after failure or restart."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from pathlib import Path
import os
import sqlite3
import subprocess
import sys

import pytest

from artifacts.store import ContentAddressedArtifactStore
from server_lifespan import start_critical_services
from tests.test_child_sessions import _Host, _manager, _runtimes, _TEST_WORK_SERVICES


@pytest.fixture
async def child_stack(tmp_path, monkeypatch):
    host = _Host(tmp_path / "astb.sqlite3")
    manager = _manager(str(tmp_path / "astb.sqlite3"), host,
                       ContentAddressedArtifactStore(str(tmp_path / "artifacts")))
    monkeypatch.setattr(manager, "_enqueue", AsyncMock(return_value="held"))
    try:
        yield host, manager, _runtimes(host)
    finally:
        await host.require_runtime().work.shutdown()
        _TEST_WORK_SERVICES.remove(host.require_runtime().work)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["budget", "handle_insert"])
async def test_pre_handle_failure_retires_child_runtime(child_stack, monkeypatch, failure):
    host, manager, runtimes = child_stack
    runtimes.set_budget("parent", {"provider_calls": 10})
    if failure == "budget":
        def fail_budget(*args, **kwargs):
            raise RuntimeError("injected child budget failure")
        monkeypatch.setattr(runtimes.repository, "update_continuation", fail_budget)
        expected = RuntimeError
    else:
        with manager._connect() as conn:
            conn.execute("CREATE TRIGGER fail_child_insert BEFORE INSERT ON astb_child_handle "
                         "BEGIN SELECT RAISE(ABORT, 'injected child handle failure'); END")
        expected = sqlite3.IntegrityError
    with pytest.raises(expected, match="injected child"):
        await manager.spawn("parent", task="work", child_id="child_fixed",
                            child_chat_id="childchat_fixed")
    with manager._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM astb_child_handle").fetchone()[0] == 0
    record = runtimes.repository.get_runtime("childchat_fixed")
    assert record is None
    manager._enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_reaps_crash_orphan_but_keeps_committed_child(child_stack, monkeypatch):
    host, manager, runtimes = child_stack
    parent = runtimes.ensure_runtime("parent")
    kept = await manager.spawn("parent", task="keep this child")
    runtimes.repository.ensure_runtime("childchat_crash_orphan", parent.identity,
                                       creation_saga_state="child:parent")
    runtime = host.require_runtime()
    runtime.catalog = SimpleNamespace(children=manager)
    runtime.lifecycle = SimpleNamespace(dev_reset_on_launch=lambda: None)
    runtime.sessions = object()
    monkeypatch.setattr(runtimes, "startup_reconcile", AsyncMock(return_value={
        "chats": 1, "tickets_requeued": 0, "tickets_completed": 0,
    }), raising=False)
    real_start = runtime.work.start
    async def start_after_cleanup():
        assert runtimes.repository.get_runtime("childchat_crash_orphan") is None
        assert runtimes.repository.get_runtime(kept["child_chat_id"]).lifecycle_state == "active"
        await real_start()
    monkeypatch.setattr(runtime.work, "start", start_after_cleanup)
    await start_critical_services(host)
    retried = await manager.spawn("parent", task="retry interrupted birth",
                                  child_id="child_retry", child_chat_id="childchat_crash_orphan")
    assert retried["child_chat_id"] == "childchat_crash_orphan"
    assert runtimes.repository.get_runtime("childchat_crash_orphan").lifecycle_state == "active"


@pytest.mark.asyncio
async def test_spawn_refuses_existing_runtime_owned_by_different_parent(child_stack):
    host, manager, runtimes = child_stack
    parent = runtimes.ensure_runtime("parent")
    runtimes.repository.ensure_runtime("childchat_foreign", parent.identity,
                                       creation_saga_state="child:other-parent")
    with pytest.raises(RuntimeError, match="own|parent|identity"):
        await manager.spawn("parent", task="work", child_id="child_fixed",
                            child_chat_id="childchat_foreign")
    foreign = runtimes.repository.get_runtime("childchat_foreign")
    assert foreign.creation_saga_state == "child:other-parent"
    assert foreign.lifecycle_state == "active"
    manager._enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_orphan_cleanup_failure_is_visible_and_retryable(child_stack, monkeypatch):
    host, manager, runtimes = child_stack
    parent = runtimes.ensure_runtime("parent")
    runtimes.repository.ensure_runtime("childchat_orphan", parent.identity,
                                       creation_saga_state="child:parent")
    original_delete = runtimes.delete_child_runtime
    monkeypatch.setattr(runtimes, "delete_child_runtime",
                        AsyncMock(side_effect=RuntimeError("injected cleanup failure")))
    with pytest.raises(RuntimeError, match="cleanup is pending.*childchat_orphan"):
        await manager.reconcile_runtime_sagas()
    record = runtimes.repository.get_runtime("childchat_orphan")
    assert record.lifecycle_state == "deleting"
    assert record.deletion_saga_state == "orphan_child_creation"
    monkeypatch.setattr(runtimes, "delete_child_runtime", original_delete)
    assert (await manager.reconcile_runtime_sagas())["cleaned"] == 1
    assert runtimes.repository.get_runtime("childchat_orphan") is None


@pytest.mark.asyncio
async def test_reconciliation_refuses_conflicting_handle_parent(child_stack):
    host, manager, runtimes = child_stack
    child = await manager.spawn("parent", task="preserve")
    with manager._connect() as conn:
        conn.execute("UPDATE astb_chat_runtime SET creation_saga_state='child:other' WHERE chat_id=?",
                     (child["child_chat_id"],))
    with pytest.raises(RuntimeError, match="parent ownership mismatch"):
        await manager.reconcile_runtime_sagas()
    assert runtimes.repository.get_runtime(child["child_chat_id"]).lifecycle_state == "active"


@pytest.mark.asyncio
async def test_used_orphan_keeps_its_tombstone_history(child_stack):
    host, manager, runtimes = child_stack
    parent = runtimes.ensure_runtime("parent")
    runtimes.repository.ensure_runtime("childchat_used", parent.identity,
                                       creation_saga_state="child:parent")
    runtimes.repository.advance_kernel_generation("childchat_used")
    assert (await manager.reconcile_runtime_sagas())["cleaned"] == 1
    record = runtimes.repository.get_runtime("childchat_used")
    assert record.lifecycle_state == "deleted" and record.kernel_generation == 1


@pytest.mark.asyncio
async def test_explicitly_deleted_unstarted_child_keeps_its_tombstone(child_stack):
    host, manager, runtimes = child_stack
    child = await manager.spawn("parent", task="explicitly delete")
    await manager.delete_chat("parent")
    record = runtimes.repository.get_runtime(child["child_chat_id"])
    assert record.lifecycle_state == "deleted" and record.kernel_generation == 0
    assert record.deletion_saga_state == "complete"
    await manager.reconcile_runtime_sagas()
    retained = runtimes.repository.get_runtime(child["child_chat_id"])
    assert retained is not None and retained.lifecycle_state == "deleted"
    with pytest.raises(RuntimeError, match="not available"):
        await manager.spawn("parent", task="explicitly delete", child_id=child["child_id"],
                            child_chat_id=child["child_chat_id"])


@pytest.mark.asyncio
async def test_cleanup_origin_survives_failure_before_unused_id_discard(child_stack, monkeypatch):
    host, manager, runtimes = child_stack
    parent = runtimes.ensure_runtime("parent")
    runtimes.repository.ensure_runtime("childchat_orphan", parent.identity,
                                       creation_saga_state="child:parent")
    discard = runtimes.repository.discard_unstarted_child_runtime
    def fail_discard(*args, **kwargs):
        raise RuntimeError("injected discard failure")
    monkeypatch.setattr(runtimes.repository, "discard_unstarted_child_runtime", fail_discard)
    with pytest.raises(RuntimeError, match="cleanup is pending"):
        await manager.reconcile_runtime_sagas()
    record = runtimes.repository.get_runtime("childchat_orphan")
    assert record.lifecycle_state == "deleted"
    assert record.deletion_saga_state == "orphan_child_creation:complete"
    monkeypatch.setattr(runtimes.repository, "discard_unstarted_child_runtime", discard)
    await manager.reconcile_runtime_sagas()
    assert runtimes.repository.get_runtime("childchat_orphan") is None


CRASH_DURING_BIRTH = """
import asyncio, os, sys
from pathlib import Path
from artifacts.store import ContentAddressedArtifactStore
from tests.test_child_sessions import _Host, _manager
root = Path(sys.argv[1])
database = root / 'astb.sqlite3'
host = _Host(database)
manager = _manager(str(database), host, ContentAddressedArtifactStore(str(root / 'artifacts')))
with manager._connect() as conn:
    conn.execute('CREATE TRIGGER crash_birth BEFORE INSERT ON astb_child_handle BEGIN SELECT crash_now(); END')
original_connect = manager._connect
def connection():
    conn = original_connect()
    conn.create_function('crash_now', 0, lambda: os._exit(73))
    return conn
manager._connect = connection
asyncio.run(manager.spawn('parent', task='crash before handle commit',
                         child_id='child_crash', child_chat_id='childchat_crash'))
"""


def test_process_death_rolls_back_the_entire_child_birth(tmp_path):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", CRASH_DURING_BIRTH, str(tmp_path)],
        env=environment, capture_output=True, text=True, timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 73, (result.stdout, result.stderr)
    with sqlite3.connect(tmp_path / "astb.sqlite3") as conn:
        assert conn.execute("SELECT COUNT(*) FROM astb_child_handle").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM astb_chat_runtime WHERE chat_id='childchat_crash'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM astb_chat_runtime WHERE chat_id='parent'").fetchone()[0] == 1
