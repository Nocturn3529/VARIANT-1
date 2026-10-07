"""Composer Goals run ordinary turns in their visible, durable owning chat.

The supervisor releases its Work slot while a turn runs. Completion callbacks
and idle admission events wake it; an unchanged wait creates no polling jobs.
Effects fence exact admissions so a crash never silently replays a turn.
"""
from __future__ import annotations

import asyncio
import logging
import time

from tools import ToolError
from work_fabric.models import WorkActor
from .conditions import budget_allows
from .models import GoalConflict
from .executor import StepExecutionResult

_LOG = logging.getLogger(__name__)
_ACTOR = WorkActor("system", "goal-parent-session")


class ParentSessionGoals:
    def __init__(self, host, goals, adapters):
        self.host, self.goals, self.adapters = host, goals, adapters
        self._tasks = {}
        goals.parent_session = self
        goals.event_driven_wait_sources.add("parent_session")
        goals.register_wait_resolver("parent_session", self.resolve)

    def notify_idle(self, chat_id):
        goal = self.goals.repository.active_composer_goal(chat_id)
        if goal is None or goal.completion_policy.get("execution_owner") != "parent":
            return
        waits = self.goals.repository.list_waits(goal.goal_id, status="pending")
        if any(wait.source == "parent_session" and wait.matcher.get("idle") for wait in waits):
            self.goals.supervisor.enqueue(goal.goal_id, reason="parent_idle")

    def refresh_budget(self, goal_id):
        ledger = getattr(getattr(getattr(self.host, 'router', None), '_manifest_bus', None), 'usage_ledger', None)
        if ledger is None:
            return
        # Read the indexed totals on each CAS attempt. A concurrent newer
        # receipt must never be overwritten by an older observation.
        for _ in range(4):
            goal = self.goals.repository.require_goal(goal_id)
            if goal.completion_policy.get('execution_owner') != 'parent':
                return None
            totals = ledger.totals(goal_id=goal_id)
            values = {'provider_calls': totals['requests'], 'tokens': totals['total_tokens'], 'cost_usd': totals['cost_usd']}
            delta = {key: value - float(goal.budget_usage.get(key) or 0)
                     for key, value in values.items()
                     if value is not None and value != float(goal.budget_usage.get(key) or 0)}
            if not delta:
                return totals
            try:
                self.goals.repository.update_budget_usage(goal_id, delta, expected_version=goal.version, actor=_ACTOR)
                return totals
            except GoalConflict:
                continue
        raise GoalConflict('Goal accounting changed concurrently; retry the boundary')

    def boundary_budget(self, context, admission_id):
        """Pause only the exact parent admission at a completed-work boundary."""
        goal_id = str(getattr(getattr(context, 'work_scope', None), 'goal_id', '') or '')
        if not goal_id or not admission_id:
            return False
        runtime = self.host.require_runtime()
        goal = self.goals.repository.get_goal(goal_id)
        if goal is None:
            return False  # A stale/non-parent scope does not govern this run.
        slot = self.goals.repository.state_get(goal_id, 'parent_turn') or {}
        if (goal.completion_policy.get('execution_owner') != 'parent'
                or goal.owner_chat_id != context.session_id
                or slot.get('admission_id') != admission_id or slot.get('run_id') != context.run_id):
            return False
        active = runtime.session_runtimes.pause_snapshot(goal.owner_chat_id)
        if active['admission_id'] != admission_id or active['run_id'] != context.run_id:
            return False
        try:
            return self._boundary_budget(context, admission_id, goal)
        except Exception as exc:
            _LOG.warning('Goal budget boundary unavailable: %s', type(exc).__name__)
            if not (goal.budget_limits or goal.deadline):
                return False
            # Accounting failure is a visible pause for capped work, not a
            # provider failure. Never pause a successor admission after a race.
            context.metadata['goal_budget_error'] = 'Goal budget check unavailable; inspect accounting before resuming.'
            try:
                current = self.goals.repository.get_goal(goal_id)
                if current is not None and not current.terminal and current.status != 'paused':
                    self.goals.repository.transition_goal(goal_id, 'paused', expected_version=current.version,
                        reason=context.metadata['goal_budget_error'], actor=_ACTOR, event_type='goal.accounting_unavailable')
            except Exception:
                _LOG.warning('Goal accounting pause could not be persisted')
            try:
                runtime.session_runtimes.set_run_paused(goal.owner_chat_id, True,
                    expected_admission_id=admission_id, expected_run_id=context.run_id)
                return True
            except RuntimeError as stale:
                if str(stale) != 'stale_run':
                    raise
                return False  # wait_if_paused enforces the obsolete admission.

    def _boundary_budget(self, context, admission_id, goal):
        goal_id = goal.goal_id
        runtime = self.host.require_runtime()
        totals = self.refresh_budget(goal_id)
        goal = self.goals.repository.require_goal(goal_id)
        if goal.terminal:
            return False  # Canonical cancellation owns retiring this admission.
        context.metadata.pop('goal_budget_error', None)
        decision = budget_allows(goal)
        reason = decision.reason if not decision.allowed else ''
        if totals is None and any(key in goal.budget_limits for key in ('tokens', 'cost_usd', 'provider_calls')):
            reason = 'budget accounting is unavailable'
        bus = getattr(getattr(self.host, 'router', None), '_manifest_bus', None)
        lost = getattr(bus, 'goal_usage_lost', None)
        if (callable(lost) and lost(goal_id)
                and any(key in goal.budget_limits for key in ('tokens', 'cost_usd', 'provider_calls'))):
            reason = 'budget accounting has lost usage records'
        if totals and totals['requests']:
            for limit, field in [('tokens', 'total_tokens'), ('cost_usd', 'cost_usd')]:
                if (limit in goal.budget_limits
                        and totals.get(field + '_known_requests', 0) < totals['requests']):
                    reason = f'budget {limit} requires complete usage measurements'
                    break
        if not reason:
            return False
        # No new Work jobs and no task cancellation: the graph remains at its
        # saved boundary, with its live Python state available to the user.
        if goal.status != 'paused':
            self.goals.repository.transition_goal(goal_id, 'paused', expected_version=goal.version,
                reason=reason, actor=_ACTOR, event_type='goal.budget_exhausted')
        runtime.session_runtimes.set_run_paused(goal.owner_chat_id, True,
            expected_admission_id=admission_id, expected_run_id=context.run_id)
        return True

    def notify_peer_result(self, message):
        if message.get('message_kind') != 'result' or not message.get('in_reply_to'):
            return
        target = str(message.get('target_peer_id') or '')
        if not target.startswith('chat:'):
            return
        goal = self.goals.repository.active_composer_goal(target[5:])
        if goal is None or goal.completion_policy.get('execution_owner') != 'parent':
            return
        slot = self.goals.repository.state_get(goal.goal_id, 'parent_turn') or {}
        pending = (slot.get('report') or {}).get('wait_for_message_ids') or []
        if message['in_reply_to'] in pending:
            self.goals.supervisor.enqueue(goal.goal_id, reason='awaited_peer_result',
                dedupe_key='peer-result:' + str(message['message_id']))

    def panel_facts(self, goal_id):
        """Lost usage records and the peer requests this Goal's turn awaits."""
        bus = getattr(getattr(self.host, 'router', None), '_manifest_bus', None)
        lost = getattr(bus, 'goal_usage_lost', None)
        accounting = {'lost_usage_records': lost(goal_id)
                      if callable(lost) and getattr(bus, 'usage_ledger', None) is not None else None}
        goal = self.goals.repository.get_goal(goal_id)
        slot = self.goals.repository.state_get(goal_id, 'parent_turn') or {} if goal else {}
        awaited = ((slot.get('report') or {}).get('wait_for_message_ids') or [])[:20]
        peers = getattr(self.host.require_runtime(), 'peers', None)
        rows = []
        for message_id in awaited:
            try:
                rows.append(peers.awaited_request('chat:' + goal.owner_chat_id, message_id))
            except Exception:
                rows.append({'message_id': message_id, 'chat_id': '', 'display_name': '',
                             'state': 'unavailable'})
        return {'accounting': accounting, 'awaiting_peers': rows}

    def notify_peer_unavailable(self, message):
        if (message.get('message_kind') != 'request'
                or not str(message.get('sender_peer_id', '')).startswith('chat:')
                or not (message.get('state') in {'failed', 'parked'}
                        or message.get('evidence', {}).get('native_wait_failure'))):
            return
        # Wake only for a dead end. A Stop-parked request may still be resumed,
        # and an early wake would spend this request's one-time dedupe key.
        peers = getattr(self.host.require_runtime(), 'peers', None)
        failure = getattr(peers, 'native_wait_failure', None)
        if callable(failure) and not failure(message['sender_peer_id'], message['message_id']):
            return
        goal = self.goals.repository.active_composer_goal(message['sender_peer_id'][5:])
        if goal is None or goal.completion_policy.get('execution_owner') != 'parent':
            return
        slot = self.goals.repository.state_get(goal.goal_id, 'parent_turn') or {}
        if message['message_id'] in (slot.get('report') or {}).get('wait_for_message_ids', []):
            self.goals.supervisor.enqueue(goal.goal_id, reason='awaited_peer_unavailable',
                dedupe_key='peer-unavailable:' + message['message_id'])

    def _state(self, goal_id, value):
        goal = self.goals.repository.require_goal(goal_id)
        self.goals.repository.state_set(goal_id, "parent_turn", value,
            expected_version=goal.version, actor=_ACTOR)

    def _outcome(self, goal_id, value):
        goal = self.goals.repository.require_goal(goal_id)
        self.goals.repository.state_set(goal_id, "objective_outcome", value,
            expected_version=goal.version, actor=_ACTOR)

    async def execute(self, context):
        from chat_session import ConnectionSession
        from host_chat_service import NativeChatEventTransport
        from run_context import bind_run_context

        runtime = self.host.require_runtime()
        chat_id = str(context.goal.owner_chat_id or "")
        target = runtime.sessions.get_session(chat_id)
        if target is None or target.get("archived"):
            return StepExecutionResult(status="blocked", error="Goal owner chat is unavailable or archived")
        if context.cancellation_requested():
            return StepExecutionResult(status="cancelled", error="Goal cancelled before parent admission")
        # Atomic idle admission gives user/peer tickets and settings priority.
        admission = runtime.session_runtimes.try_reserve_run(chat_id, require_empty_queue=True)
        if not admission:
            return StepExecutionResult(status="waiting", wait_source="parent_session",
                wait_matcher={"chat_id": chat_id, "idle": True})
        try:
            effect = self.adapters._effect(context, kind="parent.turn", request={"chat_id": chat_id})
            if effect.status != "planned":
                runtime.session_runtimes.finish_run(admission, status="goal_effect_not_replayable")
                return StepExecutionResult(status="blocked", error="Parent turn needs reconciliation; it will not be replayed")
            session = ConnectionSession(viewed_session_id=chat_id)
            transport = NativeChatEventTransport(self.host, chat_id)
            scope = self.adapters._scope(context)
            run = self.host.make_run_context("goal", context.goal.objective, session=session,
                chat_transport=transport, inherit_parent=False,
                metadata={"_server_bound_kind": "chat", "chat_id": chat_id,
                          "work_scope": scope.to_dict(), "goal_admission_id": admission,
                          "goal_budget_limited": bool(context.goal.budget_limits or context.goal.deadline)})
            slot = {"admission_id": admission, "effect_id": effect.effect_id,
                    "run_id": run.run_id, "step_id": context.step.step_id,
                    "attempt": context.attempt.attempt, "status": "running"}
            self.adapters._effect_update(effect, "dispatched", response=slot)
            self._state(context.goal.goal_id, slot)
            self._outcome(context.goal.goal_id, {"status": "unreported", "basis": None,
                "independently_verified": False, "step_id": context.step.step_id})
            follow_up = self.goals.repository.state_get(context.goal.goal_id, "continuation_request") or {}
            text = "Goal objective:\n" + context.goal.objective
            if follow_up.get("previous_attempt") == context.attempt.attempt - 1:
                text += "\nUser guidance:\n" + str(follow_up.get("message") or "")
            text += ("\nContinue from this session's retained work and state. Before your final reply, "
                "record exactly one session.report_outcome with status completed, blocked, or continuing, "
                "a concrete summary, and evidence_refs where available. Completed means the objective "
                "has been achieved; continuing requests another turn; blocked identifies required input. "
                "A report is your claim and does not replace independent validation.")
            with bind_run_context(run):
                task = runtime.chat.launch_reserved_turn(transport, text, session,
                    runtime_admission_id=admission, source="goal",
                    client_id=effect.effect_id, task_name=f"goal-parent:{context.goal.goal_id}")
            self._tasks[context.goal.goal_id] = (admission,task)
            task.add_done_callback(lambda done: self._finished(context.goal.goal_id, slot, done))
        except BaseException:
            runtime.session_runtimes.finish_run(admission, status="goal_admission_failed")
            raise
        return StepExecutionResult(status="waiting", wait_source="parent_session",
            wait_matcher={"chat_id": chat_id, "admission_id": admission, "effect_id": effect.effect_id})

    def _finished(self, goal_id, slot, task):
        owned = self._tasks.get(goal_id)
        if owned and owned[0] == slot['admission_id']:
            self._tasks.pop(goal_id,None)
        try:
            try:
                self.refresh_budget(goal_id)
            except Exception as exc:
                _LOG.warning('Goal terminal accounting refresh unavailable: %s', type(exc).__name__)
            goal = self.goals.repository.require_goal(goal_id)
            current = self.goals.repository.state_get(goal_id, "parent_turn") or {}
            if current.get("admission_id") != slot["admission_id"] or goal.terminal:
                return
            error = ""
            if task.cancelled():
                error = "Parent turn was interrupted"
            elif task.exception() is not None:
                # Provider errors can contain sensitive request data; retain a type only.
                error = "Parent turn failed: " + type(task.exception()).__name__
            else:
                receipt = self.host.require_runtime().sessions.get_last_run_receipt(goal.owner_chat_id) or {}
                if (receipt.get("run_id") != slot["run_id"] or not receipt.get("settled")
                        or receipt.get("status") != "ok"):
                    error = "Parent turn did not commit a successful terminal receipt"
            self._state(goal_id, {**current, "status": "failed" if error else "finished", "error": error})
            self.goals.supervisor.enqueue(goal_id, reason="parent_turn_finished",
                dedupe_key="parent-finished:" + slot["admission_id"])
        except Exception:
            _LOG.exception("Goal parent terminal reconciliation failed")

    def report(self, invocation, *, status, summary, evidence_refs=None, wait_for_message_ids=None):
        goal_id = invocation.work_scope.goal_id
        goal = self.goals.repository.require_goal(goal_id)
        slot = self.goals.repository.state_get(goal_id, "parent_turn") or {}
        registry = self.host.require_runtime().session_runtimes
        active = registry.snapshot(invocation.chat_id)
        if (goal.owner_chat_id != invocation.chat_id or goal.terminal
                or slot.get("status") != "running" or slot.get("run_id") != invocation.run_id
                or active.get("active_admission_id") != slot.get("admission_id")):
            raise ToolError("Outcome report belongs to a stale or different Goal run")
        if status not in {"completed", "blocked", "continuing"}:
            raise ToolError("Invalid Goal outcome status")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 4000:
            raise ToolError("Outcome summary must contain 1–4000 characters")
        refs = [] if evidence_refs is None else evidence_refs
        if not isinstance(refs, list) or len(refs) > 32 or any(not isinstance(r, str) or not r.strip() or len(r) > 2000 for r in refs):
            raise ToolError("evidence_refs must contain at most 32 nonempty references")
        awaited = [] if wait_for_message_ids is None else wait_for_message_ids
        if (not isinstance(awaited, list) or len(awaited) > 32
                or any(not isinstance(item, str) or not item or len(item) > 160 for item in awaited)
                or (awaited and status != 'continuing')):
            raise ToolError('Peer waits require continuing and at most 32 message IDs')
        peers = getattr(self.host.require_runtime(), 'peers', None)
        for identity in awaited:
            request = peers.repository.get_message(identity) if peers else None
            if (request is None or request.get('sender_peer_id') != 'chat:' + invocation.chat_id
                    or request.get('message_kind') != 'request'):
                raise ToolError('Goal may await only its owning session\'s outgoing peer requests')
        prior = slot.get("report")
        if prior:
            if (prior["status"], prior["summary"], prior["evidence_refs"], prior.get('wait_for_message_ids', [])) == (status, summary.strip(), refs, awaited):
                return prior
            raise ToolError("This Goal turn already reported an outcome")
        value = {"status": status, "summary": summary.strip(), "evidence_refs": list(refs),
                 "wait_for_message_ids": list(awaited),
                 "basis": "agent_report", "independently_verified": False,
                 "run_id": invocation.run_id, "step_id": slot["step_id"], "reported_at": time.time()}
        self._state(goal_id, {**slot, "report": value})
        return value

    def resolve(self, wait):
        runtime = self.host.require_runtime()
        if wait.matcher.get("idle"):
            chat_id = wait.matcher["chat_id"]
            if (runtime.session_runtimes.is_busy(chat_id)
                    or runtime.session_runtimes.configuration_pending(chat_id)
                    or runtime.session_runtimes.queued_input_count(chat_id)):
                return None
            self._outcome(wait.goal_id, {"status": "continuing", "step_id": wait.step_id,
                "basis": "idle_admission", "independently_verified": False})
            return {"status": "blocked", "error": "Owner is idle; admit the Goal turn"}
        slot = self.goals.repository.state_get(wait.goal_id, "parent_turn") or {}
        if slot.get("admission_id") != wait.matcher.get("admission_id"):
            return {"status": "blocked", "error": "Parent admission identity changed"}
        if slot.get("status") == "running":
            owned = self._tasks.get(wait.goal_id)
            if owned and owned[0] == slot['admission_id'] and not owned[1].done():
                return None  # Final persistence can outlive admission release.
            active = runtime.session_runtimes.snapshot(wait.matcher["chat_id"])
            if active.get("active_admission_id") == slot["admission_id"]:
                return None
            # Recovery never automatically reruns an uncertain native turn.
            return {"status": "blocked", "error": "Parent turn lost its admission; inspect retained evidence before continuing"}
        report = slot.get("report") or {"status": "unreported", "basis": None, "independently_verified": False}
        if slot.get('status') == 'finished' and report.get('status') == 'continuing':
            peers = getattr(runtime, 'peers', None)
            awaited = report.get('wait_for_message_ids') or []
            for identity in awaited:
                if peers is None or peers.repository.find_result_reply(identity) is not None:
                    continue
                failure = getattr(peers, 'native_wait_failure', lambda *_:None)('chat:'+wait.matcher['chat_id'], identity)
                if failure:
                    self._outcome(wait.goal_id, {**report, 'status':'blocked', 'continuation_allowed':False,
                        'basis':'peer_unavailable', 'request_message_id':identity, 'error':failure})
                    return {'status':'blocked', 'error':failure}
            if awaited and (peers is None or any(
                    peers.repository.find_result_reply(item) is None
                    and getattr(peers, 'native_completion', lambda *_:None)('chat:'+wait.matcher['chat_id'], item) is None
                    for item in awaited)):
                return None
        self._outcome(wait.goal_id, {**report, "step_id": wait.step_id,
            'execution_status':slot.get('status'), 'continuation_allowed':slot.get('status')=='finished'})
        success = slot.get("status") == "finished" and report.get("status") == "completed"
        effect = self.goals.repository.get_effect(wait.matcher["effect_id"])
        if effect is not None and effect.status == "dispatched":
            self.adapters._effect_update(effect, "succeeded" if success else "failed", response=slot)
        return {"status": "succeeded" if success else "blocked",
                "error": "" if success else (slot.get("error") or "Goal work remains unfinished or no structured outcome was reported")}

    async def cancel(self, goal):
        slot = self.goals.repository.state_get(goal.goal_id, "parent_turn") or {}
        admission = slot.get("admission_id")
        if not admission:
            return
        registry = self.host.require_runtime().session_runtimes
        task = registry.cancel_active_run(goal.owner_chat_id, expected_admission_id=admission)
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
