"""Memory UI projection over the approved-memory store."""

from __future__ import annotations

import asyncio
import os
from typing import Any


def _service(srv: Any):
    return srv.require_runtime().memory.store


def _chat_id(srv: Any, session: Any) -> str:
    from ws_protocol import session_chat_id
    return str(
        session_chat_id(session)
        or srv.require_runtime().sessions.get_active()
        or ""
    )


def _goal_status(value: str) -> str:
    if value == "paused":
        return "paused"
    if value == "archived":
        return "archived"
    if value in {"succeeded", "failed", "cancelled"}:
        return "done"
    return "active"


def _goal_summary(goal: Any) -> dict[str, Any]:
    return {
        "id": goal.goal_id,
        "title": goal.title,
        "status": _goal_status(goal.status),
        "created": goal.created_at,
        "updated": goal.updated_at,
        "goal": goal.objective,
        "done_count": 1 if goal.status == "succeeded" else 0,
        "blocked_count": 1 if goal.status in {"blocked", "failed"} else 0,
        "next_count": 0 if goal.terminal else 1,
        "session_id": goal.owner_chat_id or None,
    }


def _goal_detail(service: Any, goal_id: str) -> dict[str, Any] | None:
    goal = service.get(goal_id)
    if goal is None:
        return None
    snapshot = service.snapshot(goal_id)
    steps = list(snapshot.get("steps") or ())
    return {
        "id": goal.goal_id,
        "meta": {
            "id": goal.goal_id,
            "title": goal.title,
            "status": _goal_status(goal.status),
            "session_id": goal.owner_chat_id,
            "created": goal.created_at,
            "updated": goal.updated_at,
        },
        "charter": {
            "goal": goal.objective,
            "constraints": list(goal.constraints),
            "success_criteria": [
                str(item.get("description") or item.get("criterion") or item)
                for item in goal.success_criteria
            ],
        },
        "progress": {
            "done": [str(item.get("instructions") or item.get("step_id"))
                     for item in steps if item.get("status") == "succeeded"],
            "blocked": [str(item.get("instructions") or item.get("step_id"))
                        for item in steps if item.get("status") in {"blocked", "failed"}],
            "next": [str(item.get("instructions") or item.get("step_id"))
                     for item in steps if item.get("status") in {"pending", "ready"}],
            "notes": goal.pause_reason,
        },
        "anchors": {"domains": {}},
    }


async def _send_core(srv: Any, websocket: Any) -> None:
    items = _service(srv).profile_items()
    await websocket.send_json({
        "type": "memory:core", "items": items,
        "count": len(items), "cap": 100,
    })


async def _send_list(srv: Any, websocket: Any, *, limit=200, offset=0) -> None:
    service = _service(srv)
    items = service.list_items(limit=limit, offset=offset)
    total = service.count_items()
    await websocket.send_json({
        "type": "memory:list", "items": items, "count": total,
        "limit": limit, "offset": offset,
        "has_more": offset + len(items) < total,
    })


async def _send_proposals(srv: Any, websocket: Any, *, limit=100) -> None:
    items = _service(srv).list_proposals(state="pending", limit=limit)
    await websocket.send_json({
        "type": "memory:proposals",
        "items": items,
        "count": len(items),
    })


