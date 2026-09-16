"""Phase 2 durable chat runtime ownership and recovery tests."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from chat_session import ConnectionSession
from tests.support.conversation_sessions import open_sessions
from session_runtime import (
    BudgetExhausted,
    RuntimeIdentity,
    SessionRuntimeRegistry,
    SessionRuntimeRepository,
)
from session_runtime.repository import chat_id_value


def _runtime(tmp_path, identity_factory=None):
    repository = SessionRuntimeRepository(str(tmp_path / "astb.sqlite3"))
    registry = SessionRuntimeRegistry(
        repository,
        identity_factory=identity_factory,
    )
    return registry, repository


@pytest.mark.asyncio
async def test_settings_fence_releases_after_cancellation(tmp_path):
    registry, _ = _runtime(tmp_path)
    entered = asyncio.Event()
    async def applying():
        with registry.change_settings("settings-chat"):
            entered.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(applying())
    await entered.wait()
    assert registry.try_reserve_run("settings-chat") is None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not registry.configuration_pending("settings-chat")
    admission = registry.try_reserve_run("settings-chat")
    assert admission
    registry.finish_run(admission, status="test-complete")


def test_chat_id_validation_has_one_repository_contract():
    assert chat_id_value("  chat-1  ") == "chat-1"
    with pytest.raises(ValueError, match="1-256"):
        chat_id_value("")
    with pytest.raises(ValueError, match="control characters"):
        chat_id_value("chat\ninvalid")


def test_parked_queue_survives_reload_and_requires_explicit_revisioned_action(tmp_path):
    registry, repository = _runtime(tmp_path)
    registry.ensure_runtime("queue-chat")
    first = registry.enqueue_input("queue-chat", "first", delivery="steer")
    second = registry.enqueue_input("queue-chat", "second", delivery="follow_up")
    before = registry.queue_snapshot("queue-chat")
    registry.park_queued_input_tickets("queue-chat", reason="explicit_user_cancel")
    parked = registry.queue_snapshot("queue-chat")
    assert parked["revision"] > before["revision"]
    assert [row["text"] for row in parked["items"]] == ["first", "second"]
    reloaded, _ = _runtime(tmp_path)
    assert reloaded.queue_snapshot("queue-chat") == parked
    assert reloaded.claim_input("queue-chat", "steer", run_id="new") is None
    assert reloaded.claim_input("queue-chat", "follow_up", run_id="new") is None
    with pytest.raises(RuntimeError, match="stale_queue_revision"):
        repository.queued_ticket_command("queue-chat", first.ticket_id,
            expected_revision=before["revision"], operation="remove")
    repository.queued_ticket_command("queue-chat", first.ticket_id,
        expected_revision=parked["revision"], operation="remove")
    assert repository.get_ticket(first.ticket_id).state == "cancelled"
    assert repository.get_ticket(second.ticket_id).state == "parked"


def test_failed_parking_is_retried_before_another_admission(tmp_path, monkeypatch):
    registry, repository = _runtime(tmp_path)
    registry.ensure_runtime("queue-chat")
    ticket = registry.enqueue_input("queue-chat", "retain me", delivery="follow_up")
    original = repository.park_tickets
    def fail(*args, **kwargs):
        raise OSError("fixture storage failure")
    monkeypatch.setattr(repository, "park_tickets", fail)
    with pytest.raises(OSError):
        registry.park_queued_input_tickets("queue-chat", reason="stop")
    with pytest.raises(OSError):
        registry.try_reserve_run("queue-chat")
    monkeypatch.setattr(repository, "park_tickets", original)
    assert registry.try_reserve_run("queue-chat")
    assert repository.get_ticket(ticket.ticket_id).state == "parked"


@pytest.mark.asyncio
async def test_automatic_retirement_fences_new_admissions_and_survives_waiter_cancel(tmp_path):
    registry, _ = _runtime(tmp_path)
    registry.ensure_runtime("retiring")
    with registry.claim_kernel_retirement("retiring") as claimed:
        assert claimed
        assert registry.try_reserve_run("retiring") is None
        first = asyncio.create_task(registry.reserve_run("retiring"))
        second = asyncio.create_task(registry.reserve_run("retiring"))
        await asyncio.sleep(0)
        assert not first.done() and not second.done()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not second.done()
    admission = await asyncio.wait_for(second, 1)
    assert admission
    with registry.claim_kernel_retirement("retiring") as claimed:
        assert not claimed  # Admission wins the reverse race.
    assert registry.automatic_kernel_eviction_blocked("retiring")
    registry.finish_run(admission, status="complete")
    with pytest.raises(RuntimeError, match="fixture"):
        with registry.claim_kernel_retirement("retiring") as claimed:
            assert claimed
            raise RuntimeError("fixture")
    assert await registry.reserve_run("retiring")  # Teardown failure releases the fence.


def test_profile_assignment_is_pinned_and_survives_registry_restart(tmp_path):
    calls = []

    def identity(chat_id, is_new):
        calls.append((chat_id, is_new))
        return RuntimeIdentity(
            action_surface="trusted-local.v1",
            provider_tool_schema_revision="schema-7",
            graph_revision="chat.ipython.v2" if is_new else "chat.native-tools.v1",
        )

    registry, repository = _runtime(tmp_path, identity)
    assigned = registry.ensure_runtime("chat-a", is_new=True)
    replay = registry.ensure_runtime("chat-a", is_new=False)
    restarted = SessionRuntimeRegistry(repository, identity_factory=identity)
    restored = restarted.ensure_runtime("chat-a", is_new=False)

    assert assigned.identity.action_surface == "trusted-local.v1"
    assert replay.identity == assigned.identity
    assert restored.identity == assigned.identity
    assert calls == [("chat-a", True)]


def test_runtime_owner_reconcile_retains_missing_until_explicit_purge(tmp_path):
    registry, repository = _runtime(tmp_path)
    registry.ensure_runtime("legacy-chat", is_new=False)
    registry.ensure_runtime("branch-chat", is_new=False)

    summary = registry.reconcile_runtime_owners(["branch-chat", "new-branch-chat"])

    assert summary == {
        "active_owners": 2,
        "created": 1,
        "retained_unowned": 1,
        "marked_for_purge": 0,
    }
    assert repository.get_runtime("legacy-chat").lifecycle_state == "active"
    assert repository.get_runtime("new-branch-chat").lifecycle_state == "active"

    purged = registry.reconcile_runtime_owners(
        ["branch-chat", "new-branch-chat"], purge_ids=["legacy-chat"]
    )
    assert purged["marked_for_purge"] == 1
    record = repository.get_runtime("legacy-chat")
    assert record.lifecycle_state == "deleting"
    assert record.deletion_saga_state == "explicit_owner_purge"

    with pytest.raises(ValueError, match="active and explicitly purged"):
        registry.reconcile_runtime_owners(["branch-chat"], ["branch-chat"])


@pytest.mark.asyncio
async def test_startup_never_resumes_ambiguous_runtime_deletion(tmp_path):
    registry, repository = _runtime(tmp_path)
    registry.ensure_runtime("active-branch", is_new=False)
    registry.ensure_runtime("old-unowned", is_new=False)
    repository.set_lifecycle(
        "old-unowned", "deleting", deletion_saga_state="legacy_ambiguous"
    )
    store = SimpleNamespace(
        list_sessions=lambda: [{"id": "active-branch"}],
        has_message_ticket=lambda _sid, _ticket: False,
    )

    summary = await registry.startup_reconcile(store)

    assert summary["deletions_deferred"] == 1
    assert repository.get_runtime("old-unowned").lifecycle_state == "deleting"


def test_mutation_authority_defaults_false_and_survives_restart(tmp_path):
    def identity(_chat_id, _is_new):
        return RuntimeIdentity(
            action_surface="trusted-local.v1",
            provider_tool_schema_revision="ipython.portable.v3",
            graph_revision="chat.ipython.v2",
        )

    registry, repository = _runtime(tmp_path, identity)
    initial = registry.ensure_runtime("chat-authority", is_new=True)

    assert initial.mutation_write_enabled is False
    assert initial.mutation_authority_revision == 0
    assert initial.mutation_authority_updated_at == 0
    assert initial.mutation_authority_actor == ""
    assert initial.to_dict()["schema_version"] == 3

    restarted = SessionRuntimeRegistry(repository, identity_factory=identity)
    assert restarted.runtime("chat-authority") == initial


def test_mutation_authority_cas_is_monotonic_and_idempotent(tmp_path):
    registry, repository = _runtime(
        tmp_path,
        lambda _chat_id, _is_new: RuntimeIdentity(
            action_surface="trusted-local.v1"
        ),
    )
    initial = registry.ensure_runtime("chat-authority", is_new=True)
    enabled = registry.set_mutation_write_enabled(
        "chat-authority", True, "chat_composer:user", expected_revision=0
    )

    assert enabled.identity == initial.identity
    assert enabled.mutation_write_enabled is True
    assert enabled.mutation_authority_revision == 1
    assert enabled.mutation_authority_updated_at > 0
    assert enabled.mutation_authority_actor == "chat_composer:user"
    assert enabled.version == initial.version + 1

    replay = registry.set_mutation_write_enabled(
        "chat-authority", True, "idempotent-replay", expected_revision=1
    )
    assert replay == enabled
    with pytest.raises(RuntimeError, match="authority CAS failed"):
        registry.set_mutation_write_enabled(
            "chat-authority", False, "stale-client", expected_revision=0
        )

    disabled = repository.mutation_authority_cas(
        "chat-authority",
        False,
        actor="chat_composer:user",
        expected_revision=1,
    )
    assert disabled.mutation_write_enabled is False
    assert disabled.mutation_authority_revision == 2
    assert disabled.mutation_authority_updated_at >= enabled.mutation_authority_updated_at
    assert disabled.mutation_authority_actor == "chat_composer:user"


def test_registry_blocks_mutation_authority_change_while_run_reserved(tmp_path):
    registry, _repository = _runtime(tmp_path)
    admission = registry.try_reserve_run("chat-authority")
    assert admission

    with pytest.raises(RuntimeError, match="during an active run"):
        registry.set_mutation_write_enabled(
            "chat-authority", True, "chat_composer:user"
        )
    assert registry.runtime("chat-authority").mutation_write_enabled is False

    registry.finish_run(admission, status="ok")
    enabled = registry.set_mutation_write_enabled(
        "chat-authority", True, "chat_composer:user"
    )
    assert enabled.mutation_write_enabled is True


def test_child_and_worker_mutation_authority_starts_and_stays_false(tmp_path):
    registry, repository = _runtime(tmp_path)
    identity = RuntimeIdentity(action_surface="trusted-local.v1")
    worker = registry.ensure_worker_runtime(
        "worker:automation:one", source="automation", identity=identity
    )
    child = repository.ensure_runtime(
        "childchat-one",
        identity,
        creation_saga_state="child:parent-chat",
    )

    assert worker.mutation_write_enabled is False
    assert child.mutation_write_enabled is False
    with pytest.raises(RuntimeError, match="child or worker"):
        registry.set_mutation_write_enabled(
            worker.chat_id, True, "invalid-worker-toggle"
        )
    with pytest.raises(RuntimeError, match="child or worker"):
        repository.mutation_authority_cas(
            child.chat_id, True, actor="invalid-child-toggle"
        )


def test_retired_action_surface_is_not_rewritten_or_authorized(tmp_path):
    database = tmp_path / "legacy-runtime.sqlite3"
    now = time.time()
    with sqlite3.connect(database) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_migration (
                version INTEGER PRIMARY KEY,
                applied_at REAL NOT NULL
            );
            INSERT INTO schema_migration(version, applied_at) VALUES (1, 1);
            CREATE TABLE astb_chat_runtime (
                chat_id TEXT PRIMARY KEY,
                lifecycle_state TEXT NOT NULL,
                action_surface TEXT NOT NULL,
                provider_tool_schema_revision TEXT NOT NULL DEFAULT '',
                graph_revision TEXT NOT NULL DEFAULT '',
                catalog_release_id TEXT NOT NULL DEFAULT '',
                environment_digest TEXT NOT NULL DEFAULT '',
                trust_profile TEXT NOT NULL DEFAULT '',
                kernel_generation INTEGER NOT NULL DEFAULT 0,
                disclosure_profile_id TEXT NOT NULL DEFAULT '',
                disclosure_profile_revision TEXT NOT NULL DEFAULT '',
                discovery_state_ref TEXT NOT NULL DEFAULT '',
                mount_revision INTEGER,
                selected_category_id TEXT NOT NULL DEFAULT '',
                overlay_revision INTEGER NOT NULL DEFAULT 0,
                continuation_state TEXT NOT NULL DEFAULT 'ready',
                budget_limits_json TEXT NOT NULL DEFAULT '{}',
                budget_used_json TEXT NOT NULL DEFAULT '{}',
                creation_saga_state TEXT NOT NULL DEFAULT 'complete',
                deletion_saga_state TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                tombstoned_at REAL
            );
            """
        )
        rows = (
            ("mutable", "astb-mutable.trusted-local.v1", "complete"),
            ("static", "astb-static.trusted-local.v1", "complete"),
            ("native", "native-tools.v1", "complete"),
            ("child", "astb-mutable.trusted-local.v1", "child:parent"),
            ("worker", "astb-mutable.trusted-local.v1", "worker:automation"),
        )
        conn.executemany(
            "INSERT INTO astb_chat_runtime(chat_id, lifecycle_state, "
            "action_surface, creation_saga_state, created_at, updated_at) "
            "VALUES (?, 'active', ?, ?, ?, ?)",
            [(chat_id, surface, marker, now, now) for chat_id, surface, marker in rows],
        )

    repository = SessionRuntimeRepository(str(database))
    assert repository.get_runtime("mutable").mutation_write_enabled is False
    assert repository.get_runtime("static").mutation_write_enabled is False
    assert repository.get_runtime("native").mutation_write_enabled is False
    assert repository.get_runtime("child").mutation_write_enabled is False
    assert repository.get_runtime("worker").mutation_write_enabled is False
    assert repository.get_runtime("mutable").mutation_authority_revision == 0
    assert repository.get_runtime("mutable").mutation_authority_actor == ""
    assert repository.get_runtime("static").mutation_authority_actor == ""
    assert repository.get_runtime("mutable").identity.action_surface == (
        "astb-mutable.trusted-local.v1"
    )


