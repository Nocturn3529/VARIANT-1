"""Chat goals: one standing objective per chat, continued as ordinary turns while idle.

A goal turn is a normal chat turn whose input is the continuation prompt. When the
chat goes idle and its goal is still active, the next goal turn starts; anything the
user sends runs first. Usage of every turn charges the chat's active goal. The model
finishes, blocks or pauses a goal through ``session.update_goal``; the user pauses,
resumes, edits or clears it. A few automatic stops keep a looping goal from running
away: repeated empty turns, repeated failed execution, a turn error, or a provider
usage limit.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .binding import GoalBinding, turn_binding
from .usage import GoalRequestUsage, goal_tokens
from .prompts import (
    CONTINUATION_DISPLAY,
    budget_reached_note,
    continuation_prompt,
    current_objective_note,
    objective_changed_note,
)
from .store import (
    RESUMABLE,
    ChatGoal,
    ChatGoalStore,
    ChatUnavailable,
    GoalConflict,
    GoalNotFound,
    GoalValidationError,
)

_LOG = logging.getLogger(__name__)

SOURCE_SUBMIT = "goal:submit"
SOURCE_CONTINUE = "goal:continue"
GOAL_SOURCES = frozenset({SOURCE_SUBMIT, SOURCE_CONTINUE})
# Automatic stops count consecutive goal turns, as in Codex.
STOP_AFTER = 3
_SUCCESS_TOOL_STATUSES = frozenset({"ok", "success", "succeeded", "completed"})
_ERROR_REASONS = frozenset({
    "provider_error", "model_step_error", "harness_error", "host_preflight_error",
    "transport_disconnected",
})
_USAGE_PUSH_INTERVAL_S = 1.0
_MAX_NOTES = 8


@dataclass
class _GoalTurn:
    goal_id: str
    run: Any
    source: str
    admission_id: str
    next_requested: bool = False


@dataclass
class _Streaks:
    empty: int = 0
    failed_execution: int = 0


@dataclass
class _Push:
    last: float = 0.0
    pending: Any = None
    extra: dict = field(default_factory=dict)


class ChatGoalService:
    def __init__(self, host: Any, store: ChatGoalStore, *, clock=time.monotonic) -> None:
        self.host = host
        self.store = store
        self._clock = clock
        self._guard = threading.Lock()
        self._running: dict[str, _GoalTurn] = {}
        self._notes: dict[tuple[str, str], list[tuple[int, str]]] = {}
        self._streaks: dict[str, _Streaks] = {}
        self._pushes: dict[str, _Push] = {}
        self._chat_locks: dict[str, asyncio.Lock] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    # ── reads ──────────────────────────────────────────────────────────────
    def current(self, chat_id: str) -> dict | None:
        goal = self.store.current(chat_id)
        return self._view(goal)

    def _view(self, goal: ChatGoal | None) -> dict | None:
        if goal is None:
            return None
        with self._guard:
            turn = self._running.get(goal.chat_id)
        return goal.to_dict(continuing=bool(turn and turn.goal_id == goal.goal_id and turn.source in GOAL_SOURCES))

    def _chat_goal(self, chat_id: str, goal_id: str) -> ChatGoal:
        goal = self.store.current(chat_id)
        if goal is None or goal.goal_id != str(goal_id or ""):
            raise GoalNotFound("The goal is no longer current in this chat")
        return goal

    def _require_chat(self, chat_id: str) -> None:
        target = self.host.require_runtime().sessions.get_session(chat_id)
        if target is None or target.get("archived"):
            raise ChatUnavailable("The chat is unavailable or archived")

    # ── user commands ──────────────────────────────────────────────────────
    async def submit(self, chat_id: str, request_id: str, objective: str, *, replace: bool = False,
                     expected_goal_id: str | None = None) -> dict | None:
        self._require_chat(chat_id)
        if not request_id:
            raise GoalValidationError("request_id is required")
        goal, created = await asyncio.to_thread(
            self.store.create, chat_id, objective, request_id=request_id, replace=replace,
            expected_goal_id=expected_goal_id)
        if created:
            if expected_goal_id:
                self._forget(expected_goal_id, chat_id)
            self.publish(chat_id)
        if created or goal.submission_pending:
            await self.continue_if_idle(chat_id, expected_goal_id=goal.goal_id)
        return self.current(chat_id)

    async def control(self, chat_id: str, goal_id: str, action: str, *,
                      expected_version: int | None = None, objective: str = "") -> dict | None:
        self._require_chat(chat_id)
        goal = self._chat_goal(chat_id, goal_id)
        action = str(action or "").strip().lower()
        if action == "pause":
            updated = self.store.transition(goal.goal_id, "paused", reason="Paused by you",
                                            from_statuses={"active"}, expected_version=expected_version)
            if updated is None and goal.status != "paused":
                raise GoalConflict(f"A {goal.status.replace('_', ' ')} goal can't be paused")
        elif action == "resume":
            updated = self.store.transition(goal.goal_id, "active", from_statuses=RESUMABLE,
                                            expected_version=expected_version)
            if updated is None and goal.status != "active":
                raise GoalConflict(f"A {goal.status.replace('_', ' ')} goal can't be resumed")
            self._forget(goal.goal_id)
        elif action == "clear":
            self.store.clear(goal.goal_id, expected_version=expected_version)
            self._forget(goal.goal_id, chat_id)
            self.publish(chat_id)
            return None
        elif action == "edit":
            updated = self.store.edit(goal.goal_id, objective, expected_version=expected_version)
            if updated.objective_revision != goal.objective_revision:
                self._forget(goal.goal_id)
                if updated.status == "active":
                    self._add_note(updated, objective_changed_note(updated))
        else:
            raise GoalValidationError("action must be pause, resume, clear or edit")
        self.publish(chat_id)
        if action == "resume" and (updated or goal).status == "active":
            await self.continue_if_idle(chat_id, expected_goal_id=goal.goal_id)
        return self.current(chat_id)

    def pause_for_stop(self, chat_id: str) -> None:
        """The user pressed Stop: an active goal must not continue on its own."""
        try:
            goal = self.store.current(chat_id)
            if goal is not None and goal.status == "active":
                self.store.transition(goal.goal_id, "paused", reason="Stopped by you",
                                      from_statuses={"active"})
                self.publish(chat_id)
        except Exception:
            _LOG.exception("goal pause on stop failed")

    # ── continuation ───────────────────────────────────────────────────────
    def notify_idle(self, chat_id: str) -> None:
        """Session-runtime idle listener; it may run inside registry callbacks."""
        if self._closed:
            return
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None:
            import background_tasks
            background_tasks.spawn(self.continue_if_idle(chat_id), name=f"goal-continue:{chat_id}")
        elif loop is not None and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(self.continue_if_idle(chat_id), loop)

    def _chat_lock(self, chat_id: str) -> asyncio.Lock:
        with self._guard:
            lock = self._chat_locks.get(chat_id)
            if lock is None:
                lock = self._chat_locks[chat_id] = asyncio.Lock()
            return lock

    async def continue_if_idle(self, chat_id: str, *, expected_goal_id: str = "") -> bool:
        """Start the next goal turn if the chat is idle and its goal is active."""
        async with self._chat_lock(chat_id):
            goal = self.store.current(chat_id)
            if (self._closed or goal is None or goal.status != "active"
                    or (expected_goal_id and expected_goal_id != goal.goal_id)):
                return False
            with self._guard:
                previous = self._running.get(chat_id)
                if previous is not None:
                    if expected_goal_id:
                        # A new submit or resume can arrive while Stop settles.
                        previous.next_requested = True
                    return False
            try:
                return self._launch(chat_id, goal)
            except Exception:
                _LOG.exception("goal turn could not start")
                try:
                    self.store.transition(goal.goal_id, "blocked", reason="Goal turn could not start",
                                          from_statuses={"active"}, expected_objective_revision=goal.objective_revision)
                    self.publish(chat_id)
                except (GoalNotFound, GoalConflict):
                    pass
                return False

    def _launch(self, chat_id: str, goal: ChatGoal) -> bool:
        from chat_session import ConnectionSession
        from host_chat_service import NativeChatEventTransport
        from run_context import bind_run_context

        runtime = self.host.require_runtime()
        target = runtime.sessions.get_session(chat_id)
        if target is None or target.get("archived"):
            return False
        # Queued user input, settings changes and running turns go first.
        admission = runtime.session_runtimes.try_reserve_run(chat_id, require_empty_queue=True)
        if not admission:
            return False
        submission = goal.submission_pending
        source = SOURCE_SUBMIT if submission else SOURCE_CONTINUE
        turn = None
        try:
            session = ConnectionSession(viewed_session_id=chat_id)
            transport = NativeChatEventTransport(self.host, chat_id)
            run = self.host.make_run_context(
                "goal", goal.objective, session=session, chat_transport=transport,
                inherit_parent=False,
                metadata={"_server_bound_kind": "chat", "chat_id": chat_id,
                          "work_scope": {"chat_id": chat_id, "goal_id": goal.goal_id},
                          "goal_id": goal.goal_id,
                          "_goal_prompt_objective_revision": goal.objective_revision,
                          "goal_display_text": goal.objective if submission else CONTINUATION_DISPLAY})
            turn = _GoalTurn(goal.goal_id, run, source, admission)
            with self._guard:
                self._running[chat_id] = turn
            with bind_run_context(run):
                task = runtime.chat.launch_reserved_turn(
                    transport, continuation_prompt(goal, submission=submission), session,
                    runtime_admission_id=admission, source=source,
                    task_name=f"goal-turn:{goal.goal_id}")
        except BaseException:
            with self._guard:
                if turn is not None and self._running.get(chat_id) is turn:
                    self._running.pop(chat_id, None)
            runtime.session_runtimes.finish_run(admission, status="goal_admission_failed")
            raise
        task.add_done_callback(lambda done: self._turn_finished(chat_id, turn, done))
        self.publish(chat_id)
        return True

    def _turn_finished(self, chat_id: str, turn: _GoalTurn, task: asyncio.Task) -> None:
        with self._guard:
            if self._running.get(chat_id) is turn:
                self._running.pop(chat_id, None)
        if self._closed:
            return
        binding = turn_binding(turn.run, chat_id)
        if binding is not None:
            turn.goal_id = binding.attributed_id
        try:
            goal = self.store.current(chat_id)
            if (goal is not None and goal.goal_id == turn.goal_id and goal.status == "active"
                    and (binding is None or (binding.goal_id == goal.goal_id
                                            and binding.objective_revision == goal.objective_revision))):
                stop = self._automatic_stop(chat_id, turn, task)
                if stop is not None:
                    status, reason = stop
                    self.store.transition(goal.goal_id, status, reason=reason, from_statuses={"active"},
                                          expected_objective_revision=goal.objective_revision)
        except (GoalNotFound, GoalConflict):
            pass  # This turn's objective was retired during settlement.
        except Exception:
            _LOG.exception("goal turn evaluation failed")
        self.publish(chat_id)
        # Admission release can precede durable settlement / task completion.
        # The early idle callback saw _running; reevaluate once after deciding
        # the terminal status, including when the user replaced this goal.
        if not task.cancelled() or turn.next_requested:
            current = self.store.current(chat_id)
            if current is not None and current.status == "active":
                self.notify_idle(chat_id)

    def _automatic_stop(self, chat_id: str, turn: _GoalTurn, task: asyncio.Task) -> tuple[str, str] | None:
        metadata = dict(getattr(turn.run, "metadata", None) or {})
        status = str(metadata.get("_terminal_status") or "")
        if task.cancelled() or status == "cancelled":
            return None
        error = task.exception() if not task.cancelled() else None
        reason = str(metadata.get("_terminal_reason") or "")
        if (error is not None or status == "error" or reason in _ERROR_REASONS
                or str(metadata.get("_terminal_stop_reason") or "") == "error"):
            if str(metadata.get("_provider_failure_kind") or "") == "quota":
                return "usage_limited", "The model provider reported a usage limit"
            return "blocked", "The last goal turn ended with an error"
        receipt = self._receipt(chat_id, str(getattr(turn.run, "run_id", "") or ""), turn.admission_id)
        streaks = self._streaks_for(turn.goal_id)
        # Empty output counts only for turns the goal started by itself.
        if (turn.source == SOURCE_CONTINUE and metadata.get("_terminal_reply_empty") is True
                and int(receipt.get("tool_calls") or 0) == 0):
            streaks.empty += 1
        else:
            streaks.empty = 0
        statuses = receipt.get("tool_results_by_status") if isinstance(receipt, dict) else None
        if isinstance(statuses, dict) and statuses:
            if any(key in _SUCCESS_TOOL_STATUSES for key in statuses):
                streaks.failed_execution = 0
            else:
                streaks.failed_execution += 1
        else:
            streaks.failed_execution = 0
        if streaks.empty >= STOP_AFTER:
            return "blocked", "Three goal turns in a row produced no output"
        if streaks.failed_execution >= STOP_AFTER:
            return "blocked", "Execution failed in three goal turns in a row"
        return None

    def _receipt(self, chat_id: str, run_id: str, admission_id: str) -> dict:
        try:
            runtime = self.host.require_runtime()
            lookup = getattr(getattr(runtime.session_runtimes, "repository", None), "get_run_settlement", None)
            if callable(lookup):
                settled = lookup(chat_id, run_id=run_id, admission_id=admission_id)
                if settled:
                    return settled["receipt"]
            receipt = runtime.sessions.get_last_run_receipt(chat_id) or {}
        except Exception:
            return {}
        return receipt if run_id and receipt.get("run_id") == run_id else {}

    def _streaks_for(self, goal_id: str) -> _Streaks:
        with self._guard:
            return self._streaks.setdefault(goal_id, _Streaks())

    def _forget(self, goal_id: str, chat_id: str = "") -> None:
        with self._guard:
            self._streaks.pop(goal_id, None)
            if chat_id:
                self._notes.pop((chat_id, goal_id), None)

    # ── usage and notes ────────────────────────────────────────────────────
    def admit_chat_turn(self, context, chat_id: str, admission_id: str, source: str, *, task=None) -> bool:
        """Bind this writer to its Goal before its first model request or effect."""
        if source in GOAL_SOURCES:
            expected = str(context.metadata.get("goal_id") or "")
            if self._closed or not self.store.admit_turn(chat_id, expected, context.run_id,
                                                        submission=source == SOURCE_SUBMIT):
                return False
        goal = self.store.current(chat_id)
        if source in GOAL_SOURCES and (goal is None or goal.goal_id != expected):
            return False
        goal_id = goal.goal_id if goal else ""
        attributed = goal_id if goal and goal.status == "active" else ""
        # The launch prompt can already be older than the goal read at admission.
        revision = (context.metadata.get("_goal_prompt_objective_revision")
                    if source in GOAL_SOURCES else None)
        context.metadata["_chat_goal_binding"] = GoalBinding(
            chat_id, admission_id, goal_id, attributed,
            revision if type(revision) is int else (goal.objective_revision if goal else 0),
            show_objective=bool(source not in GOAL_SOURCES and goal and goal.status in {"active", "budget_limited"}))
        context.work_scope = context.work_scope.with_updates(goal_id=attributed)
        context.metadata["work_scope"] = context.work_scope.to_dict(include_empty=False)
        if source not in GOAL_SOURCES and task is not None:
            # User and peer turns get first admission; evaluate their failures
            # (and any goal created during them) before scheduling more work.
            turn = _GoalTurn(attributed, context, source, admission_id)
            with self._guard:
                self._running[chat_id] = turn
            task.add_done_callback(lambda done: self._turn_finished(chat_id, turn, done))
        if source == SOURCE_SUBMIT:
            self.publish(chat_id)
        return True

    def request_usage(self, manifest: dict) -> GoalRequestUsage | None:
        """Freeze attribution at physical request dispatch, including inherited child scope."""
        goal_id = ((manifest.get("run") or {}).get("work_scope") or {}).get("goal_id")
        if not goal_id or not self.store.chargeable(goal_id):
            return None
        return GoalRequestUsage(goal_id, self._charge)

    def _charge(self, goal_id: str, delta: int) -> None:
        updated, reached = self.store.apply_token_delta(goal_id, delta)
        if updated is None:
            return
        if reached:
            self._add_note(updated, budget_reached_note(updated))
        self.publish(updated.chat_id, usage_only=not reached)

    def _add_note(self, goal: ChatGoal, text: str) -> None:
        with self._guard:
            notes = self._notes.setdefault((goal.chat_id, goal.goal_id), [])
            notes.append((goal.objective_revision, text))
            del notes[:-_MAX_NOTES]

    def take_notes(self, chat_id: str, goal_id: str) -> list[str]:
        """Notes for the chat's running turn, delivered before its next model call."""
        from run_context import current_run_context
        binding = turn_binding(current_run_context(), chat_id)
        if binding is not None and not goal_id:
            goal_id = binding.goal_id
        if binding is not None and binding.goal_id != goal_id:
            return []
        with self._guard:
            notes = self._notes.pop((chat_id, goal_id), [])
        if binding is not None and goal_id:
            goal = self.store.current(chat_id)
            if goal is not None and goal.goal_id == goal_id:
                # A queued note already quotes its objective. Otherwise include
                # the current one even after Pause or a host restart lost a note.
                changed = goal.objective_revision != binding.objective_revision
                last_revision = notes[-1][0] if notes else binding.objective_revision
                if last_revision != goal.objective_revision or (binding.show_objective and not notes):
                    text = objective_changed_note(goal) if changed else current_objective_note(goal)
                    notes.append((goal.objective_revision, text))
                binding.show_objective = False
                binding.objective_revision = goal.objective_revision
        return [text for _, text in notes]

    # ── model tools (session.get_goal / create_goal / update_goal) ─────────
    def tool_get(self, chat_id: str) -> dict:
        goal = self.store.current(chat_id)
        if goal is None:
            return {"goal": None}
        return {"goal": self._view(goal), "remaining_tokens": goal.remaining_tokens()}

    @staticmethod
    def _invocation_binding(chat_id: str, context):
        from run_context import current_run_context
        return turn_binding(current_run_context(), chat_id) if context is not None else None

    def tool_create(self, chat_id: str, objective: str, token_budget: Any = None, *, context=None) -> dict:
        binding = self._invocation_binding(chat_id, context)
        expected = (binding.goal_id if binding is not None else
                    getattr(getattr(context, "work_scope", None), "goal_id", None) or None)
        run_id = str(getattr(context, "run_id", "") or "")
        goal, _created = self.store.create(chat_id, objective, token_budget=token_budget,
                                          expected_goal_id=expected, creator_run_id=run_id)
        if binding is not None:
            binding.goal_id = binding.attributed_id = goal.goal_id
            binding.objective_revision = goal.objective_revision
        self._forget(goal.goal_id)
        self.publish(chat_id)
        return {"goal": self.current(chat_id)}

    def tool_update(self, chat_id: str, status: str, *, context: Any = None) -> dict:
        goal = self.store.current(chat_id)
        if goal is None:
            raise GoalNotFound("This chat has no goal")
        status = str(status or "").strip().lower()
        allowed = {"complete": {"active", "budget_limited"}, "blocked": {"active"},
                   "paused": {"active"}}.get(status)
        if allowed is None:
            raise GoalValidationError("status must be complete, blocked or paused")
        run_id = str(getattr(context, "run_id", "") or "")
        expected_revision = None
        if context is not None:
            binding = self._invocation_binding(chat_id, context)
            expected_revision = binding.objective_revision if binding is not None else 1
            expected = (binding.goal_id if binding is not None else
                        getattr(getattr(context, "work_scope", None), "goal_id", ""))
            if expected != goal.goal_id and not (not expected and run_id and goal.first_run_id == run_id):
                raise GoalConflict("This turn belongs to an earlier goal; the current goal was not changed")
        reason = {"blocked": "Reported blocked by the model",
                  "paused": "Paused at your request"}.get(status, "")
        updated = self.store.transition(goal.goal_id, status, reason=reason,
                                        from_statuses=allowed, run_id=run_id,
                                        expected_objective_revision=expected_revision)
        if updated is None:
            raise GoalConflict(f"The goal is {goal.status.replace('_', ' ')}; it can't become {status}")
        if status == "complete":
            self._forget(updated.goal_id, chat_id)
        self.publish(chat_id)
        return {"goal": self._view(updated), "tokens_used": updated.tokens_used,
                "token_budget": updated.token_budget, "time_used_s": updated.time_used()}

    # ── push ───────────────────────────────────────────────────────────────
    def publish(self, chat_id: str, *, usage_only: bool = False) -> None:
        """Push ``goal:updated``; usage-only changes coalesce to one per second."""
        loop = self._loop
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        if self._closed or loop is None or loop.is_closed():
            return
        # Timers belong to the loop thread; tool handlers publish from workers.
        loop.call_soon_threadsafe(self._schedule_push, loop, chat_id, usage_only)

    def _schedule_push(self, loop: asyncio.AbstractEventLoop, chat_id: str, usage_only: bool) -> None:
        if self._closed:
            return
        now = self._clock()
        state = self._pushes.setdefault(chat_id, _Push())
        if usage_only and now - state.last < _USAGE_PUSH_INTERVAL_S:
            if state.pending is None:
                delay = _USAGE_PUSH_INTERVAL_S - (now - state.last)
                state.pending = loop.call_later(delay, self._flush_push, chat_id)
            return
        if state.pending is not None:
            state.pending.cancel()
            state.pending = None
        state.last = now
        self._send_update(chat_id)

    def _flush_push(self, chat_id: str) -> None:
        state = self._pushes.setdefault(chat_id, _Push())
        state.pending = None
        state.last = self._clock()
        self._send_update(chat_id)

    def _send_update(self, chat_id: str) -> None:
        try:
            registry = self.host.require_runtime().session_runtimes
            transports = list(registry.attached_transports(chat_id))
        except Exception:
            return
        if not transports:
            return
        try:
            payload = {"type": "goal:updated", "session_id": chat_id, "goal": self.current(chat_id)}
        except Exception:
            _LOG.exception("goal update could not be read")
            return
        import background_tasks
        for transport in transports:
            background_tasks.spawn(_send(transport, payload), name="goal-updated")

    def completion_event(self, chat_id: str, run_id: str) -> dict | None:
        goal = self.store.completed_by(chat_id, run_id)
        if goal is None:
            return None
        return {"event": "achieved", "goal_id": goal.goal_id,
                "time_used_s": goal.time_used(), "tokens_used": goal.tokens_used}

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        """Bind to the server loop, carry over old composer goals, resume active goals."""
        self._loop = asyncio.get_running_loop()
        try:
            await asyncio.to_thread(self.migrate_legacy_goals)
        except Exception:
            _LOG.exception("legacy goal migration failed")
            raise
        for chat_id in await asyncio.to_thread(self.store.active_chat_ids):
            await self.continue_if_idle(chat_id)

    def migrate_legacy_goals(self) -> int:
        return self.store.migrate_legacy_goals()

    async def shutdown(self) -> None:
        """Fence scheduling before other host services start shutting down."""
        self._closed = True
        self.host.require_runtime().session_runtimes.unregister_idle_listener(self.notify_idle)
        for push in self._pushes.values():
            if push.pending is not None:
                push.pending.cancel()
        self._pushes.clear()

    def delete_chat(self, chat_id: str) -> None:
        with self._guard:
            self._running.pop(chat_id, None)
            for key in [key for key in self._notes if key[0] == chat_id]:
                del self._notes[key]
        self.store.delete_chat(chat_id)


async def _send(transport: Any, payload: dict) -> None:
    try:
        await transport.send_json(payload)
    except Exception:
        pass


def create_chat_goal_service(host: Any, path: str) -> ChatGoalService:
    return ChatGoalService(host, ChatGoalStore(path))
