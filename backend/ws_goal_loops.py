"""WebSocket projection of durable goal records."""

from typing import Any

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


def register(on):
    # The former project-run UI is now a read/write projection of Goal records.
    @on("goals:loops:list")
    async def goals_list(srv, websocket, session, msg):
        goals = srv.require_runtime().goals.list(
            owner_chat_id="",
            limit=max(1, min(int(msg.get("limit") or 50), 200)),
        )
        items = [_goal_summary(goal) for goal in goals]
        await websocket.send_json({
            "type": "goals:loops", "items": items, "count": len(items),
            "active_count": sum(item["status"] == "active" for item in items),
            "projection": "goals",
        })

    @on("goals:loops:get")
    async def goal_get(srv, websocket, session, msg):
        item = _goal_detail(
            srv.require_runtime().goals,
            str(msg.get("id") or msg.get("loop_id") or ""),
        )
        await websocket.send_json({
            "type": "goals:loop", "item": item, "projection": "goal",
        })

    @on("goals:loops:create")
    async def goal_create(srv, websocket, session, msg):
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
            "type": "goals:loop",
            "item": _goal_detail(srv.require_runtime().goals, goal.goal_id),
            "projection": "goal",
        })
        await goals_list(srv, websocket, session, {"limit": 50})

    @on("goals:loops:control")
    async def goal_control(srv, websocket, session, msg):
        service = srv.require_runtime().goals
        goal_id = str(msg.get("id") or msg.get("loop_id") or "")
        action = str(msg.get("action") or "").lower()
        goal = service.get(goal_id)
        if goal is None:
            await websocket.send_json({"type": "goals:loop", "item": None})
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
                reason="paused from Goals page",
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
                    reason="archived from Goals page",
                )
            goal = service.archive(goal_id, expected_version=goal.version)
        await websocket.send_json({
            "type": "goals:loop",
            "item": (None if action == "delete" else _goal_detail(service, goal_id)),
            "deleted": action == "delete", "id": goal_id,
            "projection": "goal",
        })
        await goals_list(srv, websocket, session, {"limit": 50})

__all__ = ["register"]