def test_attachments_move_without_rebinding_other_windows(tmp_path):
    registry, _ = _runtime(tmp_path)
    first = ConnectionSession()
    second = ConnectionSession()

    registry.attach("chat-a", first.attachment_id, first)
    registry.attach("chat-a", second.attachment_id, second)
    registry.move_attachment(first.attachment_id, "chat-b")

    assert first.viewed_session_id == "chat-b"
    assert second.viewed_session_id == "chat-a"
    assert registry.snapshot("chat-a")["attachments"] == 1
    assert registry.snapshot("chat-b")["attachments"] == 1


@pytest.mark.asyncio
async def test_one_writer_admission_per_durable_chat(tmp_path):
    registry, _ = _runtime(tmp_path)
    first = registry.try_reserve_run("chat-a", attachment_id="window-a")
    second = registry.try_reserve_run("chat-a", attachment_id="window-b")

    assert first
    assert second is None
    assert registry.is_busy("chat-a") is True
    assert registry.writer_lock("chat-a") is registry.writer_lock("chat-a")

    registry.finish_run(first, status="ok")
    third = registry.try_reserve_run("chat-a", attachment_id="window-b")
    assert third


def test_terminal_fence_blocks_new_writers_and_active_input_until_finish(tmp_path):
    registry, repository = _runtime(tmp_path)
    admission = registry.try_reserve_run(
        "chat-fence", attachment_id="window-a"
    )
    assert admission

    assert registry.begin_run_finalization(admission) is True
    assert registry.is_busy("chat-fence") is True
    assert registry.accepts_inputs("chat-fence") is False
    assert registry.try_reserve_run(
        "chat-fence", attachment_id="window-b"
    ) is None
    with pytest.raises(RuntimeError, match="finalizing"):
        registry.enqueue_input(
            "chat-fence", "too late", delivery="steer"
        )
    assert repository.list_tickets("chat-fence") == []

    registry.finish_run(admission, status="cancelled")
    assert registry.try_reserve_run(
        "chat-fence", attachment_id="window-b"
    )


