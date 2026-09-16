from __future__ import annotations

from types import SimpleNamespace
import time

import pytest

from artifacts import ContentAddressedArtifactStore
from goals import create_goal_service
from goals.composer import ComposerGoals
from goals.executor import StepExecutionContext
from goals.host_handlers import build_goal_host_handlers
from work_fabric.models import WorkActor
from work_fabric.service import WorkService


class TypedChildren:
    def __init__(self):
        self.rows = {}
        self.restart_calls = []
        self.restart_requests = {}

    async def spawn(self, parent_chat_id, **kwargs):
        row = {
            "child_id": kwargs["child_id"],
            "parent_chat_id": parent_chat_id,
            "child_chat_id": kwargs["child_chat_id"],
            "status": "queued",
            "run_generation": 1,
            "outcome": {
                "schema": "variant1.child-outcome.v1",
                "status": "unreported",
                "basis": None,
                "independently_verified": False,
            },
            "artifact_ref": "",
            "error": "",
        }
        self.rows[row["child_id"]] = row
        return dict(row)

    def inspect(self, parent_chat_id, child_id):
        row = self.rows[child_id]
        if row["parent_chat_id"] != parent_chat_id:
            raise LookupError("unknown child handle")
        return dict(row)

    async def restart(
        self, parent_chat_id, child_id, *, expected_generation=None,
        request_id="", message="",
    ):
        row = self.rows[child_id]
        if row["parent_chat_id"] != parent_chat_id:
            raise LookupError("unknown child handle")
        prior = self.restart_requests.get(request_id)
        if prior is not None:
            if prior["message"] != message:
                raise RuntimeError("restart request changed")
            return dict(row)
        if expected_generation != row["run_generation"]:
            raise RuntimeError("child generation changed before restart")
        if row["status"] in {"queued", "running"}:
            raise RuntimeError("child is already active")
        self.restart_requests[request_id] = {"message": message}
        self.restart_calls.append({
            "child_id": child_id,
            "expected_generation": expected_generation,
            "request_id": request_id,
            "message": message,
        })
        row["run_generation"] += 1
        row["status"] = "queued"
        row["outcome"] = {
            "schema": "variant1.child-outcome.v1",
            "status": "unreported",
            "basis": None,
            "independently_verified": False,
        }
        return dict(row)

    def finish(self, child_id, outcome_status):
        row = self.rows[child_id]
        row["status"] = "completed"
        if outcome_status == "unreported":
            row["outcome"] = {
                "schema": "variant1.child-outcome.v1",
                "status": "unreported",
                "basis": None,
                "independently_verified": False,
            }
        else:
            row["outcome"] = {
                "schema": "variant1.child-outcome.v1",
                "status": outcome_status,
                "summary": f"typed {outcome_status} outcome",
                "evidence_refs": [f"artifact://{outcome_status}"],
                "basis": "agent_report",
                "independently_verified": False,
                "child_id": child_id,
                "generation": row["run_generation"],
                "run_id": f"child-run-{row['run_generation']}",
            }


class NoopExecution:
    pass


class NoopKernel:
    async def interrupt(self, chat_id, *, intent="stop"):
        return {"status": "absent"}

    async def close_chat(self, chat_id, *, reason):
        return False


def _stack(tmp_path):
    artifacts = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    work = WorkService.open(
        str(tmp_path / "work.sqlite3"), worker_id="goal-v4-outcome-test",
    )
    children = TypedChildren()
    runtime = SimpleNamespace(
        catalog=SimpleNamespace(children=children),
        session_artifacts=artifacts,
        execution=NoopExecution(),
        kernel=NoopKernel(),
        coding=None,
    )
    host = SimpleNamespace(
        app_root=str(tmp_path),
        session_artifacts=artifacts,
        require_runtime=lambda: runtime,
    )
    goals = create_goal_service(work, register_work_handler=False)
    handlers = build_goal_host_handlers(host, goals)
    for kind, handler in handlers.handlers().items():
        goals.executor.register(kind, handler)
    for source, resolver in handlers.wait_resolvers().items():
        goals.register_wait_resolver(source, resolver)
    return work, goals, children


