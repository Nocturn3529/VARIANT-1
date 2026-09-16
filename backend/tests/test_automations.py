"""Automation scheduler helpers and durable run settings."""

from __future__ import annotations

from datetime import datetime
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from automation import runner as automation_runner
from automation import store as automation_store
from automation.store import (
    AutomationPersistenceError,
    AutomationStore,
    durable_checkpoints_enabled,
    is_due,
)
from host_workflow_service import WorkflowService
from tests.support.model_routes import RouteAwareRouter
from work_fabric.service import WorkService
import ws_automations
import background_tasks


def test_automation_route_is_copied_into_claim_and_survives_reload(tmp_path):
    path = str(tmp_path / "automations.json")
    store = AutomationStore(path)
    route = {"mode": "cloud", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoning_effort": "xhigh"}
    task = store.add("Pinned", "work", {"type": "webhook"}, model_route=route)
    route["model"] = "changed-caller-dictionary"
    assert store.get(task["id"])["model_route"]["model"] == "gpt-5.6-luna"
    claim = store.admit_trigger(task["id"], claim_id="fixture")
    snapshot = store.task_for_claim(task["id"], claim["claim_id"])
    store.update(task["id"], {"model_route": {**route, "reasoning_effort": "low"}})
    reloaded = AutomationStore(path)
    assert reloaded.task_for_claim(task["id"], claim["claim_id"])["model_route"] == snapshot["model_route"]
    assert reloaded.get(task["id"])["model_route"]["reasoning_effort"] == "low"


def test_legacy_automation_route_is_pinned_once(tmp_path):
    store = AutomationStore(str(tmp_path / "automations.json"))
    task = store.add("Legacy", "work", {"type": "interval", "seconds": 60})
    route = {"mode": "cloud", "provider": "p", "model": "m", "reasoning_effort": "high"}
    assert store.pin_model_route(task["id"], route) == route
    assert store.pin_model_route(task["id"], {"mode": "local", "model": "other"}) == route


def test_saved_local_worker_route_does_not_silently_change_weights():
    from model_runtime.context import validate_worker_model_route
    router = RouteAwareRouter(mode="local")
    router.model_name = "second.gguf"
    with pytest.raises(ValueError, match="requires local model"):
        validate_worker_model_route(router, {"mode": "local", "model": "first.gguf"})
    validate_worker_model_route(router, {"mode": "local", "model": "second.gguf"})
    validate_worker_model_route(router, {"mode": "cloud", "model": "first.gguf"})
import server_http


@pytest.mark.parametrize(
    "task,now_ts,expected",
    [
        (
            {"enabled": False, "trigger": {"type": "interval", "seconds": 60}, "last_run": 0},
            1_000_000.0,
            False,
        ),
        (
            {"enabled": True, "trigger": {"type": "interval", "seconds": 3600}, "last_run": 0},
            3_000.0,
            False,
        ),
        (
            {"enabled": True, "trigger": {"type": "interval", "seconds": 3600}, "last_run": 0},
            5_000.0,
            True,
        ),
        (
            {"enabled": True, "trigger": {"type": "webhook", "token": "x"}, "last_run": 0},
            9_999.0,
            False,
        ),
    ],
    ids=[
        "disabled_interval",
        "interval_not_elapsed",
        "interval_due",
        "webhook_never_due",
    ],
)
def test_is_due_interval_and_webhook(task, now_ts, expected):
    assert is_due(task, now_ts) is expected


def test_is_due_daily_after_scheduled_time():
    # Friday 2024-01-05 10:30 UTC
    now = datetime(2024, 1, 5, 10, 30, 0)
    now_ts = now.timestamp()
    task = {
        "enabled": True,
        "trigger": {"type": "daily", "time": "09:00"},
        "last_run": 0,
    }
    assert is_due(task, now_ts) is True

    task["last_run"] = now.replace(hour=9, minute=5).timestamp()
    assert is_due(task, now_ts) is False


def test_daily_catches_up_before_todays_target_after_missed_day():
    now = datetime(2024, 1, 6, 8, 0, 0)
    task = {"enabled": True, "trigger": {"type": "daily", "time": "09:00"},
            "last_run": datetime(2024, 1, 4, 9, 0, 0).timestamp()}
    assert is_due(task, now.timestamp()) is True
    task["misfire_policy"] = "skip"
    assert is_due(task, now.timestamp()) is False


def test_is_due_weekly_catches_up_on_missed_day():
    # Weekly "mon 09:00"; the occurrence is Monday 2024-01-01 09:00.
    task = {
        "enabled": True,
        "trigger": {"type": "weekly", "day": "mon", "time": "09:00"},
        "last_run": 0,
    }
    mon_9 = datetime(2024, 1, 1, 9, 0, 0).timestamp()
    fri = datetime(2024, 1, 5, 12, 0, 0).timestamp()

    # Fires on the day at/after the time, AND (the catch-up fix) on a later day in
    # the same week if the app was off on Monday -- rather than silently skipping.
    assert is_due(task, mon_9) is True
    assert is_due(task, fri) is True

    # Once the occurrence has run it does not re-fire the same week...
    task["last_run"] = mon_9
    assert is_due(task, fri) is False
    # ...but next week's occurrence is due again.
    assert is_due(task, datetime(2024, 1, 8, 9, 0, 0).timestamp()) is True


def test_is_due_weekly_not_before_first_occurrence():
    # Brand-new task, and the target weekday hasn't occurred yet at/before now.
    task = {
        "enabled": True,
        "trigger": {"type": "weekly", "day": "mon", "time": "09:00"},
        # last run just after last week's occurrence -> nothing to catch up.
        "last_run": datetime(2023, 12, 25, 9, 0, 0).timestamp(),
    }
    # Monday 08:00, before this week's 09:00 -> not due yet.
    assert is_due(task, datetime(2024, 1, 1, 8, 0, 0).timestamp()) is False


def test_weekly_skip_policy_does_not_catch_prior_week_before_target():
    task = {"enabled": True, "misfire_policy": "skip",
            "trigger": {"type": "weekly", "day": "mon", "time": "09:00"},
            "last_run": datetime(2023, 12, 25, 9, 0, 0).timestamp()}
    assert is_due(task, datetime(2024, 1, 1, 8, 0, 0).timestamp()) is False
    assert is_due(task, datetime(2024, 1, 1, 9, 0, 0).timestamp()) is True


def test_is_due_weekdays_runs_once_after_the_daily_target():
    task = {
        "enabled": True,
        "trigger": {"type": "weekdays", "time": "09:00"},
        "last_run": 0,
    }
    monday = datetime(2024, 1, 1, 10, 0, 0)

    assert is_due(task, monday.timestamp()) is True

    task["last_run"] = monday.replace(hour=9, minute=5).timestamp()
    assert is_due(task, monday.replace(hour=17).timestamp()) is False
    assert is_due(task, datetime(2024, 1, 2, 9, 0, 0).timestamp()) is True


def test_is_due_weekdays_catches_up_latest_occurrence_on_weekends():
    task = {
        "enabled": True,
        "trigger": {"type": "weekdays", "time": "09:00"},
        "last_run": 0,
    }

    assert is_due(task, datetime(2024, 1, 6, 12, 0, 0).timestamp()) is True
    assert is_due(task, datetime(2024, 1, 7, 12, 0, 0).timestamp()) is True

    task["misfire_policy"] = "skip"
    assert is_due(task, datetime(2024, 1, 6, 12, 0, 0).timestamp()) is False
    assert is_due(task, datetime(2024, 1, 7, 12, 0, 0).timestamp()) is False


def test_durable_checkpoints_default_to_enabled_for_missing_field():
    assert durable_checkpoints_enabled({}) is True
    assert durable_checkpoints_enabled({"durable_checkpoints": True}) is True
    assert durable_checkpoints_enabled({"durable_checkpoints": False}) is False


def test_automation_store_retries_transient_atomic_replace_denial(
    tmp_path, monkeypatch,
):
    store = AutomationStore(str(tmp_path / "automations.json"))
    original = automation_store.os.replace
    attempts = 0

    def transient(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("transient Windows sharing denial")
        return original(source, destination)

    monkeypatch.setattr(automation_store.os, "replace", transient)
    saved = store.add("Retry", "Persist me", {"type": "webhook"})

    assert attempts == 3
    assert store.get(saved["id"])["name"] == "Retry"


def test_automation_store_add_defaults_to_durable(tmp_path):
    store = AutomationStore(str(tmp_path / "automations.json"))

    task = store.add(
        "Morning focus",
        "Pick a task",
        {"type": "daily", "time": "09:00"},
    )

    assert task["durable_checkpoints"] is True
    listed = store.list()
    assert listed[0]["durable_checkpoints"] is True


def test_automation_store_treats_legacy_missing_field_as_durable(tmp_path):
    store = AutomationStore(str(tmp_path / "automations.json"))
    store.tasks = [{
        "id": "legacy",
        "name": "Legacy automation",
        "enabled": True,
        "prompt": "Do it",
        "trigger": {"type": "interval", "seconds": 3600},
        "last_run": 0,
    }]

    listed = store.list()

    assert listed[0]["durable_checkpoints"] is True


def test_missing_cron_dependency_is_visible_on_saved_automation(tmp_path, monkeypatch):
    monkeypatch.setattr(automation_store, "HAS_CRONITER", False)
    store = AutomationStore(str(tmp_path / "automations.json"))
    store.tasks = [{
        "id": "cron-task",
        "name": "Scheduled",
        "enabled": True,
        "prompt": "Do it",
        "trigger": {"type": "cron", "expr": "0 9 * * *"},
        "last_run": 0,
    }]

    listed = store.list()[0]

    assert listed["schedule_runnable"] is False
    assert "croniter is not installed" in listed["schedule_error"]
    assert store.claim_due_tasks(datetime.now().timestamp()) == []


def test_automation_store_update_can_opt_out_of_durable_checkpoints(tmp_path):
    store = AutomationStore(str(tmp_path / "automations.json"))
    task = store.add(
        "Ephemeral",
        "Do it once",
        {"type": "interval", "seconds": 3600},
    )

    assert store.update(task["id"], {"durable_checkpoints": False}) is True

    updated = store.get(task["id"])
    assert updated["durable_checkpoints"] is False
    assert store.list()[0]["durable_checkpoints"] is False
    assert durable_checkpoints_enabled(updated) is False


@pytest.mark.asyncio
async def test_automation_work_jobs_serialize_the_same_owner(tmp_path, monkeypatch):
    store = AutomationStore(str(tmp_path / "automations.json"))
    task = store.add("Hook", "Do it", {"type": "webhook"})
    work = WorkService.open(str(tmp_path / "work.sqlite3"))
    host = SimpleNamespace(
        automations=store,
        automation_ports=lambda: object(),
    )
    host.router = RouteAwareRouter()
    workflows = WorkflowService(host)
    workflows.bind_work(work)
    started, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def fake_once(
        _ports, _task, payload="", *, cancellation_requested=None,
    ):
        del cancellation_requested
        seen.append(payload)
        if len(seen) == 1:
            started.set()
            await release.wait()
        return {"status": "completed", "reply": "done", "mood": "neutral"}

    monkeypatch.setattr(automation_runner, "execute_automation", fake_once)
    first = await workflows.run_automation(task, "first")
    await started.wait()
    second = await workflows.run_automation(task, "second")
    release.set()
    await work.jobs.wait(first, timeout_s=5)
    for _ in range(20):
        await work.scheduler.run_once()
        if work.jobs.require(second).status == "succeeded":
            break
        await asyncio.sleep(0.05)
    await work.jobs.wait(second, timeout_s=5)
    assert seen == ["first", "second"]


@pytest.mark.asyncio
async def test_workflow_service_admits_one_work_owned_execution(tmp_path, monkeypatch):
    store = AutomationStore(str(tmp_path / "automations.json"))
    work = WorkService.open(str(tmp_path / "work.sqlite3"))
    host = SimpleNamespace(
        automation_ports=lambda: object(),
        automations=store,
    )
    host.router = RouteAwareRouter()
    workflows = WorkflowService(host)
    workflows.bind_work(work)
    seen = []

    async def fake_run(
        _ports, task, payload="", *, cancellation_requested=None,
    ):
        seen.append((task["id"], payload, bool(cancellation_requested)))
        return {"status": "completed", "reply": "done", "mood": "neutral"}

    monkeypatch.setattr(automation_runner, "execute_automation", fake_run)

    task = store.add(
        "Scheduled", "Run", {"type": "interval", "seconds": 60},
        model_route={"mode": "cloud", "provider": "fixture", "model": "m"},
    )
    job_id = await workflows.run_automation(
        task, payload="due", trigger_source="schedule"
    )
    completed = await work.jobs.wait(job_id, timeout_s=5)

    assert completed.status == "succeeded"
    assert completed.owner_kind == "automation"
    assert completed.owner_id == task["id"]
    assert seen == [(task["id"], "due", True)]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["truncated", "failed", "error", "unknown"])
async def test_automation_work_handler_rejects_every_non_success_status(
    status, monkeypatch,
):
    task = {
        "id": "auto-failed",
        "enabled": True,
        "model_route": {"mode": "cloud", "provider": "fixture", "model": "m"},
    }
    host = SimpleNamespace(
        automation_ports=lambda: object(),
        automations=SimpleNamespace(get=lambda _automation_id: task),
    )
    workflows = WorkflowService(host)
    execution = SimpleNamespace(
        job=SimpleNamespace(input_manifest={"task": task}, owner_id=task["id"]),
        cancellation_requested=lambda: False,
    )

    async def fake_run(*_args, **_kwargs):
        return {"status": status, "reply": "partial native output"}

    monkeypatch.setattr(automation_runner, "execute_automation", fake_run)

    with pytest.raises(RuntimeError, match="partial native output"):
        await workflows._automation_job(execution)


@pytest.mark.asyncio
async def test_automation_work_handler_propagates_durable_cancellation(monkeypatch):
    task = {
        "id": "auto-cancelled",
        "enabled": True,
        "model_route": {"mode": "cloud", "provider": "fixture", "model": "m"},
    }
    host = SimpleNamespace(
        automation_ports=lambda: object(),
        automations=SimpleNamespace(get=lambda _automation_id: task),
    )
    workflows = WorkflowService(host)
    cancellation_checks = iter((False, True))
    execution = SimpleNamespace(
        job=SimpleNamespace(input_manifest={"task": task}, owner_id=task["id"]),
        cancellation_requested=lambda: next(cancellation_checks),
    )

    async def fake_run(*_args, **_kwargs):
        return {"status": "cancelled", "reply": "partial native output"}

    monkeypatch.setattr(automation_runner, "execute_automation", fake_run)

    with pytest.raises(asyncio.CancelledError):
        await workflows._automation_job(execution)


@pytest.mark.asyncio
async def test_truncated_automation_is_unknown_effect_and_is_not_replayed(
    tmp_path, monkeypatch,
):
    store = AutomationStore(str(tmp_path / "automations.json"))
    work = WorkService.open(str(tmp_path / "work.sqlite3"))
    host = SimpleNamespace(
        automation_ports=lambda: object(),
        automations=store,
        router=RouteAwareRouter(),
    )
    workflows = WorkflowService(host)
    workflows.bind_work(work)
    calls = 0

    async def fake_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {
            "status": "truncated",
            "diagnostic": "output limit; partial native output retained",
            "reply": "partial native output retained",
        }

    monkeypatch.setattr(automation_runner, "execute_automation", fake_run)
    task = store.add(
        "Truncated", "Run", {"type": "webhook"},
        model_route={"mode": "cloud", "provider": "fixture", "model": "m"},
    )
    job_id = await workflows.run_automation(task)
    terminal = await work.jobs.wait(job_id, timeout_s=5)
    await work.scheduler.run_once()

    assert terminal.status == "unknown_effect"
    assert "partial native output retained" in terminal.error
    assert calls == 1


@pytest.mark.asyncio
async def test_explicit_manual_run_is_allowed_while_schedule_is_disabled(
    tmp_path, monkeypatch,
):
    store = AutomationStore(str(tmp_path / "automations.json"))
    task = store.add(
        "Manual only", "Run", {"type": "interval", "seconds": 60},
        model_route={"mode": "cloud", "provider": "fixture", "model": "m"},
    )
    store.update(task["id"], {"enabled": False})
    task = store.get(task["id"])
    assert task["cancellation_epoch"] == 1
    work = WorkService.open(str(tmp_path / "work.sqlite3"))
    host = SimpleNamespace(
        automation_ports=lambda: object(), automations=store,
        router=RouteAwareRouter(),
    )
    workflows = WorkflowService(host)
    workflows.bind_work(work)
    execute = AsyncMock(return_value={
        "status": "completed", "reply": "done", "mood": "neutral",
    })
    monkeypatch.setattr(automation_runner, "execute_automation", execute)

    job_id = await workflows.run_automation(task, trigger_source="manual")
    completed = await work.jobs.wait(job_id, timeout_s=5)

    assert completed.status == "succeeded"
    assert completed.input_manifest["cancellation_epoch"] == 1
    execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_cooperative_automation_cancellation_finishes_work_cancelled(
    tmp_path, monkeypatch,
):
    store = AutomationStore(str(tmp_path / "automations.json"))
    work = WorkService.open(str(tmp_path / "work.sqlite3"))
    host = SimpleNamespace(
        automation_ports=lambda: object(),
        automations=store,
        router=RouteAwareRouter(),
    )
    workflows = WorkflowService(host)
    workflows.bind_work(work)

    async def fake_run(*_args, **_kwargs):
        return {
            "status": "cancelled",
            "diagnostic": "worker cooperatively stopped",
            "reply": "partial native output retained",
        }

    monkeypatch.setattr(automation_runner, "execute_automation", fake_run)
    job_id = await workflows.run_automation({"id": "cancelled-auto"})
    terminal = await work.jobs.wait(job_id, timeout_s=5)

    assert terminal.status == "cancelled"
    assert terminal.cancel_requested is True


@pytest.mark.asyncio
async def test_automation_claim_is_finalized_by_terminal_work_event(
    tmp_path, monkeypatch,
):
    store = AutomationStore(str(tmp_path / "automations.json"))
    task = store.add("Terminal claim", "Run", {"type": "interval", "seconds": 60})
    claim = store.claim_due_tasks(now_ts=5_000.0)[0]
    admitted = dict(claim["task"])
    admitted["_scheduled_claim_id"] = claim["claim_id"]
    work = WorkService.open(str(tmp_path / "work.sqlite3"))
    host = SimpleNamespace(
        automation_ports=lambda: object(),
        automations=store,
    )
    host.router = RouteAwareRouter()
    workflows = WorkflowService(host)
    workflows.bind_work(work)

    async def fake_run(*_args, **_kwargs):
        return {"status": "completed", "reply": "done", "mood": "neutral"}

    monkeypatch.setattr(automation_runner, "execute_automation", fake_run)
    job_id = await workflows.run_automation(admitted, trigger_source="schedule")
    completed = await work.jobs.wait(job_id, timeout_s=5)
    assert completed.status == "succeeded"
    assert store.get(task["id"])["scheduled_claim"]["claim_id"] == claim["claim_id"]

    await work.events.dispatch_once(limit=100)

    persisted = store.get(task["id"])
    assert persisted.get("scheduled_claim") is None
    assert persisted["last_schedule_status"] == "completed"


def test_due_occurrence_is_durably_claimed_before_dispatch(tmp_path):
    store = AutomationStore(str(tmp_path / "automations.json"))
    task = store.add(
        "Slow schedule",
        "Run once",
        {"type": "interval", "seconds": 60},
    )

    claims = store.claim_due_tasks(now_ts=5_000.0)

    assert len(claims) == 1
    assert claims[0]["task"]["id"] == task["id"]
    assert store.claim_due_tasks(now_ts=5_030.0) == []
    persisted = store.get(task["id"])
    assert persisted["last_run"] == 5_000.0
    assert persisted["scheduled_claim"]["claim_id"] == claims[0]["claim_id"]
    assert store.complete_scheduled_claim(
        task["id"], claims[0]["claim_id"], status="completed"
    ) is True
    assert store.get(task["id"].strip()).get("scheduled_claim") is None


def test_skipped_scheduled_claim_restores_the_unexecuted_occurrence(tmp_path):
    store = AutomationStore(str(tmp_path / "automations.json"))
    task = store.add(
        "Retry schedule", "Run once", {"type": "interval", "seconds": 60},
    )
    claim = store.claim_due_tasks(now_ts=5_000.0)[0]

    assert store.release_skipped_scheduled_claim(
        task["id"], claim["claim_id"]
    ) is True

    persisted = store.get(task["id"])
    assert persisted["last_run"] == 0.0
    assert persisted.get("scheduled_claim") is None
    assert store.claim_due_tasks(now_ts=5_001.0)


@pytest.mark.asyncio
async def test_scheduler_keeps_recovered_claim_until_work_reports_terminal(
    tmp_path, monkeypatch,
):
    path = tmp_path / "automations.json"
    first = AutomationStore(str(path))
    task = first.add(
        "Recover me", "Run once", {"type": "interval", "seconds": 60}
    )
    claim = first.claim_due_tasks(now_ts=5_000.0)[0]
    reopened = AutomationStore(str(path))
    invoked = asyncio.Event()
    real_sleep = asyncio.sleep

    async def blocked_sleep(_seconds):
        await real_sleep(3600)

    async def run(recovered_task, **_kwargs):
        assert recovered_task["id"] == task["id"]
        assert recovered_task["scheduled_claim"]["claim_id"] == claim["claim_id"]
        invoked.set()
        return "completed"

    monkeypatch.setattr(automation_runner.asyncio, "sleep", blocked_sleep)
    loop = asyncio.create_task(automation_runner.automation_loop(reopened, run))
    try:
        await asyncio.wait_for(invoked.wait(), timeout=1)
        persisted = reopened.get(task["id"])
        assert persisted["scheduled_claim"]["claim_id"] == claim["claim_id"]
        assert "last_schedule_status" not in persisted
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)