@pytest.mark.asyncio
async def test_other_window_message_becomes_durable_steering(tmp_path):
    import ws_dispatch

    registry, repository = _runtime(tmp_path)
    store = open_sessions(tmp_path / "chats")
    store.bind_runtime_lifecycle(registry.ensure_runtime)
    sid = store.create_session()
    owner = ConnectionSession(viewed_session_id=sid)
    observer = ConnectionSession(viewed_session_id=sid)
    registry.attach(sid, owner.attachment_id, owner)
    registry.attach(sid, observer.attachment_id, observer)
    admission = registry.try_reserve_run(sid, attachment_id=owner.attachment_id)
    assert admission
    sent = []

    class Socket:
        async def send_json(self, payload):
            sent.append(payload)

    server = SimpleNamespace(
        require_runtime=lambda: SimpleNamespace(
            sessions=store,
            session_runtimes=registry,
        )
    )
    await ws_dispatch.HANDLERS["chat"](
        server,
        Socket(),
        observer,
        {"type": "chat", "text": "use the new constraint", "client_id": "window-b"},
    )

    tickets = repository.list_tickets(sid)
    assert len(tickets) == 1
    assert tickets[0].state == "queued"
    assert tickets[0].attachment_id == observer.attachment_id
    assert sent[0]["type"] == "chat:queued"
    assert registry.claim_input(sid, "steer", run_id="run-owner")["text"] == (
        "use the new constraint"
    )


