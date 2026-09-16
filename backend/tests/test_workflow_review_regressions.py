"""Regression coverage for durable workflow progress and transport admission."""

import asyncio
import json
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from automation import runner as automation_runner
from automation.store import AutomationStore
from goals import create_goal_service
from goals.executor import StepExecutionResult
from goals.recovery import recover_goal_supervisors
from host_workflow_service import AUTOMATION_EXECUTION_JOB, WorkflowService
from messaging_gateway import GatewayAdapter, MessageEnvelope, MessagingGateway
from server_http import handle_webhook
from tests.support.model_routes import RouteAwareRouter
from work_fabric.models import WorkConflict
from work_fabric.scope import WorkScope
from work_fabric.service import WorkService


async def _until(predicate, *, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not reached")
        await asyncio.sleep(0.01)


def _goal(tmp_path, steps, *, handlers=None):
    work = WorkService.open(str(tmp_path / "work.sqlite3"))
    service = create_goal_service(work, handlers=handlers)
    goal = service.create(title="Review regression", objective="Complete the plan")
    goal = service.plan(goal.goal_id, steps, expected_version=goal.version)
    service.start(goal.goal_id, expected_version=goal.version)
    return work, service, goal.goal_id


@pytest.mark.asyncio
@pytest.mark.parametrize("steps", [
    [{"step_id": "first", "kind": "verification"},
     {"step_id": "second", "kind": "verification", "dependencies": ["first"]}],
    [{"step_id": f"step-{index}", "kind": "verification"} for index in range(7)],
])
async def test_work_scheduler_completes_dependent_and_multibatch_goals(tmp_path, steps):
    work, service, goal_id = _goal(tmp_path, steps)
    await work.start()
    try:
        await _until(lambda: service.get(goal_id).status == "succeeded")
        assert all(step.status == "succeeded" for step in service.repository.list_steps(goal_id))
        assert len(work.jobs.list(owner_kind="goal", owner_id=goal_id)) >= 2
    finally:
        await work.shutdown()


def test_goal_restart_reuses_supervisor_admitted_before_restart(tmp_path):
    work, service, goal_id = _goal(tmp_path, [{"step_id": "check", "kind": "verification"}])
    original = work.jobs.list(owner_id=goal_id)[0]
    reopened = WorkService.open(str(tmp_path / "work.sqlite3"))
    recovered = create_goal_service(reopened)
    report = recover_goal_supervisors(recovered)
    assert report["scheduled"][0]["job_id"] == original.job_id
    assert len(reopened.jobs.list(owner_id=goal_id)) == 1
    assert reopened.jobs.require(original.job_id).input_manifest == original.input_manifest


@pytest.mark.asyncio
async def test_goal_retry_has_future_successor_and_respects_delay(tmp_path):
    calls = []

    async def execute(_context):
        calls.append(time.time())
        return StepExecutionResult(status="retry_scheduled" if len(calls) == 1 else "succeeded")

    work, service, goal_id = _goal(tmp_path, [{
        "step_id": "retry", "kind": "python", "max_attempts": 2,
        "retry_policy": {"base_delay_s": 0.3},
    }], handlers={"python": execute})
    work.scheduler.poll_interval_s = 0.05
    await work.start()
    try:
        await _until(lambda: service.get(goal_id).status == "succeeded")
        assert len(calls) == 2
        assert calls[1] - calls[0] >= 0.29
    finally:
        await work.shutdown()


@pytest.mark.asyncio
async def test_goal_pause_survives_running_step_completion_until_resume(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def execute(_context):
        entered.set()
        await release.wait()

    work, service, goal_id = _goal(tmp_path, [
        {"step_id": "first", "kind": "python"},
        {"step_id": "second", "kind": "verification", "dependencies": ["first"]},
    ], handlers={"python": execute})
    await work.start()
    try:
        await asyncio.wait_for(entered.wait(), 2)
        service.pause(goal_id, expected_version=service.get(goal_id).version, reason="user pause")
        release.set()
        await _until(lambda: work.scheduler.active_count == 0)
        assert service.get(goal_id).status == "paused"
        assert service.repository.get_step(goal_id, "second").status == "pending"
        assert not work.jobs.list(owner_id=goal_id, statuses=("queued",))
        service.resume(goal_id, expected_version=service.get(goal_id).version)
        await _until(lambda: service.get(goal_id).status == "succeeded")
    finally:
        release.set()
        await work.shutdown()


@pytest.mark.asyncio
async def test_goal_paused_during_wait_resolution_does_not_dispatch_dependant(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def resolve(_wait):
        entered.set()
        await release.wait()
        return {"status": "succeeded"}

    work, service, goal_id = _goal(tmp_path, [
        {"step_id": "remote", "kind": "wait", "wait_spec": {"source": "remote"}},
        {"step_id": "second", "kind": "verification", "dependencies": ["remote"]},
    ])
    service.register_wait_resolver("remote", resolve)
    await work.start()
    try:
        await asyncio.wait_for(entered.wait(), 2)
        service.pause(goal_id, expected_version=service.get(goal_id).version, reason="user pause")
        release.set()
        await _until(lambda: work.scheduler.active_count == 0)
        assert service.get(goal_id).status == "paused"
        assert service.repository.get_step(goal_id, "remote").status == "succeeded"
        assert service.repository.get_step(goal_id, "second").attempt_count == 0
        assert not work.jobs.list(owner_id=goal_id, statuses=("queued",))
    finally:
        release.set()
        await work.shutdown()


def _workflow(tmp_path):
    store = AutomationStore(str(tmp_path / "automations.json"))
    work = WorkService.open(str(tmp_path / "work.sqlite3"))
    work.scheduler.run_once = AsyncMock(return_value=0)
    host = SimpleNamespace(
        automations=store,
        automation_ports=lambda: object(),
        router=RouteAwareRouter(),
    )
    workflow = WorkflowService(host)
    workflow.bind_work(work)
    host.require_runtime = lambda: SimpleNamespace(workflows=workflow)
    return store, work, workflow, host


def test_disabling_automation_discards_claims_without_reenable_replay(tmp_path):
    path = str(tmp_path / "automations.json")
    store = AutomationStore(path)
    webhook = store.add("Hook", "Run", {"type": "webhook"})
    scheduled = store.add(
        "Timer", "Run", {"type": "interval", "seconds": 60},
    )
    store.admit_trigger(webhook["id"], claim_id="accepted-trigger")
    store.claim_due_tasks(now_ts=5_000)

    assert store.update(webhook["id"], {"enabled": False})
    assert store.update(scheduled["id"], {"enabled": False})
    assert store.recover_trigger_claims() == []
    assert store.recover_scheduled_claims() == []
    assert store.get(webhook["id"])["last_trigger_status"] == "cancelled_disabled"
    assert store.get(scheduled["id"])["last_schedule_status"] == "cancelled_disabled"

    reloaded = AutomationStore(path)
    assert reloaded.update(webhook["id"], {"enabled": True})
    assert reloaded.update(scheduled["id"], {"enabled": True})
    assert reloaded.recover_trigger_claims() == []
    assert reloaded.recover_scheduled_claims() == []


def test_disabled_automation_rejects_new_trigger_admission(tmp_path):
    store = AutomationStore(str(tmp_path / "automations.json"))
    task = store.add("Hook", "Run", {"type": "webhook"}, enabled=False)
    with pytest.raises(RuntimeError, match="disabled"):
        store.admit_trigger(task["id"], claim_id="must-not-exist")


def test_recovery_drains_legacy_claims_from_disabled_definitions(tmp_path):
    path = str(tmp_path / "automations.json")
    store = AutomationStore(path)
    webhook = store.add("Hook", "Run", {"type": "webhook"})
    scheduled = store.add(
        "Timer", "Run", {"type": "interval", "seconds": 60},
    )
    store.admit_trigger(webhook["id"], claim_id="legacy-trigger")
    store.claim_due_tasks(now_ts=5_000)
    # Recreate the persisted pre-fix shape: disabled definitions that still
    # carry accepted claims into the next process.
    for task in store.tasks:
        task["enabled"] = False
    store.save()

    reloaded = AutomationStore(path)
    assert reloaded.recover_trigger_claims() == []
    assert reloaded.recover_scheduled_claims() == []
    reopened = AutomationStore(path)
    assert all(not row.get("trigger_claims") for row in reopened.tasks)
    assert all(not row.get("scheduled_claim") for row in reopened.tasks)


@pytest.mark.asyncio
async def test_automation_work_rechecks_live_enabled_definition(tmp_path, monkeypatch):
    store, work, workflow, _ = _workflow(tmp_path)
    task = store.add(
        "Accepted", "Original prompt", {"type": "webhook"},
        model_route={"mode": "cloud", "provider": "test", "model": "model"},
    )
    claim = store.admit_trigger(task["id"], claim_id="accepted")
    accepted = dict(task, _trigger_claim_id=claim["claim_id"])
    job_id = await workflow.run_automation(accepted)
    job = work.jobs.require(job_id)
    store.update(task["id"], {"enabled": False})
    cancel = Mock()
    execute = AsyncMock()
    monkeypatch.setattr(automation_runner, "execute_automation", execute)
    execution = SimpleNamespace(
        job=job,
        service=SimpleNamespace(cancel=cancel),
        cancellation_requested=lambda: False,
    )

    with pytest.raises(asyncio.CancelledError):
        await workflow._automation_job(execution)

    cancel.assert_called_once_with(
        job_id, reason="automation_cancellation_epoch_advanced",
    )
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_disable_reenable_cannot_revive_stale_claim_copy(tmp_path, monkeypatch):
    store, work, workflow, _ = _workflow(tmp_path)
    task = store.add(
        "Accepted", "Original prompt", {"type": "webhook"},
        model_route={"mode": "cloud", "provider": "test", "model": "model"},
    )
    claim = store.admit_trigger(task["id"], claim_id="accepted")
    stale = dict(task, _trigger_claim_id=claim["claim_id"])
    job_id = await workflow.run_automation(stale, trigger_source="webhook")
    queued = work.jobs.require(job_id)

    store.update(task["id"], {"enabled": False})
    store.update(task["id"], {"enabled": True})
    assert not store.claim_is_active(
        task["id"], claim["claim_id"], claim_kind="trigger",
    )
    assert await workflow.run_automation(
        stale, trigger_source="webhook",
    ) == ""

    cancel = Mock()
    execute = AsyncMock()
    monkeypatch.setattr(automation_runner, "execute_automation", execute)
    execution = SimpleNamespace(
        job=queued,
        service=SimpleNamespace(cancel=cancel),
        cancellation_requested=lambda: False,
    )
    with pytest.raises(asyncio.CancelledError):
        await workflow._automation_job(execution)
    cancel.assert_called_once_with(
        job_id, reason="automation_cancellation_epoch_advanced",
    )
    execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("reenable", [False, True])
async def test_disable_epoch_fences_queued_manual_work_across_restart(
    tmp_path, monkeypatch, reenable,
):
    store, work, workflow, _ = _workflow(tmp_path)
    task = store.add(
        "Manual", "Original prompt", {"type": "interval", "seconds": 60},
        model_route={"mode": "cloud", "provider": "test", "model": "model"},
    )
    job_id = await workflow.run_automation(task, trigger_source="manual")
    queued = work.jobs.require(job_id)
    assert queued.input_manifest["cancellation_epoch"] == 0

    # Simulate a process dying after the definition commit but before the WS
    # handler reaches WorkflowService.cancel_automation().
    store.update(task["id"], {"enabled": False})
    assert store.get(task["id"])["cancellation_epoch"] == 1
    if reenable:
        store.update(task["id"], {"enabled": True})
        assert store.get(task["id"])["cancellation_epoch"] == 1

    execute = AsyncMock()
    monkeypatch.setattr(automation_runner, "execute_automation", execute)
    reopened_work = WorkService.open(work.repository.path)
    reopened_host = SimpleNamespace(
        automations=store,
        automation_ports=lambda: object(),
        router=RouteAwareRouter(),
    )
    reopened_workflow = WorkflowService(reopened_host)
    reopened_workflow.bind_work(reopened_work)

    try:
        assert await reopened_work.scheduler.run_once() == 1
        terminal = await reopened_work.jobs.wait(job_id, timeout_s=5)
        assert terminal.status == "cancelled"
        assert terminal.cancel_reason == "automation_cancellation_epoch_advanced"
        execute.assert_not_awaited()
    finally:
        await reopened_work.shutdown()


@pytest.mark.asyncio
async def test_cancel_automation_exhausts_bounded_nonterminal_pages(
    tmp_path, monkeypatch,
):
    store, work, workflow, _ = _workflow(tmp_path)
    task = store.add(
        "Queued", "Run", {"type": "webhook"},
        model_route={"mode": "cloud", "provider": "test", "model": "model"},
    )
    job_ids = [await workflow.run_automation(task) for _ in range(5)]

    original_list = work.jobs.list
    work.jobs.list = Mock(wraps=original_list)
    monkeypatch.setattr(
        "host_workflow_service.AUTOMATION_CANCEL_BATCH_SIZE", 2,
    )
    assert workflow.cancel_automation(
        task["id"], reason="automation_disabled",
    ) == 5
    assert all(work.jobs.require(job_id).status == "cancelled" for job_id in job_ids)
    assert work.jobs.list.call_count == 4
    requested_statuses = set(work.jobs.list.call_args.kwargs["statuses"])
    assert requested_statuses == {"queued", "leased", "running", "waiting", "paused"}
    assert work.jobs.list.call_args.kwargs["cancel_requested"] is False


@pytest.mark.asyncio
async def test_webhook_recovery_reuses_original_occurrence_after_edits(tmp_path):
    store, work, workflow, host = _workflow(tmp_path)
    task = store.add("Original", "Original prompt", {"type": "webhook", "token": "secret"})
    response = await handle_webhook(host, "secret", SimpleNamespace(
        headers={"idempotency-key": "first"}, body=AsyncMock(return_value=b"body"),
    ))
    admitted = json.loads(response.body)
    assert admitted["work_job_id"]
    store.admit_trigger(task["id"], payload="second", source="webhook", claim_id="second")
    store.update(task["id"], {"name": "Changed", "prompt": "Changed prompt"})
    reopened, reopened_work, recovered_workflow, _ = _workflow(tmp_path)
    claim = reopened.recover_trigger_claims()[0]
    recovered = dict(claim["task"], _trigger_claim_id=claim["claim_id"])
    job_id = await recovered_workflow.run_automation(
        recovered, payload=claim["payload"], trigger_source=claim["source"],
    )
    assert job_id == admitted["work_job_id"]
    job = reopened_work.jobs.require(job_id)
    assert job.input_manifest["task"]["prompt"] == "Original prompt"
    assert "trigger_claims" not in job.input_manifest["task"]
    assert len(work.jobs.list()) == 1
    with pytest.raises(WorkConflict, match="occurrence identity"):
        await workflow.run_automation(recovered, payload="different", trigger_source="webhook")


@pytest.mark.asyncio
async def test_claim_snapshot_survives_edit_before_work_admission(tmp_path):
    store, work, workflow, _ = _workflow(tmp_path)
    task = store.add("Original", "Accepted prompt", {"type": "interval", "seconds": 60})
    claim = store.claim_due_tasks(now_ts=5000)[0]
    store.update(task["id"], {"prompt": "New prompt"})
    recovered = dict(store.recover_scheduled_claims()[0]["task"],
                     _scheduled_claim_id=claim["claim_id"])
    job_id = await workflow.run_automation(recovered, trigger_source="schedule")
    assert work.jobs.require(job_id).input_manifest["task"]["prompt"] == "Accepted prompt"


@pytest.mark.asyncio
async def test_legacy_webhook_manifest_can_be_recovered_unchanged(tmp_path):
    store, work, workflow, _ = _workflow(tmp_path)
    task = store.add("Legacy", "Run", {"type": "webhook"})
    claim = store.admit_trigger(task["id"], claim_id="legacy", source="webhook", payload="body")
    raw_task = dict(task, _trigger_claim_id=claim["claim_id"])
    old = work.jobs.create(
        AUTOMATION_EXECUTION_JOB, owner_kind="automation", owner_id=task["id"],
        scope=WorkScope(), idempotency_key=f"automation:{task['id']}:legacy",
        input_manifest={"task": raw_task, "payload": "body", "trigger_source": "webhook",
                        "claim_id": "legacy", "claim_kind": "trigger"},
        max_attempts=3,
    )
    recovered = dict(store.recover_trigger_claims()[0]["task"], _trigger_claim_id="legacy")
    assert await workflow.run_automation(recovered, "body", "webhook") == old.job_id
    assert work.jobs.require(old.job_id).input_manifest["task"] == raw_task


@pytest.mark.asyncio
async def test_failed_claim_does_not_block_other_recovered_automations(tmp_path):
    store = AutomationStore(str(tmp_path / "automations.json"))
    first = store.add("Broken", "Run", {"type": "webhook"})
    second = store.add("Healthy", "Run", {"type": "webhook"})
    claims = [store.admit_trigger(task["id"]) for task in (first, second)]
    handled = asyncio.Event()

    async def run(task, **_kwargs):
        if task["id"] == first["id"]:
            raise WorkConflict("one malformed occurrence")
        handled.set()
        return "job-id"

    scheduler = asyncio.create_task(automation_runner.automation_loop(store, run))
    try:
        await asyncio.wait_for(handled.wait(), 2)
        assert not scheduler.done()
        assert len(store.recover_trigger_claims()) == 2
    finally:
        scheduler.cancel()
        await asyncio.gather(scheduler, return_exceptions=True)
        for claim in claims:
            automation_runner.release_claim_tracking(claim["claim_id"])


@pytest.mark.asyncio
async def test_initial_recovery_store_error_is_retried(tmp_path, monkeypatch):
    store = AutomationStore(str(tmp_path / "automations.json"))
    task = store.add("Healthy", "Run", {"type": "webhook"})
    claim = store.admit_trigger(task["id"])
    calls = 0
    original = store.recover_scheduled_claims

    def recover():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary read failure")
        return original()

    monkeypatch.setattr(store, "recover_scheduled_claims", recover)
    original_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda seconds: original_sleep(0.01 if seconds == 30 else seconds))
    handled = asyncio.Event()

    async def run(*_args, **_kwargs):
        handled.set()

    scheduler = asyncio.create_task(automation_runner.automation_loop(store, run))
    try:
        await asyncio.wait_for(handled.wait(), 2)
        assert calls >= 2 and not scheduler.done()
    finally:
        scheduler.cancel()
        await asyncio.gather(scheduler, return_exceptions=True)
        automation_runner.release_claim_tracking(claim["claim_id"])


class _Adapter(GatewayAdapter):
    name = "fake"

    def __init__(self, gateway):
        super().__init__(gateway)
        self.sent = []

    async def start(self):
        self.connected = True

    async def stop(self):
        self.connected = False

    async def send_text(self, envelope, text):
        self.sent.append((envelope.message_id, text))


def _gateway(tmp_path):
    gateway = MessagingGateway(str(tmp_path / "messaging.json"))
    adapter = _Adapter(gateway)
    gateway.register(adapter)
    gateway.update(enabled=True)
    gateway.update(adapter="fake", values={"enabled": True, "allowed_users": ["u"]})
    gateway.set_router(AsyncMock(return_value="reply"))
    return gateway, adapter


@pytest.mark.asyncio
async def test_outbox_retries_transient_send_without_replaying_inbound(tmp_path):
    gateway, adapter = _gateway(tmp_path)
    original = adapter.send_text
    attempts = []

    async def flaky(envelope, text):
        attempts.append(time.time())
        if len(attempts) == 1:
            raise OSError("temporary disconnect")
        await original(envelope, text)

    adapter.send_text = flaky
    await gateway.start()
    try:
        envelope = MessageEnvelope("fake", "first", "c", "u", "hello")
        assert await gateway.receive(envelope)
        # The transport can finish before its durable delivery acknowledgement.
        await _until(lambda: bool(adapter.sent) and gateway.ingress.outbound_pending(envelope) == "")
        assert attempts[1] - attempts[0] >= 0.45
        assert gateway.router.await_count == 1
        assert gateway.ingress.outbound_pending(envelope) == ""
    finally:
        await gateway.stop()
    assert gateway._outbox_task is None


@pytest.mark.asyncio
async def test_outbox_drains_multiple_pages_with_bounded_delivery(tmp_path):
    gateway, adapter = _gateway(tmp_path)
    gateway._outbox_batch_size = 7
    active = peak = 0

    async def send(envelope, text):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.005)
            adapter.sent.append((envelope.message_id, text))
        finally:
            active -= 1

    adapter.send_text = send
    for index in range(23):
        envelope = MessageEnvelope("fake", str(index), "c", "u", "hello")
        gateway.ingress.admit(envelope, "hello")
        gateway.ingress.complete_routed(envelope, reply="persisted reply")
    await gateway.start()
    try:
        await _until(lambda: len(adapter.sent) == 23)
        assert peak <= gateway._outbox_concurrency
        assert gateway.router.await_count == 0
    finally:
        await gateway.stop()


@pytest.mark.asyncio
async def test_outbox_shutdown_cancels_send_and_keeps_durable_reply(tmp_path):
    gateway, adapter = _gateway(tmp_path)
    entered = asyncio.Event()

    async def blocked(*_args):
        entered.set()
        await asyncio.Event().wait()

    adapter.send_text = blocked
    envelope = MessageEnvelope("fake", "pending", "c", "u", "hello")
    gateway.ingress.admit(envelope, "hello")
    gateway.ingress.complete_routed(envelope, reply="reply")
    await gateway.start()
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(gateway.stop(), 2)
    assert gateway.ingress.outbound_pending(envelope) == "reply"
    assert gateway._outbox_task is None


@pytest.mark.asyncio
async def test_outbox_item_failure_keeps_sibling_sends_owned(tmp_path, monkeypatch):
    gateway, adapter = _gateway(tmp_path)
    adapter.connected = True
    blocked, cancelled = asyncio.Event(), asyncio.Event()

    async def send(envelope, _text):
        if envelope.message_id == "first":
            raise OSError("offline")
        blocked.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    def fail_error_write(*_args):
        raise OSError("disk write failed")

    adapter.send_text = send
    monkeypatch.setattr(gateway.ingress, "mark_outbound_error", fail_error_write)
    for message_id in ("first", "second"):
        envelope = MessageEnvelope("fake", message_id, "c", "u", "hello")
        gateway.ingress.admit(envelope, "hello")
        gateway.ingress.complete_routed(envelope, reply="reply")
    pump = asyncio.create_task(gateway._pump_outbox_once())
    try:
        await asyncio.wait_for(blocked.wait(), 2)
        # A failed peer must not detach this still-running delivery from the pump.
        await asyncio.sleep(0.02)
        assert not pump.done()
    finally:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_discord_resume_cursor_waits_for_durable_admission(tmp_path, monkeypatch):
    gateway, _ = _gateway(tmp_path)
    gateway.update(adapter="discord", values={"enabled": True, "allowed_users": ["u"]})
    discord = gateway.adapters["discord"]
    discord.session_id = "session"
    discord.sequence = 10
    discord._persist_resume_state()
    submitted = []
    gateway.submit = lambda envelope, retry=False: submitted.append(envelope)
    attempts = 0
    admission_started, allow_admission = asyncio.Event(), asyncio.Event()
    heartbeat_seen = asyncio.Event()
    heartbeats = []

    async def attachments(_message):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("download failed before admission")
        admission_started.set()
        await allow_admission.wait()
        return [], []

    discord._message_attachments = attachments
    resumes = []
    finished = asyncio.Event()

    class Socket:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def recv(self): return json.dumps({"d": {"heartbeat_interval": 10}})
        async def send(self, raw):
            event = json.loads(raw)
            if event["op"] == 6:
                resumes.append(event["d"]["seq"])
            elif event["op"] == 1:
                heartbeats.append(event["d"])
                heartbeat_seen.set()
        def __aiter__(self): return self.events()
        async def events(self):
            yield json.dumps({"op": 0, "s": 11, "t": "MESSAGE_CREATE", "d": {
                "id": "message", "channel_id": "c", "author": {"id": "u"},
                "content": "hello",
            }})
            finished.set()
            await asyncio.Event().wait()

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=lambda *_a, **_k: Socket()))
    original_sleep = asyncio.sleep

    async def fast_retry(seconds):
        await original_sleep(0 if seconds == 5 else seconds)

    monkeypatch.setattr(asyncio, "sleep", fast_retry)
    poll = asyncio.create_task(discord._run("unused-token"))
    try:
        await asyncio.wait_for(admission_started.wait(), 3)
        await asyncio.wait_for(heartbeat_seen.wait(), 3)
        assert heartbeats[-1] == 11
        assert discord.sequence == gateway.ingress.adapter_state("discord", "sequence") == 10
        assert submitted == []
        allow_admission.set()
        await asyncio.wait_for(finished.wait(), 3)
        assert resumes == [10, 10]
        assert discord.sequence == gateway.ingress.adapter_state("discord", "sequence") == 11
        assert len(submitted) == 1
        admitted = gateway.ingress.admit(submitted[0], "hello")
        assert admitted.duplicate and admitted.status == "admitted"
        assert gateway.router.await_count == 0
    finally:
        poll.cancel()
        await asyncio.gather(poll, return_exceptions=True)
        await discord.client.aclose()
