import asyncio
from types import SimpleNamespace

import pytest

from goals import create_goal_service
from goals.host_handlers import build_goal_host_handlers
from goals.parent_session import ParentSessionGoals
from host_chat_service import launch_reserved_chat_turn
from run_context import Variant1RunContext, current_run_context
from session_runtime.registry import SessionRuntimeRegistry
from session_runtime.repository import SessionRuntimeRepository
from tools import ToolError
from work_fabric.scope import coerce_work_scope
from work_fabric.service import WorkService


def stack(tmp_path, *, terminal_status='ok'):
    work = WorkService.open(str(tmp_path / "work.sqlite3"), worker_id="parent-goal-test")
    goals = create_goal_service(work)
    registry = SessionRuntimeRegistry(SessionRuntimeRepository(str(tmp_path / "runtime.sqlite3")))
    gate = asyncio.Event()
    runs, receipts, sessions = [], {}, {"owner": {"id": "owner"}}
    runtime = SimpleNamespace(session_runtimes=registry, goals=goals,
        sessions=SimpleNamespace(get_session=sessions.get, get_last_run_receipt=receipts.get))
    host = SimpleNamespace(require_runtime=lambda: runtime)
    def make_context(source, title, **args):
        return Variant1RunContext.create(source=source, title=title, session_id="owner",
            work_scope=coerce_work_scope(args["metadata"]["work_scope"]), metadata=args["metadata"],
            chat_session=args["session"], chat_transport=args["chat_transport"])
    host.make_run_context = make_context
    adapters = build_goal_host_handlers(host, goals)
    parent = ParentSessionGoals(host, goals, adapters)
    registry.register_idle_listener(parent.notify_idle)
    goals.executor.register("agent", adapters.agent)
    async def run_task(transport, text, session, **args):
        run = current_run_context()
        registry.begin_run(args["runtime_admission_id"], run_id=run.run_id, thread_id=run.thread_id, source="goal")
        runs.append((run, text, args))
        await gate.wait()
        receipts["owner"] = {"run_id": run.run_id, "status": terminal_status, "settled": True}
    runtime.chat = SimpleNamespace(launch_reserved_turn=lambda transport, text, session, **args:
        launch_reserved_chat_turn(host, run_task, transport, text, session, **args))
    return work, goals, parent, registry, gate, runs, receipts


async def launch(goals, runs):
    accepted = await goals.composer.submit("owner", "submission", "Build and verify the deliverable")
    goal_id = accepted["goal"]["goal_id"]
    await goals.supervisor.tick(goal_id)
    await asyncio.sleep(0)
    return goal_id


def invocation(run):
    return SimpleNamespace(chat_id="owner", run_id=run.run_id, work_scope=run.work_scope)


async def finish(goals, goal_id, gate, registry):
    task = registry._reservations[registry.active_admission("owner")].task
    gate.set()
    await task
    await asyncio.sleep(0)
    return await goals.supervisor.tick(goal_id)


@pytest.mark.asyncio
async def test_parent_goal_uses_canonical_chat_and_reports_without_spawning_child(tmp_path):
    work, goals, parent, registry, gate, runs, _ = stack(tmp_path)
    goal_id = await launch(goals, runs)
    run, _, args = runs[0]
    assert run.session_id == "owner" and run.work_scope.goal_id == goal_id
    assert args["source"] == "goal" and run.chat_session.viewed_session_id == "owner"
    assert [e.kind for e in goals.repository.list_effects(goal_id)] == ["parent.turn"]
    report = parent.report(invocation(run), status="completed", summary="Verified artifact", evidence_refs=["artifact://verified"])
    assert parent.report(invocation(run), status="completed", summary="Verified artifact", evidence_refs=["artifact://verified"]) == report
    result = await finish(goals, goal_id, gate, registry)
    assert result["status"] == "succeeded"
    assert goals.repository.state_get(goal_id, "objective_outcome")["independently_verified"] is False
    await work.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [("blocked", "blocked"), (None, "blocked"), ("continuing", "running")])