@pytest.mark.asyncio
async def test_observer_disconnect_does_not_cancel_initiating_window(tmp_path):
    registry, _ = _runtime(tmp_path)
    owner = ConnectionSession()
    observer = ConnectionSession()
    registry.attach("chat-a", owner.attachment_id, owner)
    registry.attach("chat-a", observer.attachment_id, observer)
    admission = registry.try_reserve_run(
        "chat-a", attachment_id=owner.attachment_id
    )

    release = asyncio.Event()

    async def running():
        await release.wait()

    task = asyncio.create_task(running())
    registry.bind_admission_task(admission, task)

    assert registry.detach(observer.attachment_id) == []
    assert not task.cancelled()
    cancelled = registry.detach(owner.attachment_id)
    assert cancelled == [task]
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()


@pytest.mark.asyncio
async def test_detached_view_disconnect_preserves_unobserved_foreground_run(
    tmp_path,
):
    registry, _ = _runtime(tmp_path)
    owner = ConnectionSession(view_role="detached_chat")
    transport = SimpleNamespace(send_json=AsyncMock())
    registry.attach("chat-a", owner.attachment_id, owner, transport)
    admission = registry.try_reserve_run(
        "chat-a", attachment_id=owner.attachment_id,
    )
    task = asyncio.create_task(asyncio.Event().wait())
    registry.bind_admission_task(admission, task)

    cancelled = registry.detach(
        owner.attachment_id, cancel_unobserved_foreground=False,
    )

    assert cancelled == []
    assert registry.attachment_count("chat-a") == 0
    assert registry.active_admission("chat-a") == admission
    assert registry.active_run_owner("chat-a") == (owner, None)
    assert not task.done()

    await registry.shutdown()
    assert task.cancelled()