@pytest.mark.asyncio
async def test_scheduler_cancellation_retains_scheduled_claim(tmp_path, monkeypatch):
    store = AutomationStore(str(tmp_path / "automations.json"))
    task = store.add("Shutdown", "Keep claim", {"type": "interval", "seconds": 60})
    claim = store.claim_due_tasks(now_ts=5_000.0)[0]
    started = asyncio.Event()
    real_sleep = asyncio.sleep

    async def blocked_run(_task, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    async def blocked_sleep(_seconds):
        await real_sleep(3600)

    monkeypatch.setattr(automation_runner.asyncio, "sleep", blocked_sleep)
    loop = asyncio.create_task(automation_runner.automation_loop(store, blocked_run))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
        persisted = store.get(task["id"])
        assert persisted["scheduled_claim"]["claim_id"] == claim["claim_id"]
        assert store.recover_scheduled_claims()[0]["claim_id"] == claim["claim_id"]
    finally:
        if not loop.done():
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)


@pytest.mark.asyncio
async def test_webhook_202_is_backed_by_durable_trigger_claim(
    tmp_path, monkeypatch,
):
    store = AutomationStore(str(tmp_path / "automations.json"))
    task = store.add("Hook", "Run", {"type": "webhook", "token": "secret"})
    workflows = SimpleNamespace(run_automation=AsyncMock(return_value="completed"))
    runtime = SimpleNamespace(
        workflows=workflows,
        session_runtimes=SimpleNamespace(snapshot=lambda _runtime_id: {}),
        kernel=SimpleNamespace(status=lambda _runtime_id: {}),
    )
    srv = SimpleNamespace(
        automations=store,
        require_runtime=lambda: runtime,
    )

    def fail_spawn(awaitable, **_kwargs):
        awaitable.close()
        raise RuntimeError("scheduler unavailable")

    monkeypatch.setattr(background_tasks, "spawn", fail_spawn)
    request = SimpleNamespace(
        headers={"idempotency-key": "request-1"},
        body=AsyncMock(return_value=b"payload"),
    )
    response = await server_http.handle_webhook(srv, "secret", request)
    body = json.loads(response.body)

    assert response.status_code == 202
    assert body["claim_id"] == f"webhook:{task['id']}:request-1"
    recovered = store.recover_trigger_claims()
    assert len(recovered) == 1
    assert recovered[0]["payload"] == "payload"


