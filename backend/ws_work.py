"""Replayable Work Fabric WebSocket inspection and control."""

from __future__ import annotations

import asyncio
from typing import Any


def _runtime(srv: Any) -> Any:
    return srv.require_runtime().work


def _chat_id(srv: Any, session: Any) -> str:
    from ws_protocol import session_chat_id
    value = session_chat_id(session)
    if value:
        return value
    try:
        return str(
            srv.require_runtime().sessions.get_active() or ""
        ).strip()
    except Exception:
        return ""


def _visible(event: Any, chat_id: str) -> bool:
    event_chat = str(getattr(getattr(event, "scope", None), "chat_id", "") or "")
    return not event_chat or not chat_id or event_chat == chat_id


async def _snapshot(srv: Any, session: Any, msg: dict[str, Any]) -> dict[str, Any]:
    runtime = _runtime(srv)
    chat_id = _chat_id(srv, session)
    after = max(0, int(msg.get("after_sequence") or 0))
    limit = max(1, min(int(msg.get("limit") or 100), 500))
    events, jobs = await asyncio.gather(
        asyncio.to_thread(runtime.events.list, after_sequence=after, limit=limit * 2),
        asyncio.to_thread(runtime.jobs.list, chat_id=chat_id, limit=200),
    )
    visible = []
    cursor = after
    for event in events:
        cursor = int(event.sequence)
        if _visible(event, chat_id):
            visible.append(event)
            if len(visible) >= limit:
                break
    state = dict(runtime.state())
    state.pop("database", None)
    return {
        "type": "work:snapshot",
        "schema": "variant1.work-snapshot.v1",
        "request_id": str(msg.get("request_id") or ""),
        "chat_id": chat_id or None,
        "after_sequence": after,
        "cursor": cursor,
        "events": [event.to_dict() for event in visible],
        "jobs": [job.to_dict() for job in jobs],
        "runtime": state,
    }


def register(on):
    @on("work:get")
    async def work_get(srv, websocket, session, msg):
        await websocket.send_json(await _snapshot(srv, session, msg))

    @on("work:events")
    async def work_events(srv, websocket, session, msg):
        snapshot = await _snapshot(srv, session, msg)
        await websocket.send_json({
            "type": "work:events",
            "schema": "variant1.work-events.v1",
            "request_id": snapshot["request_id"],
            "chat_id": snapshot["chat_id"],
            "after_sequence": snapshot["after_sequence"],
            "cursor": snapshot["cursor"],
            "events": snapshot["events"],
        })

    @on("work:jobs")
    async def work_jobs(srv, websocket, session, msg):
        runtime = _runtime(srv)
        chat_id = _chat_id(srv, session)
        raw_statuses = msg.get("statuses")
        statuses = (
            tuple(str(item) for item in raw_statuses)
            if isinstance(raw_statuses, list)
            else ()
        )
        jobs = await asyncio.to_thread(
            runtime.jobs.list,
            chat_id=chat_id,
            statuses=statuses,
            kind=str(msg.get("kind") or ""),
            limit=max(1, min(int(msg.get("limit") or 100), 500)),
        )
        await websocket.send_json({
            "type": "work:jobs",
            "schema": "variant1.work-jobs.v1",
            "request_id": str(msg.get("request_id") or ""),
            "chat_id": chat_id or None,
            "jobs": [job.to_dict() for job in jobs],
        })

    @on("work:job:cancel")
    async def work_job_cancel(srv, websocket, session, msg):
        runtime = _runtime(srv)
        job_id = str(msg.get("job_id") or "").strip()
        request_id = str(msg.get("request_id") or "")
        if not job_id or not request_id:
            await websocket.send_json({
                "type": "work:rejected",
                "request_id": request_id,
                "error": "request_id_and_job_id_required",
            })
            return
        job = await asyncio.to_thread(runtime.jobs.require, job_id)
        chat_id = _chat_id(srv, session)
        if job.scope.chat_id and job.scope.chat_id != chat_id:
            await websocket.send_json({
                "type": "work:rejected",
                "request_id": request_id,
                "error": "job_outside_active_chat",
            })
            return
        expected = msg.get("expected_version")
        updated = await asyncio.to_thread(
            runtime.cancel_job,
            job_id,
            reason=str(msg.get("reason") or ""),
            expected_revision=(int(expected) if expected is not None else None),
        )
        await websocket.send_json({
            "type": "work:accepted",
            "request_id": request_id,
            "operation": "job.cancel",
            "job": updated.to_dict(),
        })


__all__ = ["register"]