@pytest.mark.asyncio
@pytest.mark.parametrize("observer_chat", ["chat-a", "chat-b"])
async def test_navigated_owner_disconnect_checks_admitted_chat_observers(
    tmp_path, observer_chat,
):
    registry, _ = _runtime(tmp_path)
    owner = ConnectionSession()
    observer = ConnectionSession()
    registry.attach("chat-a", owner.attachment_id, owner)
    registry.attach(observer_chat, observer.attachment_id, observer)
    admission = registry.try_reserve_run(
        "chat-a", attachment_id=owner.attachment_id
    )
    task = asyncio.create_task(asyncio.Event().wait())
    registry.bind_admission_task(admission, task)
    try:
        registry.move_attachment(owner.attachment_id, "chat-b")
        cancelled = registry.detach(owner.attachment_id)
        assert registry.admission_chat_id(admission) == "chat-a"
        if observer_chat == "chat-a":
            assert cancelled == []
            assert task.cancelling() == 0
            assert registry.attachment_count("chat-a") == 1
        else:
            assert cancelled == [task]
            assert task.cancelling() == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_observer_cancel_finalizes_the_owning_window(tmp_path):
    import ws_dispatch

    registry, _ = _runtime(tmp_path)
    store = open_sessions(tmp_path / "chats")
    store.bind_runtime_lifecycle(registry.ensure_runtime)
    sid = store.create_session()
    owner = ConnectionSession(viewed_session_id=sid)
    observer = ConnectionSession(viewed_session_id=sid)
    owner_socket = SimpleNamespace(send_json=AsyncMock())
    observer_socket = SimpleNamespace(send_json=AsyncMock())
    registry.attach(sid, owner.attachment_id, owner, owner_socket)
    registry.attach(sid, observer.attachment_id, observer, observer_socket)
    admission = registry.try_reserve_run(
        sid, attachment_id=owner.attachment_id
    )
    owner.reserve_turn()
    owner.active.runtime_chat_id = sid
    owner.active.runtime_admission_id = admission
    owner.active.turn_session_id = sid
    owner.active.turn_display_user_text = "run the long task"
    owner.active.turn_display_attachments = []
    runtimes_ticket = registry.enqueue_input(
        sid,
        "durable queued input",
        delivery="steer",
        ticket_id="ticket-observer-cancel",
    )

    async def running():
        await asyncio.Event().wait()

    turn = asyncio.create_task(running())
    owner.active.turn_task = turn
    registry.bind_admission_task(admission, turn)
    host = SimpleNamespace(
        require_runtime=lambda: SimpleNamespace(
            sessions=store,
            session_runtimes=registry,
        ),
        hub=SimpleNamespace(broadcast=AsyncMock()),
    )

    await ws_dispatch.HANDLERS["cancel"](
        host, observer_socket, observer, {"type": "cancel"}
    )

    observer_payloads = [
        call.args[0] for call in observer_socket.send_json.await_args_list
    ]
    owner_payloads = [call.args[0] for call in owner_socket.send_json.await_args_list]
    assert observer_payloads[0]["type"] == "cancelling"
    assert any(row.get("type") == "done" and row.get("cancelled")
               for row in owner_payloads)
    assert turn.cancelled()
    assert owner.busy is False
    assert owner.active.turn_session_id is None
    messages = store.get_session(sid)["messages"]
    assert [row["text"] for row in messages] == [
        "run the long task", "Task stopped.",
    ]
    settle_events = [
        call.args[0]
        for call in host.hub.broadcast.await_args_list
        if call.args and call.args[0].get("type") == "chat:queue_snapshot"
    ]
    assert settle_events == [registry.queue_snapshot(sid)]
    assert registry.repository.get_ticket(runtimes_ticket.ticket_id).state == "parked"
    assert registry.active_admission(sid) == ""