def test_automation_mutations_roll_back_when_atomic_save_fails(
    tmp_path, monkeypatch,
):
    path = tmp_path / "automations.json"
    store = AutomationStore(str(path))
    task = store.add(
        "Stable", "Keep me", {"type": "interval", "seconds": 3600}
    )
    before_tasks = store.list()
    before_bytes = path.read_bytes()

    def fail(_tasks):
        raise AutomationPersistenceError("simulated disk failure")

    monkeypatch.setattr(store, "_write_tasks", fail)
    operations = (
        lambda: store.add("New", "No", {"type": "webhook"}),
        lambda: store.update(task["id"], {"name": "Changed"}),
        lambda: store.remove(task["id"]),
        lambda: store.claim_due_tasks(now_ts=9_000.0),
    )
    for operation in operations:
        with pytest.raises(AutomationPersistenceError):
            operation()
        assert store.list() == before_tasks
        assert path.read_bytes() == before_bytes


@pytest.mark.asyncio
async def test_websocket_automation_persistence_error_is_correlated():
    handlers = {}

    def on(*names):
        def decorate(fn):
            for name in names:
                handlers[name] = fn
            return fn
        return decorate

    ws_automations.register(on)

    class FailedStore:
        def add(self, *_args, **_kwargs):
            raise AutomationPersistenceError("disk full")

    websocket = SimpleNamespace(send_json=AsyncMock())
    srv = SimpleNamespace(
        automations=FailedStore(),
        router=RouteAwareRouter(),
        require_runtime=lambda: SimpleNamespace(sessions=None),
        hub=SimpleNamespace(broadcast=AsyncMock()),
    )
    await handlers["automation:add"](
        srv,
        websocket,
        None,
        {
            "type": "automation:add",
            "request_id": "automation-write-1",
            "name": "Never persisted",
        },
    )

    websocket.send_json.assert_awaited_once_with({
        "type": "automation:error",
        "request_id": "automation-write-1",
        "action": "add",
        "error": "automation_persistence_failed",
    })
    srv.hub.broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_websocket_disable_and_remove_cancel_owned_work(tmp_path):
    handlers = {}

    def on(*names):
        def decorate(fn):
            for name in names:
                handlers[name] = fn
            return fn
        return decorate

    ws_automations.register(on)
    store = AutomationStore(str(tmp_path / "automations.json"))
    disabled = store.add("Disable", "Run", {"type": "webhook"})
    removed = store.add("Remove", "Run", {"type": "webhook"})
    workflows = SimpleNamespace(
        cancel_automation=Mock(), run_automation=AsyncMock(),
    )
    delete_worker = AsyncMock()
    websocket = SimpleNamespace(send_json=AsyncMock())
    runtime = SimpleNamespace(
        workflows=workflows,
        session_runtimes=SimpleNamespace(snapshot=lambda _runtime_id: {}),
        kernel=SimpleNamespace(status=lambda _runtime_id: {}),
    )
    srv = SimpleNamespace(
        automations=store,
        require_runtime=lambda: runtime,
        automation_ports=lambda: SimpleNamespace(
            agent=SimpleNamespace(delete_worker_runtime=delete_worker),
        ),
        hub=SimpleNamespace(broadcast=AsyncMock()),
        router=RouteAwareRouter(),
    )

    await handlers["automation:update"](
        srv, websocket, None,
        {"type": "automation:update", "id": disabled["id"], "enabled": False},
    )
    await handlers["automation:run"](
        srv, websocket, None,
        {"type": "automation:run", "id": disabled["id"]},
    )
    await handlers["automation:remove"](
        srv, websocket, None,
        {"type": "automation:remove", "id": removed["id"]},
    )

    assert store.get(disabled["id"])["enabled"] is False
    assert store.get(removed["id"]) is None
    assert workflows.cancel_automation.call_args_list == [
        ((disabled["id"],), {"reason": "automation_disabled"}),
        ((removed["id"],), {"reason": "automation_deleted"}),
    ]
    workflows.run_automation.assert_awaited_once_with(
        store.get(disabled["id"]), trigger_source="manual",
    )
    delete_worker.assert_awaited_once_with(
        source="automation", key=removed["id"],
    )