def _create_composer_goal(goals):
    goal = goals.create(
        title="Typed objective",
        objective="Complete through a structured child outcome",
        owner_chat_id="parent-chat",
        completion_policy={
            "entrypoint": "composer_goal",
            "completion_basis": "structured_child_report",
            "auto_continue": True,
        },
    )
    goal = goals.plan(
        goal.goal_id,
        [{"step_id": "agent", "kind": "agent", "instructions": "work"}],
        expected_version=goal.version,
    )
    return goals.start(goal.goal_id, expected_version=goal.version, enqueue=False)


async def _dispatch(goals, goal_id):
    result = await goals.supervisor.tick(goal_id)
    wait = goals.repository.list_waits(goal_id, status="pending")[0]
    return result, wait


async def _blocked_goal(goals, children):
    goal = _create_composer_goal(goals)
    _, wait = await _dispatch(goals, goal.goal_id)
    children.finish(wait.matcher["child_id"], "blocked")
    await goals.supervisor.tick(goal.goal_id)
    blocked = goals.get(goal.goal_id)
    assert blocked.status == "blocked"
    return blocked, wait.matcher["child_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome_status", "goal_status"),
    [
        ("completed", "succeeded"),
        ("blocked", "blocked"),
        ("unreported", "blocked"),
    ],
)
async def test_typed_outcome_controls_goal_success(
    tmp_path, outcome_status, goal_status,
):
    work, goals, children = _stack(tmp_path)
    try:
        goal = _create_composer_goal(goals)
        _, wait = await _dispatch(goals, goal.goal_id)
        child_id = wait.matcher["child_id"]
        children.finish(child_id, outcome_status)

        result = await goals.supervisor.tick(goal.goal_id)

        assert result["status"] == goal_status
        assert goals.get(goal.goal_id).status == goal_status
        objective = goals.repository.state_get(
            goal.goal_id, "objective_outcome",
        )
        assert objective["status"] == outcome_status
        assert objective["execution_status"] == "completed"
        assert objective["independently_verified"] is False
        effect = goals.repository.list_effects(goal.goal_id)[0]
        assert effect.status == "succeeded"
    finally:
        await work.shutdown()


@pytest.mark.asyncio
async def test_continuing_reuses_same_child_generation_idempotently(tmp_path):
    work, goals, children = _stack(tmp_path)
    try:
        goal = _create_composer_goal(goals)
        _, wait_one = await _dispatch(goals, goal.goal_id)
        child_id = wait_one.matcher["child_id"]
        children.finish(child_id, "continuing")

        continued = await goals.supervisor.tick(goal.goal_id)
        assert continued["status"] == "running"
        step = goals.repository.get_step(goal.goal_id, "agent")
        assert step.status == "retry_scheduled"

        _, wait_two = await _dispatch(goals, goal.goal_id)
        assert wait_two.matcher["child_id"] == child_id
        assert wait_two.matcher["run_generation"] == 2
        assert len(children.restart_calls) == 1
        assert children.rows[child_id]["outcome"]["status"] == "unreported"

        # Replaying the same effect request cannot create generation 3.
        call = children.restart_calls[0]
        replay = await children.restart(
            "parent-chat",
            child_id,
            expected_generation=call["expected_generation"],
            request_id=call["request_id"],
            message=call["message"],
        )
        assert replay["run_generation"] == 2
        assert len(children.restart_calls) == 1

        children.finish(child_id, "completed")
        final = await goals.supervisor.tick(goal.goal_id)
        assert final["status"] == "succeeded"
    finally:
        await work.shutdown()