def test_ticket_ids_are_idempotent_and_conflicts_fail(tmp_path):
    registry, repository = _runtime(tmp_path)
    registry.ensure_runtime("chat-a")
    first = registry.enqueue_input(
        "chat-a", "continue", delivery="steer", ticket_id="ticket-fixed"
    )
    replay = registry.enqueue_input(
        "chat-a", "continue", delivery="steer", ticket_id="ticket-fixed"
    )

    assert replay == first
    with pytest.raises(ValueError, match="conflicts"):
        registry.enqueue_input(
            "chat-a", "different", delivery="steer", ticket_id="ticket-fixed"
        )
    assert len(repository.list_tickets("chat-a")) == 1


@pytest.mark.asyncio
async def test_mutation_change_rehydrates_every_window_attached_to_chat(
    tmp_path, monkeypatch,
):
    import ws_chat_sessions
    import ws_dispatch

    registry, _repository = _runtime(tmp_path)
    store = open_sessions(tmp_path / "chats")
    store.bind_runtime_lifecycle(registry.ensure_runtime)
    sid = store.create_session()
    requester = ConnectionSession(viewed_session_id=sid)
    observer = ConnectionSession(viewed_session_id=sid)
    requester_socket = SimpleNamespace(send_json=AsyncMock())
    observer_socket = SimpleNamespace(send_json=AsyncMock())
    registry.attach(sid, requester.attachment_id, requester, requester_socket)
    registry.attach(sid, observer.attachment_id, observer, observer_socket)
    status = {
        "enabled": True,
        "effective_enabled": False,
        "authority_revision": 4,
        "available": True,
        "locked": False,
        "reason": "temporarily frozen",
    }
    authority = SimpleNamespace(
        set_mutation=lambda *_args, **_kwargs: {
            "ok": True,
            "mutation_enabled": True,
            "authority_revision": 4,
        },
        mutation_toggle_status=lambda *_args, **_kwargs: dict(status),
    )
    host = SimpleNamespace(
        require_runtime=lambda: SimpleNamespace(
            sessions=store,
            session_runtimes=registry,
            kernel=None,
            session_control=None,
            catalog=SimpleNamespace(mutation_authority=authority),
        ),
        router=SimpleNamespace(),
    )
    monkeypatch.setattr(
        ws_chat_sessions,
        "_mutation_route",
        lambda _srv, _sid: {
            "provider": "local", "model": "test", "adapter": "test",
        },
    )

    await ws_dispatch.HANDLERS["chat:runtime:mutation:set"](
        host,
        requester_socket,
        requester,
        {
            "type": "chat:runtime:mutation:set",
            "id": sid,
            "enabled": True,
            "request_id": "mutation-request",
            "expected_revision": 3,
        },
    )

    requester_events = [
        call.args[0] for call in requester_socket.send_json.await_args_list
    ]
    observer_events = [
        call.args[0] for call in observer_socket.send_json.await_args_list
    ]
    done = next(
        row for row in requester_events
        if row.get("type") == "chat:runtime:mutation:set:done"
    )
    assert done["enabled"] is True
    assert done["effective_enabled"] is False
    assert done["authority_revision"] == 4
    for events in (requester_events, observer_events):
        snapshot = next(row for row in events if row.get("type") == "chat:session")
        assert snapshot["session"]["id"] == sid
        assert snapshot["session"]["runtime"]["mutation_enabled"] is True
        assert snapshot["session"]["runtime"]["mutation_effective_enabled"] is False