async def test_parent_outcomes_control_continuation_and_prose_never_completes(tmp_path, status, expected):
    work, goals, parent, registry, gate, runs, _ = stack(tmp_path)
    goal_id = await launch(goals, runs)
    if status:
        parent.report(invocation(runs[0][0]), status=status, summary="More work or input")
    assert (await finish(goals, goal_id, gate, registry))["status"] == expected
    assert not registry.is_busy("owner")
    if status == "continuing":
        gate.clear()
        await goals.supervisor.tick(goal_id)
        await asyncio.sleep(0)
        assert len(runs) == 2 and runs[1][0].session_id == runs[0][0].session_id
        parent.report(invocation(runs[1][0]), status="completed", summary="Finished")
        assert (await finish(goals, goal_id, gate, registry))["status"] == "succeeded"
    await work.shutdown()


@pytest.mark.asyncio
async def test_unchanged_parent_wait_does_not_create_successor_poll_jobs(tmp_path):
    work, goals, _, registry, gate, runs, _ = stack(tmp_path)
    goal_id = await launch(goals, runs)
    initial = len(work.jobs.list(owner_kind="goal", owner_id=goal_id))
    for index in range(8):
        await goals.supervisor._work_handler(SimpleNamespace(job=SimpleNamespace(
            job_id=f"probe-{index}", input_manifest={"goal_id": goal_id})))
    assert len(work.jobs.list(owner_kind="goal", owner_id=goal_id)) == initial
    await finish(goals, goal_id, gate, registry)
    await work.shutdown()


@pytest.mark.asyncio
async def test_busy_owner_and_queued_inputs_take_priority_and_idle_event_wakes(tmp_path):
    work, goals, _, registry, gate, runs, _ = stack(tmp_path)
    admission = registry.try_reserve_run("owner")
    ticket = registry.enqueue_input("owner", "User guidance", delivery="follow_up")
    goal_id = await launch(goals, runs)
    assert runs == [] and goals.get(goal_id).status == "waiting_external"
    registry.finish_run(admission, status="ok")
    await goals.supervisor.tick(goal_id)
    assert runs == []
    claimed = registry.claim_input("owner", "follow_up", run_id="user-turn")
    assert claimed["id"] == ticket.ticket_id
    parent_admission = await registry.reserve_run("owner", require_empty_queue=True)
    registry.finish_run(parent_admission, status="ok")
    await goals.supervisor.tick(goal_id)
    await goals.supervisor.tick(goal_id)
    await asyncio.sleep(0)
    assert len(runs) == 1
    await finish(goals, goal_id, gate, registry)
    await work.shutdown()


@pytest.mark.asyncio
async def test_reports_and_cancellation_are_fenced_to_exact_parent_admission(tmp_path):
    work, goals, parent, registry, gate, runs, _ = stack(tmp_path)
    goal_id = await launch(goals, runs)
    run = runs[0][0]
    with pytest.raises(ToolError):
        parent.report(SimpleNamespace(chat_id="other", run_id=run.run_id, work_scope=run.work_scope), status="completed", summary="wrong owner")
    with pytest.raises(ToolError):
        parent.report(SimpleNamespace(chat_id="owner", run_id="old", work_scope=run.work_scope), status="completed", summary="stale run")
    await finish(goals, goal_id, gate, registry)
    other = registry.try_reserve_run("owner")
    task = asyncio.create_task(asyncio.Event().wait())
    registry.bind_admission_task(other, task)
    await parent.cancel(goals.get(goal_id))
    assert not task.cancelled() and not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    registry.finish_run(other, status="test_cleanup")
    await work.shutdown()


@pytest.mark.asyncio
async def test_restart_with_lost_parent_admission_blocks_without_replay(tmp_path):
    work, goals, parent, registry, gate, runs, _ = stack(tmp_path)
    goal_id = await launch(goals, runs)
    wait = goals.repository.list_waits(goal_id, status="pending")[0]
    fresh = SessionRuntimeRegistry(SessionRuntimeRepository(registry.repository.path))
    parent.host.require_runtime().session_runtimes = fresh
    parent._tasks.clear()  # Process-owned task identities do not survive a restart.
    assert "lost its admission" in parent.resolve(wait)["error"]
    assert len(runs) == 1
    parent.host.require_runtime().session_runtimes = registry
    await finish(goals, goal_id, gate, registry)
    await work.shutdown()