@pytest.mark.asyncio
async def test_explicit_continue_reuses_blocked_child_and_guidance(tmp_path):
    work, goals, children = _stack(tmp_path)
    try:
        goal = _create_composer_goal(goals)
        _, wait_one = await _dispatch(goals, goal.goal_id)
        child_id = wait_one.matcher["child_id"]
        children.finish(child_id, "blocked")
        await goals.supervisor.tick(goal.goal_id)
        blocked = goals.get(goal.goal_id)
        assert blocked.status == "blocked"

        composer = ComposerGoals(goals)
        first = await composer.continue_goal(
            "parent-chat",
            goal.goal_id,
            "continue-request-1",
            blocked.version,
            message="The required value is 42.",
        )
        repeated = await composer.continue_goal(
            "parent-chat",
            goal.goal_id,
            "continue-request-1",
            first["goal"]["version"],
            message="The required value is 42.",
        )
        assert repeated["goal"]["goal_id"] == goal.goal_id

        _, wait_two = await _dispatch(goals, goal.goal_id)
        assert wait_two.matcher["child_id"] == child_id
        assert wait_two.matcher["run_generation"] == 2
        assert len(children.restart_calls) == 1
        assert "User follow-up: The required value is 42." in (
            children.restart_calls[0]["message"]
        )
    finally:
        await work.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault_boundary",
    ["after_request", "after_retry", "after_running"],
)
async def test_continue_request_repairs_each_durable_fault_boundary(
    tmp_path, fault_boundary,
):
    work, goals, children = _stack(tmp_path)
    request_id = "continue-fault-window"
    message = "Use the recovered value."
    try:
        blocked, child_id = await _blocked_goal(goals, children)
        step = goals.repository.get_step(blocked.goal_id, "agent")
        prior_attempt = step.attempt_count
        goal = goals.repository.state_set(
            blocked.goal_id,
            "continuation_request",
            {
                "request_id": request_id,
                "message": message,
                "step_id": step.step_id,
                "previous_attempt": prior_attempt,
            },
            expected_version=blocked.version,
            actor=WorkActor("user", "composer"),
            correlation_id=request_id,
        )
        if fault_boundary in {"after_retry", "after_running"}:
            goal, _ = goals.repository.retry_step(
                goal.goal_id,
                step.step_id,
                expected_version=goal.version,
                continuation=True,
                actor=WorkActor("user", "composer"),
                correlation_id=request_id,
            )
        if fault_boundary == "after_running":
            goal = goals.repository.transition_goal(
                goal.goal_id,
                "running",
                expected_version=goal.version,
                actor=WorkActor("user", "composer"),
                correlation_id=request_id,
            )

        composer = ComposerGoals(goals)
        staged = composer.snapshot(blocked.goal_id)
        assert staged['continuation_admission']['status'] == 'staged'
        assert staged['continuation_admission']['request_id'] == request_id
        recovered = await composer.continue_goal(
            "parent-chat",
            blocked.goal_id,
            request_id,
            goals.get(blocked.goal_id).version,
            message=message,
        )

        assert recovered["goal"]["status"] == "running"
        repaired = goals.repository.get_step(blocked.goal_id, "agent")
        assert repaired.status == "retry_scheduled"
        assert repaired.attempt_count == prior_attempt
        assert children.rows[child_id]["run_generation"] == 1
        job = work.jobs.get_by_idempotency(
            owner_kind="goal",
            owner_id=blocked.goal_id,
            kind="goal.supervise.v1",
            idempotency_key=(
                f"goal-supervise:{blocked.goal_id}:continue:{request_id}"
            ),
        )
        assert job is not None
        assert recovered['continuation_admission'] == {
            'request_id': request_id, 'step_id': step.step_id,
            'previous_attempt': prior_attempt, 'status': 'admitted',
            'basis': 'work_job', 'job_id': job.job_id, 'job_status': job.status,
        }

        # A second replay is a no-op repair of the same durable occurrence.
        again = await composer.continue_goal(
            "parent-chat",
            blocked.goal_id,
            request_id,
            recovered["goal"]["version"],
            message=message,
        )
        assert again["goal"]["goal_id"] == blocked.goal_id
        assert goals.repository.get_step(
            blocked.goal_id, "agent",
        ).attempt_count == prior_attempt
    finally:
        await work.shutdown()


