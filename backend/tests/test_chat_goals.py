"""Chat goals: one standing goal per chat, continued as ordinary turns while idle."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from chat_goals import (
    ChatGoalService,
    ChatGoalStore,
    GoalConflict,
    GoalValidationError,
    GoalVersionConflict,
    goal_tokens,
)
from chat_goals.events import clean_goal_event
from run_context import Variant1RunContext, bind_run_context


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _store(tmp_path, clock=None):
    return ChatGoalStore(str(tmp_path / "work.sqlite3"), clock=clock or Clock())


# ── store ──────────────────────────────────────────────────────────────────
def test_one_current_goal_per_chat_with_idempotent_submission(tmp_path):
    store = _store(tmp_path)
    goal, created = store.create("chat", "Ship it", request_id="r1")
    assert created and goal.status == "active" and goal.submission_pending
    again, created = store.create("chat", "Ship it", request_id="r1")
    assert not created and again.goal_id == goal.goal_id
    with pytest.raises(GoalConflict):
        store.create("chat", "Something else", request_id="r1")
    with pytest.raises(GoalConflict):
        store.create("chat", "Second goal", request_id="r2")
    replaced, _ = store.create("chat", "Second goal", request_id="r2", replace=True, expected_goal_id=goal.goal_id)
    assert store.current("chat").goal_id == replaced.goal_id
    store.transition(replaced.goal_id, "complete")
    # A complete goal is replaced without confirmation.
    third, created = store.create("chat", "Third goal", request_id="r3")
    assert created and store.current("chat").goal_id == third.goal_id
    with pytest.raises(GoalValidationError):
        store.create("other", "   ", request_id="r4")


def test_token_budget_flips_once_and_late_usage_does_not_reactivate_a_paused_goal(tmp_path):
    store = _store(tmp_path)
    goal, _ = store.create("chat", "Do work", token_budget=100)
    updated, reached = store.apply_token_delta(goal.goal_id, 60)
    assert updated.tokens_used == 60 and not reached and updated.status == "active"
    updated, reached = store.apply_token_delta(goal.goal_id, 50)
    assert reached and updated.status == "budget_limited" and updated.tokens_used == 110
    updated, reached = store.apply_token_delta(goal.goal_id, 5)
    assert not reached and updated.tokens_used == 115
    other, _ = store.create("chat2", "Other")
    store.transition(other.goal_id, "paused")
    late, reached = store.apply_token_delta(other.goal_id, 10)
    assert late.status == "paused" and late.tokens_used == 10 and not reached


def test_time_counts_only_while_active_and_edit_rules(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    goal, _ = store.create("chat", "Do work")
    clock.now += 30
    paused = store.transition(goal.goal_id, "paused")
    clock.now += 100
    assert paused.time_used(clock.now) == 30
    active = store.transition(goal.goal_id, "active")
    clock.now += 5
    assert active.time_used(clock.now) == 35
    with pytest.raises(GoalVersionConflict):
        store.edit(goal.goal_id, "New text", expected_version=1)
    edited = store.edit(goal.goal_id, "New text", expected_version=active.version)
    assert edited.objective == "New text"
    done = store.transition(goal.goal_id, "complete", run_id="run-9")
    assert done.completed_run_id == "run-9" and done.completed_at == clock.now
    with pytest.raises(GoalConflict):
        store.edit(goal.goal_id, "Again")


def test_goal_tokens_are_uncached_input_plus_output():
    assert goal_tokens({"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 7}) == 47
    assert goal_tokens({"input_tokens": None, "output_tokens": "3"}) == 3


# ── service ────────────────────────────────────────────────────────────────
class Transport:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


class Host:
    """Just enough host for goal turns: admission, launch and run contexts."""

    def __init__(self):
        self.busy = False
        self.launched = []
        self.receipts = {}
        self.transport = Transport()
        self.archived = False
        registry = SimpleNamespace(
            try_reserve_run=self._reserve, finish_run=lambda *a, **k: None,
            attached_transports=lambda chat_id: [self.transport])
        sessions = SimpleNamespace(
            get_session_metadata=lambda chat_id: {"archived": self.archived},
            get_last_run_receipt=lambda chat_id: self.receipts.get(chat_id, {}))
        chat = SimpleNamespace(launch_reserved_turn=self._launch)
        self.runtime = SimpleNamespace(session_runtimes=registry, sessions=sessions, chat=chat)
        self.runtime.goals = None

    def require_runtime(self):
        return self.runtime

    def _reserve(self, chat_id, require_empty_queue=False):
        return "" if self.busy else f"adm-{len(self.launched) + 1}"

    def make_run_context(self, source, title, **kwargs):
        metadata = dict(kwargs.get("metadata") or {})
        return Variant1RunContext.create(source=source, metadata=metadata,
                                         work_scope=metadata.get("work_scope"))

    def _launch(self, transport, text, session, *, runtime_admission_id, source, task_name):
        from run_context import current_run_context
        done = asyncio.get_running_loop().create_future()
        chat_id = session.viewed_session_id
        assert self.runtime.goals.admit_chat_turn(current_run_context(), chat_id, runtime_admission_id, source)

        async def turn():
            await done
        task = asyncio.get_running_loop().create_task(turn())
        self.launched.append(SimpleNamespace(text=text, source=source, run=current_run_context(),
                                             done=done, task=task))
        return task


async def _finish(host, service, chat_id, *, metadata=None, receipt=None, error=None):
    turn = host.launched[-1]
    turn.run.metadata.update(metadata or {})
    host.receipts[chat_id] = {"run_id": turn.run.run_id, **(receipt or {})}
    if error is not None:
        turn.done.set_exception(error)
    else:
        turn.done.set_result(None)
    try:
        await turn.task
    except Exception:
        pass
    await asyncio.sleep(0)
    # Let the production task completion callback hand the chat to the next turn.
    await asyncio.sleep(0)


def _service(tmp_path):
    host = Host()
    service = ChatGoalService(host, _store(tmp_path))
    host.runtime.goals = service
    return host, service


@pytest.mark.asyncio
async def test_submit_starts_a_goal_turn_and_idle_continues_it(tmp_path):
    host, service = _service(tmp_path)
    goal = await service.submit("chat", "req-1", "Make the tests pass")
    assert len(host.launched) == 1
    first = host.launched[0]
    assert first.source == "goal:submit"
    assert "standing goal" in first.text and "Make the tests pass" not in first.text
    assert first.run.metadata["goal_display_text"] == "Make the tests pass"
    assert first.run.work_scope.goal_id == goal["goal_id"]
    assert service.current("chat")["continuing"] is True
    assert service.current("chat")["submission_pending"] is False
    # Only one goal turn at a time.
    assert not await service.continue_if_idle("chat")
    await _finish(host, service, "chat", metadata={"_terminal_status": "ok"})
    assert [turn.source for turn in host.launched] == ["goal:submit", "goal:continue"]
    assert host.launched[1].run.metadata["goal_display_text"] == "Continue toward the goal"


@pytest.mark.asyncio
async def test_user_input_and_running_turns_go_first(tmp_path):
    host, service = _service(tmp_path)
    host.busy = True
    goal = await service.submit("chat", "req-1", "Objective")
    assert host.launched == [] and goal["submission_pending"] is True
    host.busy = False
    assert await service.continue_if_idle("chat")
    # The deferred first turn is still the submission turn.
    assert host.launched[0].source == "goal:submit"


@pytest.mark.asyncio
async def test_stop_pauses_and_resume_continues(tmp_path):
    host, service = _service(tmp_path)
    goal = await service.submit("chat", "req-1", "Objective")
    service.pause_for_stop("chat")
    assert service.current("chat")["status"] == "paused"
    await _finish(host, service, "chat", metadata={"_terminal_status": "cancelled"})
    assert len(host.launched) == 1
    resumed = await service.control("chat", goal["goal_id"], "resume")
    assert resumed["status"] == "active" and len(host.launched) == 2
    with pytest.raises(GoalConflict):
        service.store.transition(goal["goal_id"], "budget_limited")
        await service.control("chat", goal["goal_id"], "resume")


@pytest.mark.asyncio
async def test_three_empty_continuation_turns_block_the_goal(tmp_path):
    host, service = _service(tmp_path)
    await service.submit("chat", "req-1", "Objective")
    empty = {"_terminal_status": "ok", "_terminal_reply_empty": True}
    # The user's own submission turn never counts as an empty goal turn.
    await _finish(host, service, "chat", metadata=empty, receipt={"tool_calls": 0})
    await _finish(host, service, "chat", metadata=empty, receipt={"tool_calls": 0})
    await _finish(host, service, "chat", metadata=empty, receipt={"tool_calls": 0})
    assert service.current("chat")["status"] == "active"
    await _finish(host, service, "chat", metadata=empty, receipt={"tool_calls": 0})
    goal = service.current("chat")
    assert goal["status"] == "blocked" and "no output" in goal["status_reason"]
    assert len(host.launched) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("successful_receipt", [
    {"tool_calls": 1, "tool_results_by_status": {"ok": 1}}, {"tool_calls": 0},
])
async def test_repeated_failed_execution_blocks_and_success_resets(tmp_path, successful_receipt):
    host, service = _service(tmp_path)
    await service.submit("chat", "req-1", "Objective")
    failed = {"tool_calls": 2, "tool_results_by_status": {"error": 2}}
    await _finish(host, service, "chat", metadata={"_terminal_status": "ok"}, receipt=failed)
    await _finish(host, service, "chat", metadata={"_terminal_status": "ok"}, receipt=failed)
    await _finish(host, service, "chat", metadata={"_terminal_status": "ok"},
                  receipt=successful_receipt)
    await _finish(host, service, "chat", metadata={"_terminal_status": "ok"}, receipt=failed)
    await _finish(host, service, "chat", metadata={"_terminal_status": "ok"}, receipt=failed)
    assert service.current("chat")["status"] == "active"
    await _finish(host, service, "chat", metadata={"_terminal_status": "ok"}, receipt=failed)
    assert service.current("chat")["status"] == "blocked"


@pytest.mark.asyncio
async def test_turn_errors_block_and_quota_errors_usage_limit(tmp_path):
    host, service = _service(tmp_path)
    await service.submit("chat", "req-1", "Objective")
    await _finish(host, service, "chat", metadata={"_terminal_status": "error",
                                                   "_terminal_reason": "model_step_error"})
    assert service.current("chat")["status"] == "blocked"
    (tmp_path / "quota").mkdir()
    host2, service2 = _service(tmp_path / "quota")
    await service2.submit("chat", "req-1", "Objective")
    await _finish(host2, service2, "chat", metadata={
        "_terminal_status": "ok", "_terminal_stop_reason": "error", "_provider_failure_kind": "quota"})
    assert service2.current("chat")["status"] == "usage_limited"
    assert len(host2.launched) == 1


@pytest.mark.asyncio
async def test_usage_charges_the_goal_and_budget_sends_one_wrap_up_note(tmp_path):
    host, service = _service(tmp_path)
    service.tool_create("chat", "Bounded work", token_budget=50)
    goal = service.current("chat")
    assert goal["submission_pending"] is False and goal["token_budget"] == 50
    manifest = {"run": {"work_scope": {"chat_id": "chat", "goal_id": goal["goal_id"]}}}
    service.request_usage(manifest).settle({"input_tokens": 30, "cached_input_tokens": 10, "output_tokens": 5})
    assert service.current("chat")["tokens_used"] == 25
    service.request_usage(manifest).settle({"input_tokens": 40, "output_tokens": 0})
    goal = service.current("chat")
    assert goal["status"] == "budget_limited" and goal["tokens_used"] == 65
    notes = service.take_notes("chat", goal["goal_id"])
    assert len(notes) == 1 and "reached its token budget" in notes[0]
    assert service.take_notes("chat", goal["goal_id"]) == []
    assert not await service.continue_if_idle("chat")


@pytest.mark.asyncio
async def test_model_tools_follow_the_codex_status_rules(tmp_path):
    host, service = _service(tmp_path)
    assert service.tool_get("chat") == {"goal": None}
    service.tool_create("chat", "Write the report")
    with pytest.raises(GoalConflict):
        service.tool_create("chat", "Another goal")
    with pytest.raises(GoalValidationError):
        service.tool_update("chat", "active")
    service.tool_update("chat", "paused")
    with pytest.raises(GoalConflict):
        service.tool_update("chat", "complete")
    await service.control("chat", service.current("chat")["goal_id"], "resume")
    result = service.tool_update("chat", "complete", context=SimpleNamespace(
        run_id="run-7", work_scope=host.launched[-1].run.work_scope))
    assert result["goal"]["status"] == "complete"
    assert result["goal"]["completed_run_id"] == "run-7"
    event = service.completion_event("chat", "run-7")
    assert event["event"] == "achieved" and event["goal_id"] == result["goal"]["goal_id"]
    # A complete goal can be replaced by the model when the user asks again.
    assert service.tool_create("chat", "Next goal")["goal"]["status"] == "active"


@pytest.mark.asyncio
async def test_editing_a_running_goal_notes_the_change_and_publishes(tmp_path):
    host, service = _service(tmp_path)
    goal = await service.submit("chat", "req-1", "First objective")
    edited = await service.control("chat", goal["goal_id"], "edit", objective="Better objective",
                                   expected_version=service.current("chat")["version"])
    assert edited["objective"] == "Better objective"
    notes = service.take_notes("chat", goal["goal_id"])
    assert len(notes) == 1 and "Better objective" in notes[0]
    await asyncio.sleep(0.01)
    updates = [msg for msg in host.transport.sent if msg["type"] == "goal:updated"]
    assert updates and updates[-1]["goal"]["objective"] == "Better objective"
    assert await service.control("chat", goal["goal_id"], "clear") is None
    assert service.current("chat") is None


def test_old_composer_goals_are_carried_over_paused_once(tmp_path):
    path = str(tmp_path / "work.sqlite3")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE workflow_goal(goal_id TEXT, owner_chat_id TEXT, objective TEXT, "
                 "status TEXT, completion_policy_json TEXT, updated_at REAL)")
    composer = json.dumps({"entrypoint": "composer_goal"})
    conn.executemany("INSERT INTO workflow_goal VALUES(?,?,?,?,?,?)", [
        ("g1", "chat-a", "Running goal", "running", composer, 1),
        ("g2", "chat-b", "Done goal", "succeeded", composer, 2),
        ("g3", "chat-c", "Cancelled goal", "cancelled", composer, 3),
        ("g4", "chat-d", "Goals page record", "running", "{}", 4),
    ])
    conn.commit()
    conn.close()
    service = ChatGoalService(SimpleNamespace(), ChatGoalStore(path))
    assert service.migrate_legacy_goals() == 2
    assert service.store.current("chat-a").status == "paused"
    assert not service.store.current("chat-a").submission_pending
    assert service.store.current("chat-b").status == "complete"
    assert service.store.current("chat-c") is None and service.store.current("chat-d") is None
    assert service.migrate_legacy_goals() == 0


# ── websocket and history ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_websocket_commands_use_the_correlated_envelope(tmp_path):
    import ws_goals

    host, service = _service(tmp_path)
    handlers = {}

    def on(*names):
        def register(fn):
            for name in names:
                handlers[name] = fn
            return fn
        return register

    ws_goals.register(on)
    srv = SimpleNamespace(require_runtime=lambda: host.runtime)
    socket = Transport()
    await handlers["goal:submit"](srv, socket, None, {
        "type": "goal:submit", "session_id": "chat", "request_id": "r1", "objective": "Goal one"})
    accepted = socket.sent[-1]
    assert accepted["type"] == "goal:accepted" and accepted["operation"] == "submit"
    goal = accepted["result"]["goal"]
    await handlers["goal:submit"](srv, socket, None, {
        "type": "goal:submit", "session_id": "chat", "request_id": "r2", "objective": "Goal two"})
    assert socket.sent[-1]["type"] == "goal:rejected" and socket.sent[-1]["reason_code"] == "GoalConflict"
    await handlers["goal:current:get"](srv, socket, None, {"session_id": "chat"})
    assert socket.sent[-1]["type"] == "goal:current"
    assert socket.sent[-1]["result"]["goal"]["request_id"] == "r1"
    await handlers["goal:control"](srv, socket, None, {
        "session_id": "chat", "request_id": "c1", "goal_id": goal["goal_id"], "action": "pause",
        "expected_version": 1})
    assert socket.sent[-1]["reason_code"] == "GoalVersionConflict"
    host.archived = True
    await handlers["goal:control"](srv, socket, None, {
        "session_id": "chat", "request_id": "c2", "goal_id": goal["goal_id"], "action": "pause"})
    assert socket.sent[-1]["reason_code"] == "ChatUnavailable"


def test_history_keeps_goal_origins_and_achieved_events():
    from chat_sessions.service import ChatSessionService

    user = ChatSessionService._clean_message({
        "role": "user", "text": "Ship it",
        "origin": {"kind": "goal", "event": "submission", "goal_id": "goal_1"}})
    assert user["metadata"]["origin"] == {"kind": "goal", "event": "submission", "goal_id": "goal_1"}
    bad = ChatSessionService._clean_message({
        "role": "user", "text": "x", "origin": {"kind": "goal", "event": "anything"}})
    assert "origin" not in bad["metadata"]
    assistant = ChatSessionService._clean_message({
        "role": "assistant", "text": "Done", "run_id": "run-1",
        "goal_event": {"event": "achieved", "goal_id": "goal_1", "time_used_s": 12.5,
                       "tokens_used": 900, "extra": "dropped"}})
    assert assistant["metadata"]["goal_event"] == {
        "event": "achieved", "goal_id": "goal_1", "time_used_s": 12.5, "tokens_used": 900}
    assert clean_goal_event({"event": "achieved"}) is None


def test_goal_turn_rows_carry_origin_and_the_achieved_event(tmp_path):
    from chat_finalize import _durable_turn_messages
    _, goals = _service(tmp_path)
    goal, _ = goals.store.create("chat-x", "Achieve")
    goals.store.transition(goal.goal_id, "complete", run_id="run-x")
    session = SimpleNamespace(active=SimpleNamespace(
        transcript_id="", turn_ticket_id="", turn_source="goal:continue", turn_client_id="",
        delivered_inputs=[], runtime_chat_id="chat-x"))
    context = Variant1RunContext.create(source="goal", run_id="run-x", metadata={"goal_id": goal.goal_id})
    with bind_run_context(context):
        rows = _durable_turn_messages(session, "Continue toward the goal", "All done", mood="happy", goals=goals)
    assert rows[0]["origin"] == {"kind": "goal", "event": "continuation", "goal_id": goal.goal_id}
    assert rows[-1]["goal_event"]["goal_id"] == goal.goal_id
    assert goals.completion_event("chat-x", "unrelated-run") is None
    assert goals.completion_event("chat-x", "run-x") == rows[-1]["goal_event"]


@pytest.mark.asyncio
async def test_replacing_a_goal_mid_turn_starts_the_new_goal_when_idle(tmp_path, caplog):
    host, service = _service(tmp_path)
    first = await service.submit("chat", "req-1", "First goal")
    second = await service.submit("chat", "req-2", "Second goal", replace=True, expected_goal_id=first["goal_id"])
    assert second["goal_id"] != first["goal_id"] and second["submission_pending"] is True
    assert len(host.launched) == 1
    await _finish(host, service, "chat", metadata={"_terminal_status": "ok"})
    assert "goal turn evaluation failed" not in caplog.text
    assert host.launched[-1].source == "goal:submit"
    assert host.launched[-1].run.work_scope.goal_id == second["goal_id"]
