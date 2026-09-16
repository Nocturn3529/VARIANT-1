from __future__ import annotations

import asyncio
from types import SimpleNamespace
import sys

import pytest

from artifacts import ContentAddressedArtifactStore
from execution_hosts import ExecutionNotFound, create_execution_runtime
from goals import create_goal_service
from goals.host_handlers import build_goal_host_handlers
from work_fabric.service import WorkService


def _stack(tmp_path):
    artifacts = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    work = WorkService.open(str(tmp_path / "work.sqlite3"), worker_id="goal-host-test")
    execution = create_execution_runtime(
        data_dir=str(tmp_path / "runtime"), artifact_store=artifacts,
        backend_instance_id="goal-host-test",
    )
    composed = SimpleNamespace(
        execution=execution,
        coding=None,
        kernel=None,
        catalog=SimpleNamespace(children=None),
        session_artifacts=artifacts,
    )
    host = SimpleNamespace(
        app_root=str(tmp_path),
        session_artifacts=artifacts,
        require_runtime=lambda: composed,
        session_catalog=None,
        kernel_runtime=None,
    )
    goals = create_goal_service(work, register_work_handler=False)
    adapters = build_goal_host_handlers(host, goals)
    goals.register_cancellation_handler(adapters.cancel_goal_resources)
    for kind, handler in adapters.handlers().items():
        goals.executor.register(kind, handler)
    for source, resolver in adapters.wait_resolvers().items():
        goals.register_wait_resolver(source, resolver)
    return work, execution, goals


@pytest.mark.asyncio
async def test_goal_cancel_stops_all_owned_host_resources(tmp_path):
    work, execution, goals = _stack(tmp_path)
    adapter = goals.executor._handlers["child"].__self__
    stopped_processes = []
    interrupted_kernels = []
    cancelled_children = []

    class Processes:
        def get(self, process_id):
            if process_id != "process-live":
                raise ExecutionNotFound(process_id)
            return SimpleNamespace(owner=SimpleNamespace(kind="goal", owner_id=goal.goal_id))

        def stop(self, process_id, *, force):
            stopped_processes.append((process_id, force))

    class Kernel:
        async def interrupt(self, chat_id, *, intent="stop"):
            interrupted_kernels.append((chat_id, intent))
        async def close_chat(self,chat_id,*,reason):
            assert chat_id=='child-chat' and reason=='goal_cancelled'
            return True

    class Children:
        def inspect(self,parent_chat_id,child_id):
            return {'child_id':child_id,'parent_chat_id':parent_chat_id,'child_chat_id':'child-chat','status':'running'}
        def tree(self,*args,**kwargs):return {'items':[],'truncated':False}
        async def cancel(self, parent_chat_id, child_id):
            cancelled_children.append((parent_chat_id, child_id))
            return {**self.inspect(parent_chat_id,child_id),'status':'cancelled'}

    async def stop_process(process_id,*,force):
        stopped_processes.append((process_id,force));return SimpleNamespace(live=False)
    async def delete_execution(chat_id):
        assert chat_id=='child-chat';return 0

    runtime = SimpleNamespace(
        execution=SimpleNamespace(processes=Processes(),stop_process=stop_process,delete_chat=delete_execution,
            repository=SimpleNamespace(live_ids_for_chat=lambda _: ((),()))),
        kernel=Kernel(),
        catalog=SimpleNamespace(children=Children()),
    )
    adapter.host.require_runtime = lambda: runtime

    goal = goals.create(
        title="Cancel resources",
        objective="Stop everything",
        owner_chat_id="parent-chat",
    )
    goal = goals.plan(
        goal.goal_id,
        [
            {"step_id": "process", "kind": "process"},
            {"step_id": "kernel", "kind": "python"},
            {"step_id": "child", "kind": "child"},
        ],
        expected_version=goal.version,
    )

    for step_id, kind, request, response in (
        (
            "process", "process.start", {},
            {"process_id": "process-live"},
        ),
        (
            "kernel", "kernel.execute", {"chat_id": "kernel-chat"},
            {"chat_id": "kernel-chat"},
        ),
        (
            "child", "child.spawn", {},
            {"child_id": "child-live"},
        ),
    ):
        current = goals.get(goal.goal_id)
        effect = goals.repository.record_effect(
            goal.goal_id,
            step_id,
            expected_version=current.version,
            kind=kind,
            idempotency_key=f"cancel:{step_id}",
            request=request,
        )
        current = goals.get(goal.goal_id)
        goals.repository.update_effect(
            effect.effect_id,
            expected_version=current.version,
            status="dispatched",
            response=response,
        )

    try:
        current = goals.get(goal.goal_id)
        cancelled = await goals.cancel_async(
            goal.goal_id,
            expected_version=current.version,
            reason="user stopped goal",
        )

        assert cancelled.status == "cancelled"
        assert stopped_processes == [("process-live", True)]
        assert interrupted_kernels == [("parent-chat", "stop")]
        assert cancelled_children == [("parent-chat", "child-live")]
        assert {
            effect.status for effect in goals.repository.list_effects(goal.goal_id)
        } == {"cancelled"}
    finally:
        execution.shutdown()
        await work.shutdown()