@pytest.mark.asyncio
async def test_restart_then_effect_commit_failure_replays_same_request(
    tmp_path, monkeypatch,
):
    work, goals, children = _stack(tmp_path)
    try:
        goal = _create_composer_goal(goals)
        _, first_wait = await _dispatch(goals, goal.goal_id)
        child_id = first_wait.matcher["child_id"]
        children.finish(child_id, "continuing")
        await goals.supervisor.tick(goal.goal_id)

        current = goals.get(goal.goal_id)
        current = await goals.supervisor._prepare_ready(current, time.time())
        step = goals.repository.get_step(goal.goal_id, "agent")
        assert step.status == "ready"
        current, leased, attempt = goals.repository.lease_step(
            goal.goal_id,
            step.step_id,
            expected_version=current.version,
            lease_owner=goals.supervisor.worker_id,
            actor=WorkActor("system", goals.supervisor.worker_id),
            lease_ttl_s=goals.supervisor.step_lease_ttl_s,
        )
        current, running, attempt = goals.repository.start_step(
            goal.goal_id,
            step.step_id,
            expected_version=current.version,
            lease_owner=leased.lease_owner,
            lease_epoch=leased.lease_epoch,
            actor=WorkActor("system", goals.supervisor.worker_id),
        )
        context = StepExecutionContext(
            goal=current,
            step=running,
            attempt=attempt,
            scope={},
            cancellation_requested=lambda: False,
        )
        handler = goals.executor._handlers["agent"].__self__
        original_update = handler._effect_update
        failed_once = False

        def fail_after_restart(effect, status, **kwargs):
            nonlocal failed_once
            if not failed_once and status == "dispatched":
                failed_once = True
                raise RuntimeError("injected crash before effect commit")
            return original_update(effect, status, **kwargs)

        monkeypatch.setattr(handler, "_effect_update", fail_after_restart)
        with pytest.raises(RuntimeError, match="injected crash"):
            await handler._spawn_child(context, source="agent")
        # The dispatched receipt now precedes the external restart effect.
        assert children.rows[child_id]["run_generation"] == 1
        assert len(children.restart_calls) == 0
        planned = [
            effect for effect in goals.repository.list_effects(goal.goal_id)
            if effect.attempt == 2
        ]
        assert len(planned) == 1 and planned[0].status == "planned"
        persisted_request = dict(planned[0].request)

        monkeypatch.setattr(handler, "_effect_update", original_update)
        replayed = await handler._spawn_child(context, source="agent")

        assert replayed.status == "waiting"
        assert children.rows[child_id]["run_generation"] == 2
        assert len(children.restart_calls) == 1
        committed = [
            effect for effect in goals.repository.list_effects(goal.goal_id)
            if effect.attempt == 2
        ][0]
        assert committed.status == "dispatched"
        assert dict(committed.request) == persisted_request
    finally:
        await work.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['lookup_failed', 'wrong_owner'])
async def test_continuation_projection_does_not_invent_admission(tmp_path, monkeypatch, failure):
    from work_fabric.scope import WorkScope
    work, goals, children = _stack(tmp_path)
    try:
        blocked, _ = await _blocked_goal(goals, children)
        step = goals.repository.get_step(blocked.goal_id, 'agent')
        goals.repository.state_set(blocked.goal_id, 'continuation_request', {
            'request_id': 'pending-guidance', 'message': 'Keep this text',
            'step_id': step.step_id, 'previous_attempt': step.attempt_count,
        }, expected_version=blocked.version)
        def lookup(**kwargs):
            if failure == 'lookup_failed':
                raise OSError('simulated read failure')
            return SimpleNamespace(scope=WorkScope(chat_id='another-chat'),
                                   input_manifest={'goal_id': blocked.goal_id})
        monkeypatch.setattr(work.jobs, 'get_by_idempotency', lookup)
        snapshot = ComposerGoals(goals).snapshot(blocked.goal_id)
        assert snapshot['goal']['status'] == 'blocked'
        assert snapshot['continuation_admission']['status'] == 'unknown'
        assert snapshot['continuation_admission']['job_id'] is None
        assert snapshot['state']['continuation_request']['message'] == 'Keep this text'
    finally:
        await work.shutdown()