@pytest.mark.asyncio
async def test_final_persistence_after_admission_release_is_not_misclassified_as_restart(tmp_path):
    work,goals,parent,registry,gate,runs,_=stack(tmp_path)
    goal_id=await launch(goals,runs)
    wait=goals.repository.list_waits(goal_id,status='pending')[0]
    registry.finish_run(registry.active_admission('owner'),status='ok')
    assert parent.resolve(wait) is None
    gate.set()
    await parent._tasks[goal_id][1]
    await asyncio.sleep(0)
    assert (await goals.supervisor.tick(goal_id))['status']=='blocked'
    await work.shutdown()


@pytest.mark.asyncio
async def test_interrupted_parent_turn_cannot_restart_from_earlier_continuation_claim(tmp_path):
    work,goals,parent,registry,gate,runs,_=stack(tmp_path,terminal_status='cancelled')
    goal_id=await launch(goals,runs)
    parent.report(invocation(runs[0][0]),status='continuing',summary='More work remains')
    assert (await finish(goals,goal_id,gate,registry))['status']=='blocked'
    assert goals.repository.state_get(goal_id,'objective_outcome')['continuation_allowed'] is False
    await goals.supervisor.tick(goal_id)
    assert len(runs)==1
    await work.shutdown()


@pytest.mark.asyncio
async def test_only_correlated_peer_results_wake_a_sleeping_coordinator(tmp_path):
    from peers.repository import PeerRepository
    work, goals, parent, registry, gate, runs, _ = stack(tmp_path)
    repo = PeerRepository(tmp_path / 'peers.sqlite3')
    parent.host.require_runtime().peers = SimpleNamespace(repository=repo)
    request, _ = repo.persist_message({'message_id': 'request-a', 'sender_peer_id': 'chat:owner',
        'target_peer_id': 'chat:collaborator', 'content': 'Verify the deliverable',
        'delivery': 'follow_up', 'message_kind': 'request', 'state': 'queued'})
    goal_id = await launch(goals, runs)
    parent.report(invocation(runs[0][0]), status='continuing', summary='Waiting for review', wait_for_message_ids=['request-a'])
    result = await finish(goals, goal_id, gate, registry)
    assert result['status'] == 'waiting_external'
    before = len(work.jobs.list(owner_kind='goal', owner_id=goal_id))
    parent.notify_peer_result({'message_kind':'notice','in_reply_to':'request-a','target_peer_id':'chat:owner'})
    assert len(work.jobs.list(owner_kind='goal', owner_id=goal_id)) == before
    reply, _ = repo.persist_message({'sender_peer_id':'chat:collaborator','target_peer_id':'chat:owner',
        'content':'Review evidence', 'delivery':'notice','message_kind':'result','in_reply_to':'request-a','state':'received'})
    parent.notify_peer_result(reply)
    assert len(work.jobs.list(owner_kind='goal', owner_id=goal_id)) == before + 1
    assert (await goals.supervisor.tick(goal_id))['status'] == 'running'
    await work.shutdown()


@pytest.mark.asyncio
async def test_physical_request_usage_enforces_goal_token_budget_before_next_turn(tmp_path):
    from model_runtime.usage_ledger import ModelUsageLedger
    from tests.test_model_usage_ledger import request
    work,goals,parent,registry,gate,runs,_=stack(tmp_path)
    ledger=ModelUsageLedger(tmp_path/'usage.sqlite3')
    parent.host.router=SimpleNamespace(_manifest_bus=SimpleNamespace(usage_ledger=ledger))
    goal_id=await launch(goals,runs)
    parent.report(invocation(runs[0][0]),status='continuing',summary='Keep working')
    ledger.record(request(goal=goal_id))
    ledger.patch_usage('mreq-a',{'total_tokens':100})
    goal=goals.get(goal_id)
    with goals.repository.work._write() as conn:
        conn.execute("UPDATE workflow_goal SET budget_limits_json=? WHERE goal_id=?", ('{"tokens":100}',goal_id))
    assert (await finish(goals,goal_id,gate,registry))['status']=='paused'
    assert goals.get(goal_id).budget_usage['tokens']==100
    assert len(runs)==1
    await work.shutdown()