@pytest.mark.asyncio
async def test_process_step_releases_supervisor_and_wakes_to_success(tmp_path):
    work, execution, goals = _stack(tmp_path)
    goal = goals.create(title="Run process", objective="Finish exact command")
    goal = goals.plan(
        goal.goal_id,
        [{
            "step_id": "process",
            "kind": "process",
            "config": {
                "command": [sys.executable, "-c", "print('goal-process-ok')"],
                "cwd": str(tmp_path),
            },
        }],
        expected_version=goal.version,
    )
    goal = goals.start(goal.goal_id, expected_version=goal.version, enqueue=False)
    first = await goals.supervisor.tick(goal.goal_id)
    assert first["status"] == "waiting_external"
    step = goals.repository.get_step(goal.goal_id, "process")
    assert step is not None and step.status == "waiting"
    process_id = goals.repository.list_waits(goal.goal_id, status="pending")[0].matcher[
        "process_id"
    ]
    await asyncio.to_thread(
        execution.processes.wait, process_id, condition="exit", timeout=10
    )
    second = await goals.supervisor.tick(goal.goal_id)
    try:
        assert second["status"] == "succeeded"
        step = goals.repository.get_step(goal.goal_id, "process")
        assert step is not None and step.status == "succeeded"
        assert step.result_ref.startswith("artifact://sha256/")
        assert not goals.repository.list_waits(goal.goal_id, status="pending")
    finally:
        execution.shutdown()
        await work.shutdown()


@pytest.mark.asyncio
async def test_goal_process_defaults_to_owner_chat_project(tmp_path):
    work, execution, goals = _stack(tmp_path)
    project = tmp_path / "bound-project"
    project.mkdir()
    handler = goals.executor._handlers["process"].__self__
    handler.host.require_runtime().sessions = SimpleNamespace(
        get_project=lambda chat_id: (
            {"root": str(project), "name": "bound-project"}
            if chat_id == "chat-project" else None
        ),
    )
    goal = goals.create(
        title="Project process", objective="Use owner project",
        owner_chat_id="chat-project",
    )
    goal = goals.plan(
        goal.goal_id,
        [{
            "step_id": "process",
            "kind": "process",
            "config": {
                "command": [sys.executable, "-c", "print('project-cwd')"],
            },
        }],
        expected_version=goal.version,
    )
    goal = goals.start(goal.goal_id, expected_version=goal.version, enqueue=False)
    first = await goals.supervisor.tick(goal.goal_id)
    try:
        assert first["status"] == "waiting_external"
        effect = goals.repository.list_effects(goal.goal_id)[0]
        assert effect.request["cwd"] == str(project.resolve())
        process = execution.processes.get(effect.response["process_id"])
        assert process.recipe.cwd == str(project.resolve())
        await asyncio.to_thread(
            execution.processes.wait,
            process.process_id,
            condition="exit",
            timeout=10,
        )
        assert (await goals.supervisor.tick(goal.goal_id))["status"] == "succeeded"
    finally:
        execution.shutdown()
        await work.shutdown()


