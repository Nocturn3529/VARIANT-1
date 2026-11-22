"""Crash, ownership and scheduling boundaries of an ordinary Goal turn."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from chat_goals import ChatGoalService, ChatGoalStore, GoalConflict, GoalNotFound
from host_chat_service import launch_reserved_chat_turn
from run_context import Variant1RunContext, bind_run_context, current_run_context
from tests.test_chat_goals import Host, _service
from tests.test_session_runtime import _runtime
from work_fabric.repository import CHAT_GOAL_MIGRATION_VERSION


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["complete", "blocked", "paused"])
async def test_late_goal_reply_cannot_change_an_edited_objective(tmp_path, status):
    host, service = _service(tmp_path)
    goal = await service.submit("owner", "initial", "First objective")
    turn = host.launched[0]
    invocation = SimpleNamespace(run_id=turn.run.run_id, work_scope=turn.run.work_scope)
    try:
        with bind_run_context(turn.run):
            assert service.take_notes("owner", goal["goal_id"]) == []
            await service.control("owner", goal["goal_id"], "edit", objective="Second objective")
            assert service.tool_get("owner")["goal"]["objective"] == "Second objective"
            with pytest.raises(GoalConflict, match="objective"):
                service.tool_update("owner", status, context=invocation)
            assert service.current("owner")["status"] == "active"
            assert not service.completion_event("owner", turn.run.run_id)
            notes = service.take_notes("owner", goal["goal_id"])
            assert len(notes) == 1 and "Second objective" in notes[0]
            service.store.apply_token_delta(goal["goal_id"], 13)
            updated = service.tool_update("owner", status, context=invocation)
            assert updated["goal"]["status"] == status
            assert updated["tokens_used"] == 13
    finally:
        await service.control("owner", goal["goal_id"], "clear")
        turn.done.set_result(None)
        await turn.task


@pytest.mark.asyncio
async def test_resume_cannot_launch_work_after_a_paused_goal_exhausts_its_budget(tmp_path):
    host, service = _service(tmp_path)
    goal = service.tool_create("owner", "Bounded work", token_budget=5)["goal"]
    goal_id = goal["goal_id"]
    usage = service.request_usage({"run": {"work_scope": {"chat_id": "owner", "goal_id": goal_id}}})
    await service.control("owner", goal_id, "pause")
    usage.settle({"input_tokens": 10})
    assert service.current("owner")["status"] == "paused"
    resumed = await service.control("owner", goal_id, "resume")
    assert resumed["status"] == "budget_limited" and resumed["tokens_used"] == 10
    assert not host.launched
    usage.settle({"input_tokens": 3})
    assert service.current("owner")["status"] == "paused"
    assert (await service.control("owner", goal_id, "resume"))["status"] == "active"
    assert len(host.launched) == 1
    await service.control("owner", goal_id, "clear")
    host.launched[0].done.set_result(None)
    await host.launched[0].task


@pytest.mark.parametrize("submission", [True, False])
def test_an_already_active_exhausted_legacy_goal_cannot_admit_a_request(tmp_path, submission):
    store = ChatGoalStore(str(tmp_path / "work.sqlite"))
    goal, _ = store.create("owner", "bounded", token_budget=5, creator_run_id=None if submission else "first")
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE chat_goal SET tokens_used=5 WHERE goal_id=?", (goal.goal_id,))
    assert not store.admit_turn("owner", goal.goal_id, "unexpected", submission=submission)
    limited = store.current("owner")
    assert limited.status == "budget_limited" and limited.active_since is None
    assert limited.submission_pending == submission and limited.first_run_id == goal.first_run_id


def test_objective_revision_survives_upgrade_aba_edits_and_accounting(tmp_path):
    path = str(tmp_path / "work.sqlite")
    store = ChatGoalStore(path)
    goal, _ = store.create("owner", "A", token_budget=25)
    store.apply_token_delta(goal.goal_id, 3)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE chat_goal DROP COLUMN objective_revision")
    store = ChatGoalStore(path)
    original = store.current("owner")
    assert (original.objective, original.tokens_used, original.status) == ("A", 3, "active")
    first = store.edit(goal.goal_id, "B")
    latest = store.edit(goal.goal_id, "A")
    assert original.objective_revision < first.objective_revision < latest.objective_revision
    store.edit(goal.goal_id, "A")
    store.apply_token_delta(goal.goal_id, 4)
    with pytest.raises(GoalConflict, match="objective"):
        store.transition(goal.goal_id, "complete", expected_objective_revision=original.objective_revision)
    complete = store.transition(goal.goal_id, "complete", expected_objective_revision=latest.objective_revision)
    assert complete.tokens_used == 7 and complete.status == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_resumed_pending_goal_call_uses_the_request_checkpoint(tmp_path, legacy):
    from agent_engine.presets import chat_task_default
    from agent_engine.runner import run_main_chat_task
    from agent_engine.sqlite_snapshot_store import SQLiteRunSnapshotStore
    from agent_types import ToolBatchResult
    from assistant_turn import AssistantTurn
    from tests.test_agent_engine import FakePorts, SPEC

    _host, service = _service(tmp_path)
    goal = service.tool_create("owner", "Objective A")["goal"]
    checkpoint = SQLiteRunSnapshotStore(str(tmp_path / "snapshot.sqlite"))
    actions = [AssistantTurn(tool_calls=({"tool": "ipython", "id": name,
               "args": {"code": "session.update_goal('complete')"}},)) for name in ("old", "fresh")]
    model = FakePorts([*actions, AssistantTurn(text="done")])
    ports = model.build()
    ports.loop.drain_notes = lambda: service.take_notes("owner", goal["goal_id"])
    effects = []

    async def run_actions(batch):
        ctx = current_run_context()
        call = batch[0]
        invocation = SimpleNamespace(run_id=ctx.run_id, work_scope=ctx.work_scope)
        try:
            service.tool_update("owner", "complete", context=invocation)
            outcome = "complete"
        except GoalConflict:
            outcome = "stale objective"
        effects.append((call["id"], outcome))
        return ToolBatchResult(text=outcome, executed=True, outcomes=[{
            "tool": "ipython", "call_id": call["id"], "model_result": outcome, "ok": outcome == "complete",
        }])

    ports.loop.run_actions = run_actions
    options = dict(config=chat_task_default().with_overrides(checkpoints=True), text="Help with this goal",
                   base_system="system", full_tspec=[SPEC], convo_tail=[], images=[], ports=ports)

    class Saved(Exception):
        pass

    async def interrupt(completed, next_node, state):
        if completed == "model_step" and next_node == "tool":
            observation = state["main"]["loop"]["goal_observation"]
            assert observation == {"goal_id": goal["goal_id"], "objective_revision": 1}
            if legacy:
                state["main"]["loop"].pop("goal_observation")
            checkpoint.commit_boundary_sync(state, completed_node=completed, next_node=next_node,
                                            expected_head_sequence=None)
            raise Saved(state["thread_id"])

    async def run(admission, **kwargs):
        ctx = Variant1RunContext.create(source="chat", session_id="owner", work_scope={"chat_id": "owner"})
        assert service.admit_chat_turn(ctx, "owner", admission, "chat")
        with bind_run_context(ctx):
            return await run_main_chat_task(**options, **kwargs)

    with pytest.raises(Saved) as saved:
        await run("first", is_resume=False, resume_snap=None, snapshot_store=checkpoint, commit=interrupt)
    await service.control("owner", goal["goal_id"], "edit", objective="Objective B")
    # Reopening the host also loses the transient edit note.
    service = ChatGoalService(_host, ChatGoalStore(service.store.path))
    resumed = await run("second", is_resume=True, resume_snap={"thread_id": saved.value.args[0]},
                        snapshot_store=SQLiteRunSnapshotStore(checkpoint.path))
    assert effects == [("old", "stale objective"), ("fresh", "complete")]
    assert any("Objective B" in row.get("content", "") for row in model.stream_messages[1])
    assert resumed.loop_result.reply == "done" and service.current("owner")["status"] == "complete"


@pytest.mark.asyncio
async def test_task_completion_rechecks_early_idle_and_shutdown_fences_successor(tmp_path):
    registry, _ = _runtime(tmp_path)
    host = Host()
    host.runtime.session_runtimes = registry
    service = ChatGoalService(host, ChatGoalStore(str(tmp_path / "work.sqlite")))
    registry.register_idle_listener(service.notify_idle)
    entered = asyncio.Queue()
    allow_settlement = asyncio.Event()
    tasks = []

    async def run(_transport, _text, _session, *, runtime_admission_id, source, **_kwargs):
        ctx = current_run_context()
        registry.begin_run(runtime_admission_id, run_id=ctx.run_id, thread_id=ctx.run_id, source=source)
        assert service.admit_chat_turn(ctx, "owner", runtime_admission_id, source)
        await entered.put(source)
        if source == "goal:continue":
            await allow_settlement.wait()
        registry.finish_run(runtime_admission_id, status="ok")
        # Production emits idle before awaiting terminal publication. The
        # callback must run now, while this task is still registered.
        await asyncio.sleep(0)

    def launch(ws, text, session, **kwargs):
        task = launch_reserved_chat_turn(host, run, ws, text, session, **kwargs)
        tasks.append(task)
        return task

    host.runtime.chat.launch_reserved_turn = launch
    await service.submit("owner", "request", "Finish")
    assert await asyncio.wait_for(entered.get(), 2) == "goal:submit"
    assert await asyncio.wait_for(entered.get(), 2) == "goal:continue"
    await service.shutdown()
    allow_settlement.set()
    await asyncio.gather(*tasks)
    assert entered.empty()
    assert service.current("owner")["status"] == "active"
    assert not await service.continue_if_idle("owner")
    await registry.shutdown()


@pytest.mark.asyncio
async def test_first_submission_only_consumed_at_actual_admission(tmp_path):
    host, service = _service(tmp_path)
    # Hold the scheduled coroutine before its normal chat admission, like
    # shutting down after the save but before the coroutine gets CPU time.
    future = asyncio.get_running_loop().create_future()
    host.runtime.chat.launch_reserved_turn = lambda *args, **kwargs: future
    first = await service.submit("owner", "request", "First turn")
    assert first["submission_pending"] and not service.store.get(first["goal_id"]).first_run_id
    service._closed = True
    future.cancel()
    restarted = Host()
    recovered = ChatGoalService(restarted, ChatGoalStore(service.store.path))
    restarted.runtime.goals = recovered
    await recovered.start()
    assert [turn.source for turn in restarted.launched] == ["goal:submit"]
    assert not recovered.current("owner")["submission_pending"]
    assert recovered.store.get(first["goal_id"]).first_run_id == restarted.launched[0].run.run_id
    assert not recovered.store.admit_turn("owner", first["goal_id"], "second-attempt", submission=True)
    repeated = await recovered.submit("owner", "request", "First turn")
    assert repeated["goal_id"] == first["goal_id"] and len(restarted.launched) == 1
    recovered.pause_for_stop("owner")
    restarted.launched[0].done.set_result(None)
    await restarted.launched[0].task


@pytest.mark.parametrize("obstacle", ["queued_user", "retiring_kernel"])
@pytest.mark.asyncio
async def test_idle_submission_wakes_after_last_obstacle_clears(tmp_path, obstacle):
    registry, _ = _runtime(tmp_path)
    host, service = _service(tmp_path)
    host.runtime.session_runtimes = registry
    registry.register_idle_listener(service.notify_idle)
    registry.ensure_runtime("owner")
    if obstacle == "queued_user":
        ticket = registry.enqueue_input("owner", "User goes first", delivery="follow_up", source="user")
        goal = await service.submit("owner", "request", "Finish")
        assert goal["submission_pending"] and not host.launched
        registry.remove_queued_ticket("owner", ticket.ticket_id, expected_revision=registry.queue_snapshot("owner")["revision"])
    else:
        with registry.claim_kernel_retirement("owner") as claimed:
            assert claimed
            goal = await service.submit("owner", "request", "Finish")
            assert goal["submission_pending"] and not host.launched
    await asyncio.sleep(0)
    assert [turn.source for turn in host.launched] == ["goal:submit"]
    service.pause_for_stop("owner")
    await service.shutdown()
    host.launched[0].done.set_result(None)
    await host.launched[0].task
    await registry.shutdown()


@pytest.mark.parametrize(("source", "kind", "expected"), [
    ("user", "quota", "usage_limited"), ("peer:request", "rate_limit", "blocked"),
])
@pytest.mark.asyncio
async def test_ordinary_goal_work_obeys_provider_failures(tmp_path, source, kind, expected):
    host, service = _service(tmp_path)
    goal, _ = service.store.create("owner", "A standing goal")
    ctx = Variant1RunContext.create(source="chat", work_scope={"chat_id": "owner"})
    done = asyncio.get_running_loop().create_future()
    service.admit_chat_turn(ctx, "owner", "ordinary", source, task=done)
    assert ctx.work_scope.goal_id == goal.goal_id
    ctx.metadata.update(_terminal_status="error", _provider_failure_kind=kind)
    done.set_result(None)
    await asyncio.sleep(0)
    assert service.store.current("owner").status == expected
    assert not host.launched


@pytest.mark.parametrize("next_action", ["resume", "replace"])
@pytest.mark.asyncio
async def test_explicit_new_work_survives_a_predecessor_still_settling_stop(tmp_path, next_action):
    host, service = _service(tmp_path)
    original = await service.submit("owner", "first", "First")
    old = host.launched[0]
    service.pause_for_stop("owner")
    if next_action == "resume":
        next_goal = await service.control("owner", original["goal_id"], "resume")
    else:
        next_goal = await service.submit("owner", "second", "Second", replace=True, expected_goal_id=original["goal_id"])
    assert len(host.launched) == 1
    old.task.cancel()
    await asyncio.gather(old.task, return_exceptions=True)
    await asyncio.sleep(0)
    assert len(host.launched) == 2
    assert host.launched[1].run.work_scope.goal_id == next_goal["goal_id"]
    assert host.launched[1].source == ("goal:continue" if next_action == "resume" else "goal:submit")
    service.pause_for_stop("owner")
    host.launched[1].done.set_result(None)
    await host.launched[1].task


@pytest.mark.asyncio
async def test_replacement_is_fenced_and_replay_never_changes_a_different_goal(tmp_path):
    host, service = _service(tmp_path)
    host.busy = True
    a = await service.submit("owner", "a", "Original")
    await service.control("owner", a["goal_id"], "edit", objective="Edited")
    assert (await service.submit("owner", "a", "Original"))["objective"] == "Edited"
    with pytest.raises(GoalConflict):
        await service.submit("owner", "b", "Replacement", replace=True)
    b = await service.submit("owner", "b", "Replacement", replace=True, expected_goal_id=a["goal_id"])
    assert (await service.submit("owner", "b", "Replacement", replace=True,
                                 expected_goal_id=a["goal_id"]))["goal_id"] == b["goal_id"]
    with pytest.raises(GoalConflict):
        await service.submit("owner", "c", "Unexpected", replace=True, expected_goal_id=a["goal_id"])
    with pytest.raises(GoalConflict):
        service.tool_update("owner", "complete", context=SimpleNamespace(
            run_id="a-run", work_scope=SimpleNamespace(goal_id=a["goal_id"])))
    assert (await service.submit("owner", "a", "Original"))["goal_id"] == b["goal_id"]
    assert service.current("owner")["status"] == "active"
    await service.control("owner", b["goal_id"], "clear")
    assert await service.submit("owner", "a", "Original") is None
    with pytest.raises(GoalNotFound):
        await service.submit("owner", "c", "Unexpected", replace=True, expected_goal_id=b["goal_id"])


def test_legacy_upgrade_is_atomic_and_migrates_the_current_objective(tmp_path):
    path = str(tmp_path / "work.sqlite")
    store = ChatGoalStore(path)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE workflow_goal(goal_id TEXT,owner_chat_id TEXT,objective TEXT,status TEXT,"
                     "completion_policy_json TEXT,updated_at REAL)")
        for goal_id, chat, status, stamp in [("old", "chat", "succeeded", 1), ("live", "chat", "running", 2),
                                              ("other", "second", "running", 3)]:
            conn.execute("INSERT INTO workflow_goal VALUES(?,?,?,?,?,?)", (goal_id, chat, goal_id, status,
                         json.dumps({"entrypoint": "composer_goal", "submission_request_id": "submit-" + goal_id}), stamp))
        conn.execute("CREATE TRIGGER interrupted BEFORE INSERT ON chat_goal WHEN new.goal_id='other' "
                     "BEGIN SELECT RAISE(ABORT,'interrupted migration'); END")
    with pytest.raises(sqlite3.IntegrityError, match="interrupted migration"):
        store.migrate_legacy_goals()
    assert store.current("chat") is None and not store.active_chat_ids()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT 1 FROM work_schema_migration WHERE version=?",
                            (CHAT_GOAL_MIGRATION_VERSION,)).fetchone() is None
        conn.execute("DROP TRIGGER interrupted")
    assert store.migrate_legacy_goals() == 2
    saved = store.current("chat")
    assert (saved.goal_id, saved.status, saved.request_id) == ("live", "paused", "submit-live")
    assert not saved.submission_pending and not store.active_chat_ids()
    assert store.migrate_legacy_goals() == 0
