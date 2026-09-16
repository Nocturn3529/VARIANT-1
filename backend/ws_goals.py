"""Correlated WebSocket commands for durable goals and supervisor control."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from ws_protocol import (
    CorrelatedResponder,
    request_id as _request_id,
    session_chat_id,
)


def _chat_id(srv, session, msg):
    selected = str(msg.get('session_id') or session_chat_id(session)).strip()
    if msg.get('session_id'):
        sessions = srv.require_runtime().sessions
        if sessions.get_session(selected) is None:
            raise ValueError('The requested chat is unavailable')
    return selected


def _runtime(srv: Any, session=None, msg=None) -> Any:
    runtime = getattr(srv.require_runtime(), "goals", None)
    if runtime is None:
        raise RuntimeError("Goal service is unavailable")
    if msg is not None and msg.get('goal_id'):
        goal = runtime.get(str(msg['goal_id']))
        if goal is None or goal.owner_chat_id != _chat_id(srv, session, msg):
            raise ValueError('Goal is unavailable in the requested chat')
    return runtime


_goal_responder = CorrelatedResponder(
    family="goal",
    schema="variant1.goal-command.v1",
)


async def _respond(websocket, msg, operation, action, *, mutation=None):
    class ScopedReply:
        async def send_json(self, value):
            value = {**value, 'session_id': str(msg.get('session_id') or '')}
            if operation == 'current' and value['type'] == 'goal:accepted':
                value['type'] = 'goal:current'
            await websocket.send_json(value)
    await _goal_responder(ScopedReply(), msg, operation, action, mutation=mutation)


def _with_reports(srv, snapshot):
    if snapshot is None:
        return None
    runtime = srv.require_runtime()
    manager = getattr(getattr(runtime, 'catalog', None), 'children', None)
    reports = []
    seen=set()
    if manager is not None:
        from session_catalog.children import reported_child_text
        owner = str(snapshot['goal'].get('owner_chat_id') or '')
        for effect in snapshot.get('effects', ()):
            child_id = str((effect.get('response') or {}).get('child_id') or '')
            if effect.get('kind') not in {'agent.spawn', 'child.spawn'} or not child_id:
                continue
            key=(child_id,effect.get('step_id'))
            if key in seen:continue
            seen.add(key)
            try:
                child = manager.inspect(owner, child_id)
            except Exception as exc:
                reports.append({'child_id': child_id, 'step_id': effect.get('step_id'),
                    'status': 'unavailable', 'text': '', 'error': str(exc)[:500],
                    'completion_basis': 'agent_report'})
                continue
            text = reported_child_text(child.get('result_text') or '')
            reports.append({'child_id': child_id, 'step_id': effect.get('step_id'),
                'status': child.get('status'), 'text': text[:16000],
                'outcome':child.get('outcome') or {'status':'unreported'},
                'generation':child.get('run_generation'),
                'truncated': len(text) > 16000, 'artifact_ref': child.get('artifact_ref'),
                'completion_basis': 'agent_report'})
    return {**snapshot, 'reports': reports}


def register(on):
    @on('goal:finish')
    async def goal_finish(srv,websocket,session,msg):
        async def action():
            runtime=_runtime(srv,session,msg)
            goal=await runtime.finish_async(str(msg.get('goal_id') or ''),
                expected_version=int(msg.get('expected_version') or 0),correlation_id=_request_id(msg))
            return _with_reports(srv,runtime.composer.snapshot(goal.goal_id))
        await _respond(websocket,msg,'finish',action)

    @on('goal:continue')
    async def goal_continue(srv,websocket,session,msg):
        async def action():
            runtime=_runtime(srv,session,msg)
            return _with_reports(srv,await runtime.composer.continue_goal(
                _chat_id(srv,session,msg),str(msg.get('goal_id') or ''),_request_id(msg),
                int(msg.get('expected_version') or 0),msg.get('message','')))
        await _respond(websocket,msg,'continue',action)

    @on('goal:submit')
    async def goal_submit(srv, websocket, session, msg):
        async def action():
            if not msg.get('session_id'):
                raise ValueError('session_id is required')
            return _with_reports(srv, await _runtime(srv).composer.submit(
                _chat_id(srv, session, msg), _request_id(msg), msg.get('objective')))
        await _respond(websocket, msg, 'submit', action)

    @on('goal:current:get')
    async def goal_current(srv, websocket, session, msg):
        async def action():
            if not msg.get('session_id'):
                raise ValueError('session_id is required')
            return _with_reports(srv, await asyncio.to_thread(_runtime(srv).composer.current,
                _chat_id(srv, session, msg), str(msg.get('submission_request_id') or '')))
        await _respond(websocket, msg, 'current', action, mutation=False)

    @on("goal:list")
    async def goal_list(srv, websocket, session, msg):
        async def action():
            rows = await asyncio.to_thread(
                _runtime(srv, session, msg).list,
                status=str(msg.get("status") or ""),
                owner_chat_id=_chat_id(srv, session, msg),
                limit=max(1, min(int(msg.get("limit") or 100), 500)),
            )
            return [item.to_dict() for item in rows]
        await _respond(websocket, msg, "list", action, mutation=False)

    @on("goal:get")
    async def goal_get(srv, websocket, session, msg):
        async def action():
            return await asyncio.to_thread(
                _runtime(srv, session, msg).snapshot, str(msg.get("goal_id") or "")
            )
        await _respond(websocket, msg, "get", action, mutation=False)

    @on("goal:create")
    async def goal_create(srv, websocket, session, msg):
        async def action():
            criteria = msg.get("success_criteria") or []
            if not isinstance(criteria, list) or any(
                not isinstance(item, Mapping) for item in criteria
            ):
                raise ValueError("success_criteria must be a list of objects")
            goal = await asyncio.to_thread(
                _runtime(srv, session, msg).create,
                title=str(msg.get("title") or ""),
                objective=str(msg.get("objective") or ""),
                owner_chat_id=_chat_id(srv, session, msg),
                constraints=list(msg.get("constraints") or ()),
                success_criteria=[dict(item) for item in criteria],
                completion_policy=dict(msg.get("completion_policy") or {}),
                priority=int(msg.get("priority") or 0),
                budget=dict(msg.get("budget") or {}),
                deadline=float(msg.get("deadline") or 0),
                initial_state=dict(msg.get("initial_state") or {}),
                correlation_id=_request_id(msg),
            )
            return goal.to_dict()
        await _respond(websocket, msg, "create", action)

    @on("goal:plan")
    async def goal_plan(srv, websocket, session, msg):
        async def action():
            steps = msg.get("steps")
            if not isinstance(steps, list) or any(
                not isinstance(item, Mapping) for item in steps
            ):
                raise ValueError("steps must be a list of objects")
            goal = await asyncio.to_thread(
                _runtime(srv, session, msg).plan,
                str(msg.get("goal_id") or ""),
                [dict(item) for item in steps],
                expected_version=int(msg.get("expected_version") or 0),
                correlation_id=_request_id(msg),
            )
            return goal.to_dict()
        await _respond(websocket, msg, "plan", action)

    @on("goal:start", "goal:pause", "goal:resume", "goal:retry",
        "goal:cancel", "goal:archive")
    async def goal_control(srv, websocket, session, msg):
        operation = str(msg.get("type") or "").split(":", 1)[-1]

        async def action():
            runtime = _runtime(srv, session, msg)
            goal_id = str(msg.get("goal_id") or "")
            expected = int(msg.get("expected_version") or 0)
            if operation == "start":
                goal = runtime.start(
                    goal_id, expected_version=expected,
                    correlation_id=_request_id(msg),
                )
            elif operation == "pause":
                goal = runtime.pause(
                    goal_id, expected_version=expected,
                    reason=str(msg.get("reason") or "paused by user"),
                    correlation_id=_request_id(msg),
                )
            elif operation == "resume":
                goal = runtime.resume(
                    goal_id, expected_version=expected,
                    correlation_id=_request_id(msg),
                )
            elif operation == "retry":
                goal = runtime.retry(
                    goal_id, str(msg.get("step_id") or ""),
                    expected_version=expected,
                    delay_s=max(0.0, float(msg.get("delay_s") or 0)),
                )
            elif operation == "cancel":
                goal = await runtime.request_cancel_async(
                    goal_id, owner_chat_id=_chat_id(srv, session, msg),
                    observed_version=msg.get('expected_version'),
                    reason=str(msg.get("reason") or ""),
                    correlation_id=_request_id(msg),
                )
            else:
                goal = await runtime.archive_async(goal_id, expected_version=expected)
            if goal.completion_policy.get('entrypoint') == 'composer_goal':
                return _with_reports(srv, runtime.composer.snapshot(goal.goal_id))
            return goal.to_dict()

        await _respond(websocket, msg, operation, action)

    @on("goal:state:set")
    async def goal_state_set(srv, websocket, session, msg):
        async def action():
            goal = await asyncio.to_thread(
                _runtime(srv, session, msg).state_set,
                str(msg.get("goal_id") or ""),
                str(msg.get("key") or ""),
                msg.get("value"),
                expected_version=int(msg.get("expected_version") or 0),
            )
            return goal.to_dict()
        await _respond(websocket, msg, "state.set", action)

    @on("goal:verify")
    async def goal_verify(srv, websocket, session, msg):
        async def action():
            report, goal = await asyncio.to_thread(
                _runtime(srv, session, msg).verify,
                str(msg.get("goal_id") or ""),
                expected_version=int(msg.get("expected_version") or 0),
                complete=bool(msg.get("complete", True)),
            )
            return {"verification": report.to_dict(), "goal": goal.to_dict()}
        await _respond(websocket, msg, "verify", action)

    @on("goal:input:answer")
    async def goal_input_answer(srv, websocket, session, msg):
        async def action():
            goal = await asyncio.to_thread(
                _runtime(srv, session, msg).answer_input,
                str(msg.get("attention_id") or ""),
                msg.get("response"),
                expected_version=int(msg.get("expected_version") or 0),
                expected_attention_version=int(
                    msg.get("expected_attention_version") or 1
                ),
            )
            return goal.to_dict()
        await _respond(websocket, msg, "input.answer", action)


__all__ = ["register"]