@pytest.mark.asyncio
async def test_nonzero_process_exit_fails_step_without_false_success(tmp_path):
    work, execution, goals = _stack(tmp_path)
    goal = goals.create(title="Fail process", objective="Report failure")
    goal = goals.plan(
        goal.goal_id,
        [{
            "step_id": "process",
            "kind": "process",
            "config": {
                "command": [sys.executable, "-c", "raise SystemExit(7)"],
                "cwd": str(tmp_path),
            },
        }],
        expected_version=goal.version,
    )
    goal = goals.start(goal.goal_id, expected_version=goal.version, enqueue=False)
    await goals.supervisor.tick(goal.goal_id)
    wait = goals.repository.list_waits(goal.goal_id, status="pending")[0]
    await asyncio.to_thread(
        execution.processes.wait,
        wait.matcher["process_id"],
        condition="exit",
        timeout=10,
    )
    result = await goals.supervisor.tick(goal.goal_id)
    try:
        assert result["status"] == "blocked"
        step = goals.repository.get_step(goal.goal_id, "process")
        assert step is not None and step.status == "failed"
        assert step.error
    finally:
        execution.shutdown()
        await work.shutdown()


@pytest.mark.asyncio
async def test_process_owner_reserves_before_goal_effect_projection(tmp_path, monkeypatch):
    work, execution, goals = _stack(tmp_path)
    goal = goals.create(title="Fence process", objective="Fence before launch")
    goal = goals.plan(
        goal.goal_id,
        [{
            "step_id": "process",
            "kind": "process",
            "config": {
                "command": [sys.executable, "-c", "print('fenced')"],
                "cwd": str(tmp_path),
            },
        }],
        expected_version=goal.version,
    )
    goal = goals.start(goal.goal_id, expected_version=goal.version, enqueue=False)
    original = execution.processes.start
    observed = []

    def fenced_start(*args, **kwargs):
        effect = goals.repository.list_effects(goal.goal_id)[0]
        observed.append((effect.status, dict(effect.response), kwargs["process_id"]))
        return original(*args, **kwargs)

    monkeypatch.setattr(execution.processes, "start", fenced_start)
    try:
        result = await goals.supervisor.tick(goal.goal_id)
        assert result["status"] == "waiting_external"
        assert observed and observed[0][0] == "dispatched"
        assert observed[0][1]["process_id"] == observed[0][2]
        effect = goals.repository.list_effects(goal.goal_id)[0]
        assert effect.status == "dispatched"
        assert effect.response["process_id"] == observed[0][2]
    finally:
        execution.shutdown()
        await work.shutdown()


@pytest.mark.asyncio
async def test_child_owner_admits_work_before_goal_effect_projection(tmp_path):
    work, execution, goals = _stack(tmp_path)
    handler = goals.executor._handlers["child"].__self__
    observed = []

    class Children:
        async def spawn(self, parent_chat_id, **kwargs):
            effect = goals.repository.list_effects(goal.goal_id)[0]
            observed.append((effect.status, dict(effect.response), dict(kwargs)))
            return {
                "child_id": kwargs["child_id"],
                "child_chat_id": kwargs["child_chat_id"],
                "status": "queued",
            }

    handler.host.require_runtime().catalog = SimpleNamespace(children=Children())
    goal = goals.create(
        title="Fence child",
        objective="Fence before child launch",
        owner_chat_id="parent-chat",
    )
    goal = goals.plan(
        goal.goal_id,
        [{"step_id": "child", "kind": "child", "instructions": "inspect"}],
        expected_version=goal.version,
    )
    goal = goals.start(goal.goal_id, expected_version=goal.version, enqueue=False)
    try:
        result = await goals.supervisor.tick(goal.goal_id)
        assert result["status"] == "waiting_external"
        assert observed and observed[0][0] == "dispatched"
        assert observed[0][1]["child_id"] == observed[0][2]["child_id"]
        response = goals.repository.list_effects(goal.goal_id)[0].response
        request = observed[0][2]
        assert response["child_id"] == request["child_id"]
        assert response["child_chat_id"] == request["child_chat_id"]
    finally:
        execution.shutdown()
        await work.shutdown()