def register(on):
    @on("memory:core:get")
    async def memory_core_get(srv, websocket, session, msg):
        await _send_core(srv, websocket)

    @on("memory:core:set")
    async def memory_core_set(srv, websocket, session, msg):
        text = str(msg.get("text") or msg.get("fact") or "").strip()
        if text:
            _service(srv).remember_explicit(
                _chat_id(srv, session), text,
                metadata={"type": "profile", "source": "manual",
                          "importance": 5, "subject": "user"},
                actor="user",
            )
        await _send_core(srv, websocket)

    @on("memory:core:delete")
    async def memory_core_delete(srv, websocket, session, msg):
        text = str(msg.get("query") or msg.get("text") or "").strip()
        item = _service(srv).find_profile(text)
        if item is not None:
            _service(srv).tombstone(
                item["item_id"], expected_revision=int(item["version"]),
                actor="user", reason="removed from core profile",
            )
        await _send_core(srv, websocket)

    @on("memory:core:update")
    async def memory_core_update(srv, websocket, session, msg):
        prior = str(msg.get("prior") or "").strip()
        text = str(msg.get("text") or "").strip()
        item = _service(srv).find_profile(prior)
        if item is None or not text:
            await websocket.send_json({
                "type": "memory:error", "error": "Core fact could not be updated",
            })
        else:
            proposal = _service(srv).propose(
                _chat_id(srv, session), kind="update", content=text,
                item_id=item["item_id"], expected_revision=int(item["version"]),
                scope="user", provenance="user_edit", confidence=1.0,
                metadata=dict(item.get("metadata") or {}),
            )
            _service(srv).approve(proposal["proposal_id"], actor="user")
        await _send_core(srv, websocket)

    @on("memory:list")
    async def memory_list(srv, websocket, session, msg):
        await _send_list(
            srv, websocket,
            limit=max(1, min(int(msg.get("limit") or 200), 500)),
            offset=max(0, int(msg.get("offset") or 0)),
        )

    @on("memory:proposals")
    async def memory_proposals(srv, websocket, session, msg):
        await _send_proposals(
            srv,
            websocket,
            limit=max(1, min(int(msg.get("limit") or 100), 500)),
        )

    @on("memory:proposal:approve")
    async def memory_proposal_approve(srv, websocket, session, msg):
        proposal_id = str(msg.get("proposal_id") or "")
        override = msg.get("content")
        try:
            _service(srv).approve(
                proposal_id,
                actor="user",
                content_override=(str(override) if override is not None else None),
            )
        except (LookupError, RuntimeError, ValueError) as exc:
            await websocket.send_json({
                "type": "memory:error",
                "error": str(exc),
            })
        await _send_proposals(srv, websocket)
        await _send_list(srv, websocket)
        await _send_core(srv, websocket)

    @on("memory:proposal:reject")
    async def memory_proposal_reject(srv, websocket, session, msg):
        proposal_id = str(msg.get("proposal_id") or "")
        try:
            _service(srv).reject(
                proposal_id,
                actor="user",
                note=str(msg.get("note") or "Rejected from Memory"),
            )
        except (LookupError, RuntimeError, ValueError) as exc:
            await websocket.send_json({
                "type": "memory:error",
                "error": str(exc),
            })
        await _send_proposals(srv, websocket)

    @on("memory:delete")
    async def memory_delete(srv, websocket, session, msg):
        item_id = str(msg.get("id") or "")
        item = _service(srv).get_item(item_id)
        if item is not None:
            _service(srv).tombstone(
                item_id, expected_revision=int(item["version"]),
                actor="user", reason="removed from Memory UI",
            )
        await _send_list(srv, websocket)

    @on("memory:clear")
    async def memory_clear(srv, websocket, session, msg):
        _service(srv).clear(actor="user", preserve_profile=True)
        await _send_list(srv, websocket)

    @on("memory:consolidate")
    async def memory_consolidate(srv, websocket, session, msg):
        removed = await srv.require_runtime().memory.consolidate_once()
        await websocket.send_json({
            "type": "memory:consolidate", "removed": removed, "ok": True,
        })
        await _send_list(srv, websocket)

    @on("memory:export")
    async def memory_export(srv, websocket, session, msg):
        destination = os.path.join(
            srv.data_dir, "data", "memory-export.jsonl"
        )
        count = await asyncio.to_thread(
            _service(srv).export_jsonl, destination
        )
        await websocket.send_json({
            "type": "memory:export", "path": destination, "count": count,
        })

    # The former project-run UI is now a read/write projection of Goal records.
    @on("memory:loops:list")
    async def memory_goals_list(srv, websocket, session, msg):
        goals = srv.require_runtime().goals.list(
            owner_chat_id="",
            limit=max(1, min(int(msg.get("limit") or 50), 200)),
        )
        items = [_goal_summary(goal) for goal in goals]
        await websocket.send_json({
            "type": "memory:loops", "items": items, "count": len(items),
            "active_count": sum(item["status"] == "active" for item in items),
            "projection": "goals",
        })

    @on("memory:loops:get")
    async def memory_goal_get(srv, websocket, session, msg):
        item = _goal_detail(
            srv.require_runtime().goals,
            str(msg.get("id") or msg.get("loop_id") or ""),
        )
        await websocket.send_json({
            "type": "memory:loop", "item": item, "projection": "goal",
        })

    @on("memory:loops:create")
    async def memory_goal_create(srv, websocket, session, msg):
        goal = srv.require_runtime().goals.create(
            title=str(msg.get("title") or msg.get("goal") or "Goal run")[:500],
            objective=str(msg.get("goal") or msg.get("title") or "")[:32_000],
            owner_chat_id=_chat_id(srv, session),
            constraints=list(msg.get("constraints") or ()),
            success_criteria=[
                {"criterion": str(item)}
                for item in list(
                    msg.get("success_criteria") or msg.get("criteria") or ()
                )
            ],
        )
        await websocket.send_json({
            "type": "memory:loop",
            "item": _goal_detail(srv.require_runtime().goals, goal.goal_id),
            "projection": "goal",
        })
        await memory_goals_list(srv, websocket, session, {"limit": 50})

    @on("memory:loops:control")
    async def memory_goal_control(srv, websocket, session, msg):
        service = srv.require_runtime().goals
        goal_id = str(msg.get("id") or msg.get("loop_id") or "")
        action = str(msg.get("action") or "").lower()
        goal = service.get(goal_id)
        if goal is None:
            await websocket.send_json({"type": "memory:loop", "item": None})
            return
        repo = service.repository
        actor = service._actor("user")
        if action == "pause":
            if goal.status == "draft":
                goal = repo.transition_goal(
                    goal_id, "queued", expected_version=goal.version, actor=actor
                )
            goal = service.pause(
                goal_id, expected_version=goal.version,
                reason="paused from Memory projection",
            )
        elif action in {"resume", "activate"}:
            goal = service.resume(
                goal_id, expected_version=goal.version, enqueue=False
            )
        elif action in {"complete", "done"}:
            for target in (
                ["queued", "running", "succeeded"] if goal.status == "draft"
                else ["running", "succeeded"] if goal.status == "queued"
                else ["running", "succeeded"] if goal.status == "paused"
                else ["succeeded"]
            ):
                goal = repo.transition_goal(
                    goal_id, target, expected_version=goal.version, actor=actor
                )
        elif action in {"archive", "delete"}:
            if not goal.terminal:
                goal = await service.cancel_async(
                    goal_id, expected_version=goal.version,
                    reason="archived from Memory projection",
                )
            goal = service.archive(goal_id, expected_version=goal.version)
        await websocket.send_json({
            "type": "memory:loop",
            "item": (None if action == "delete" else _goal_detail(service, goal_id)),
            "deleted": action == "delete", "id": goal_id,
            "projection": "goal",
        })
        await memory_goals_list(srv, websocket, session, {"limit": 50})

__all__ = ["register"]