def test_ticket_lifecycle_and_transcript_proof(tmp_path):
    registry, repository = _runtime(tmp_path)
    store = open_sessions(tmp_path / "chats")
    sid = store.create_session()
    registry.ensure_runtime(sid)
    ticket = registry.enqueue_input(sid, "steer here", delivery="steer")
    row = registry.claim_input(sid, "steer", run_id="run-1")
    session = ConnectionSession(viewed_session_id=sid)

    registry.record_input_delivery(sid, session, row, "prior answer")
    registry.begin_transcript_commit(session.active.delivered_inputs)
    store.append_messages(sid, [{
        "role": "user",
        "text": row["text"],
        "ticket_id": row["id"],
    }])
    registry.complete_transcript_commit(sid, session.active.delivered_inputs)

    assert store.has_message_ticket(sid, ticket.ticket_id)
    terminal = repository.get_ticket(ticket.ticket_id)
    assert terminal.state == "completed"
    assert terminal.proof["ticket_id"] == ticket.ticket_id


def test_transcript_append_if_absent_is_idempotent(tmp_path):
    store = open_sessions(tmp_path / "chats")
    sid = store.create_session()

    _, first = store.append_if_absent(
        sid, "ticket-once", {"role": "user", "text": "only once"}
    )
    _, replay = store.append_if_absent(
        sid, "ticket-once", {"role": "user", "text": "only once"}
    )

    assert first is True
    assert replay is False
    assert [row["text"] for row in store.get_session(sid)["messages"]] == ["only once"]


@pytest.mark.asyncio
async def test_startup_reconciliation_uses_transcript_proof(tmp_path):
    registry, repository = _runtime(tmp_path)
    store = open_sessions(tmp_path / "chats")
    sid = store.create_session()
    registry.ensure_runtime(sid)
    committed = registry.enqueue_input(sid, "committed", delivery="steer")
    pending = registry.enqueue_input(sid, "pending", delivery="steer")
    leftover = registry.enqueue_input(sid, "leftover", delivery="steer")
    repository.claim_ticket(sid, "steer", run_id="old-run")
    repository.claim_ticket(sid, "steer", run_id="old-run")
    store.append_messages(sid, [{
        "role": "user", "text": "committed", "ticket_id": committed.ticket_id,
    }])

    summary = await registry.startup_reconcile(store)

    assert summary["tickets_completed"] == 1
    assert summary["tickets_requeued"] == 2
    assert repository.get_ticket(committed.ticket_id).state == "completed"
    assert repository.get_ticket(pending.ticket_id).state == "resume_queued"
    assert repository.get_ticket(leftover.ticket_id).state == "resume_queued"
    assert registry.claim_input(sid, "steer", run_id="fresh-run") is None
    assert registry.promote_recovered_inputs(sid) == 2
    assert registry.claim_input(sid, "steer", run_id="resume-run")["text"] == "pending"
    assert registry.claim_input(sid, "steer", run_id="resume-run")["text"] == "leftover"


def test_budget_exhaustion_pauses_instead_of_reporting_success(tmp_path):
    registry, _ = _runtime(tmp_path)
    registry.ensure_runtime("chat-a")
    registry.set_budget("chat-a", {"provider_calls": 2})
    registry.record_run_usage("chat-a", "run-1", {
        "llm_calls": 1, "total_tokens": 10, "wall_time_s": 1,
    })
    ready = registry.runtime("chat-a")
    assert ready.continuation_state == "ready"

    paused = registry.record_run_usage("chat-a", "run-2", {
        "llm_calls": 1, "total_tokens": 10, "wall_time_s": 1,
    })
    assert paused.continuation_state == "paused_budget_exhausted"
    with pytest.raises(BudgetExhausted):
        registry.try_reserve_run("chat-a")


@pytest.mark.asyncio
async def test_delete_saga_drains_threads_and_rebinds_attachments(tmp_path, monkeypatch):
    registry, repository = _runtime(tmp_path)
    store = open_sessions(tmp_path / "chats")
    store.bind_runtime_lifecycle(registry.ensure_runtime)
    doomed = store.create_session("Doomed")
    fallback = store.create_session("Fallback")
    store.set_active(doomed)
    registry.ensure_runtime(doomed)
    repository.link_thread(doomed, "thread-1", source="chat")
    session = ConnectionSession()
    registry.attach(doomed, session.attachment_id, session)
    deleted_threads = []

    class FakeSnapshotStore:
        def __init__(self, _path=None):
            pass

        async def delete_thread(self, thread_id):
            deleted_threads.append(thread_id)

    import agent_engine.sqlite_snapshot_store as snapshots

    monkeypatch.setattr(snapshots, "SQLiteRunSnapshotStore", FakeSnapshotStore)

    selected = await registry.delete_chat(doomed, store)

    assert selected == fallback
    assert deleted_threads == ["thread-1"]
    assert repository.get_runtime(doomed).lifecycle_state == "deleted"
    assert store.get_session(doomed) is None
    assert session.viewed_session_id == fallback
    assert registry.snapshot(fallback)["attachments"] == 1


@pytest.mark.asyncio
async def test_delete_commits_conversation_tombstone_before_runtime_cleanup(
    tmp_path,
):
    registry, repository = _runtime(tmp_path)
    store = open_sessions(tmp_path / "chats")
    store.bind_runtime_lifecycle(registry.ensure_runtime)
    doomed = store.create_session("Doomed")

    def fail_cleanup(_chat_id):
        raise OSError("runtime cleanup unavailable")

    registry.register_chat_cleanup(fail_cleanup)

    with pytest.raises(OSError, match="runtime cleanup unavailable"):
        await registry.delete_chat(doomed, store)

    assert store.get_session(doomed) is None
    record = repository.get_runtime(doomed)
    assert record.lifecycle_state == "deleting"
    assert record.deletion_saga_state == "deleting_transcript"


@pytest.mark.asyncio
async def test_recoverable_tombstone_stops_live_owners_before_hiding_chat(tmp_path):
    registry, _repository = _runtime(tmp_path)
    store = open_sessions(tmp_path / "chats")
    store.bind_runtime_lifecycle(registry.ensure_runtime)
    doomed = store.create_session("Doomed")
    fallback = store.create_session("Fallback")
    store.set_active(doomed)
    observed: list[str] = []

    async def stop_owner(chat_id: str) -> None:
        assert store.get_session(chat_id) is not None
        observed.append(chat_id)

    registry.register_chat_tombstone_cleanup(stop_owner)

    selected = await registry.tombstone_chat_owner(doomed, store)

    assert selected == fallback
    assert observed == [doomed]
    assert store.get_session(doomed) is None
    assert registry.runtime(doomed).continuation_state == "owner_tombstoned"
