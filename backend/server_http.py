"""HTTP + WebSocket endpoint helpers for the composition root.

Keeps route bodies out of ``server.py`` while still reading live hub globals
through the ``srv`` module (or AppHost-synced server module) passed in.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Any, Callable

from fastapi import Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from security import secretstore
import ws_dispatch

class _NullWebSocket:
    """Best-effort fallback transport after the owning socket has gone away."""

    async def send_json(self, _message: Any) -> None:
        return None


def health_payload(srv: Any) -> dict:
    installed = getattr(srv, "runtime", None)
    memory = getattr(getattr(installed, "memory", None), "store", None)
    router = srv.router
    ready = bool(getattr(srv, "startup_ready", False))
    startup_error = str(getattr(srv, "startup_error", "") or "")
    runtime = getattr(srv, "runtime", None)
    work = getattr(runtime, "work", None)
    sessions = getattr(runtime, "sessions", None)
    return {
        "status": "ok" if ready else ("error" if startup_error else "starting"),
        "ready": ready,
        "startup_error": startup_error,
        "startup_failures": list(getattr(srv, "startup_failures", None) or []),
        "engine": srv.engine_label(),
        "engine_ready": router.engine_ready,
        "model": router.model_name,
        "memory": memory is not None,
        "memory_count": memory.count_items() if memory is not None else 0,
        "work": {
            "ready": bool(work and getattr(work, "started", False)),
            "scheduler_running": bool(
                work and getattr(getattr(work, "scheduler", None), "running", False)
            ),
        },
        "sessions": {
            "ready": bool(sessions is not None),
        },
        "version": srv.version,
        "instance_id": srv.instance_id,
        "uptime_s": round(time.time() - srv.start_time, 1),
    }


def require_loopback_bearer(auth_token: str, request: Request) -> JSONResponse | None:
    """Return an error response when the caller is not the local control plane."""
    client_host = str(getattr(getattr(request, "client", None), "host", "") or "")
    if client_host not in {"127.0.0.1", "::1"}:
        return JSONResponse({"ok": False, "error": "loopback only"}, status_code=403)
    provided = str(request.headers.get("authorization", "") or "")
    expected = f"Bearer {auth_token}"
    if not auth_token or not secrets.compare_digest(provided, expected):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    return None


def handle_shutdown(runtime: dict, auth_token: str, request: Request) -> JSONResponse:
    """Authorize Electron's loopback-only graceful process shutdown request."""
    denied = require_loopback_bearer(auth_token, request)
    if denied is not None:
        return denied
    server = runtime.get("server")
    if server is None:
        return JSONResponse({"ok": False, "error": "server unavailable"}, status_code=503)
    server.should_exit = True
    return JSONResponse({"ok": True}, status_code=202)


async def handle_webhook(srv: Any, token: str, request: Request) -> JSONResponse:
    """Fire a webhook-triggered automation (loopback + token gate)."""
    task = srv.automations.get_by_webhook(token)
    if not task:
        return JSONResponse(
            {"ok": False, "error": "no matching enabled webhook"}, status_code=404)
    try:
        body = (await request.body())[:8000].decode("utf-8", errors="replace")
    except Exception:
        body = ""
    task_id = str(task.get("id") or "")
    request_key = str(
        request.headers.get("idempotency-key")
        or request.headers.get("x-request-id")
        or ""
    ).strip()[:200]
    claim_id = f"webhook:{task_id}:{request_key}" if request_key else ""
    try:
        claim = srv.automations.admit_trigger(
            task_id,
            payload=body,
            source="webhook",
            claim_id=claim_id,
        )
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"webhook could not be admitted: {exc}"},
            status_code=503,
        )

    admitted_task = dict(task)
    admitted_task["_trigger_claim_id"] = str(claim["claim_id"])
    try:
        work_job_id = await srv.require_runtime().workflows.run_automation(
            admitted_task, payload=body, trigger_source="webhook",
        )
    except Exception as exc:
        # The scheduler's durable-claim scan owns retry from this point.
        print(f"[webhook] admitted claim awaits scheduler: {exc}", flush=True)
        work_job_id = ""
    return JSONResponse(
        {"ok": True, "triggered": task.get("name", ""),
         "claim_id": claim["claim_id"], "work_job_id": work_job_id}, status_code=202)


def hello_payload(srv: Any, orphan: dict | None) -> dict:
    router = srv.router
    memory = srv.require_runtime().memory.store
    return {
        "type": "hello",
        "ready": bool(getattr(srv, "startup_ready", False)),
        "startup_error": str(getattr(srv, "startup_error", "") or ""),
        "version": srv.version,
        "engine": srv.engine_label(),
        "engine_ready": router.engine_ready,
        "model_ready": bool(
            router.cloud_route_ready()
            if router.mode == "cloud"
            else router.engine_ready
        ),
        "model": router.model_name,
        "cloud_model": router.get_cloud_model(),
        "memory": memory is not None,
        "mode": router.mode,
        "provider": router.cloud_provider,
        "hardware": srv.hardware,
        "keys": {p: router.has_cloud_key(p)
                 for p in (
                     "anthropic", "openai", "openai-codex", "xai",
                     "nvidia", "gemini",
                 )},
        "dpapi": secretstore.is_available(),
        "sampling": router.sampling,
        "agent_mode": srv.agent_mode(),
        "orphaned_task": orphan,
    }


async def cancel_session_work(
    session: Any,
    *,
    host: Any = None,
    websocket: Any = None,
    runtime_registry: Any = None,
) -> None:
    """Stop and durably finalize work whose owning socket disconnected."""
    request_interrupt = getattr(session, "request_interrupt", None)
    if callable(request_interrupt):
        request_interrupt()

    tasks = []
    active = getattr(session, "active", None)
    turn_task = getattr(active, "turn_task", None) if active is not None else None
    turn_seq = int(getattr(session, "latest_turn_seq", 0) or 0)
    chat_id = str(
        getattr(active, "runtime_chat_id", "")
        or getattr(active, "turn_session_id", "")
        or getattr(session, "viewed_session_id", "")
        or ""
    )
    admission_id = str(
        getattr(active, "runtime_admission_id", "") or ""
    )
    display_text = str(
        getattr(active, "turn_display_user_text", "") or ""
    ).strip()
    had_turn = bool(
        turn_task is not None
        or getattr(active, "task", None) is not None
        or admission_id
        or display_text
    )
    if runtime_registry is not None and admission_id:
        runtime_registry.begin_run_finalization(admission_id)
    transcribe_task = getattr(session, "transcribe_task", None)
    tts_preview_task = getattr(session, "tts_preview_task", None)
    for task in (turn_task, transcribe_task, tts_preview_task):
        if task is None or task.done():
            continue
        if task not in tasks:
            tasks.append(task)
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    finalized_by_turn = (
        getattr(session, "last_cancelled_turn_seq", None) == turn_seq
    )
    if host is not None:
        from chat_finalize import (
            persist_unfinalized_turn,
            publish_terminal_event,
            settle_undelivered_inputs,
        )
        from chat_stream import stream_meta

        fallback_socket = websocket or _NullWebSocket()
        await settle_undelivered_inputs(
            host,
            fallback_socket,
            session,
            chat_id,
            reason="owner_disconnect",
            runtime_registry=runtime_registry,
        )
        if had_turn and display_text and not finalized_by_turn:
            terminal_reply = str(
                getattr(active, "terminal_reply", "") or ""
            ).strip()
            stopped_text = terminal_reply or "Task stopped."
            preserved_terminal = bool(terminal_reply)
            meta = stream_meta(session)
            durable = await persist_unfinalized_turn(
                host,
                fallback_socket,
                session,
                stopped_text,
                meta=meta,
            )
            active.terminal_sent = True
            terminal_event = {
                "type": "done",
                "mood": str(getattr(session, "mood", "neutral") or "neutral"),
                "text": stopped_text,
                "durable": bool(durable),
                **meta,
            }
            if not preserved_terminal:
                terminal_event["cancelled"] = True
            await publish_terminal_event(host.hub, fallback_socket, terminal_event, transports=(
                runtime_registry.attached_transports(chat_id)
                if runtime_registry is not None and chat_id
                else None
            ))
    clear_active_turn = getattr(session, "clear_active_turn", None)
    if callable(clear_active_turn):
        clear_active_turn()
    if getattr(session, "transcribe_task", None) is not None:
        if session.transcribe_task.done():
            session.transcribe_task = None
    if getattr(session, "tts_preview_task", None) is not None:
        if session.tts_preview_task.done():
            session.tts_preview_task = None
    if hasattr(session, "busy"):
        session.busy = False
    if runtime_registry is not None and admission_id:
        runtime_registry.finish_run(admission_id, status="owner_disconnect")


async def websocket_endpoint(srv: Any, websocket: WebSocket,
                             *, auth_token: str,
                             session_factory: Callable[[], Any]) -> None:
    """Authenticated WS loop: hello hydrate + dispatch table."""
    token = websocket.query_params.get("token", "")
    if not token or not secrets.compare_digest(token, auth_token):
        await websocket.close(code=1008)
        return

    sessions = srv.require_runtime().sessions
    view_role = str(
        websocket.query_params.get("view_role", "main") or "main"
    ).strip().lower()
    if view_role not in {"main", "detached_chat"}:
        await websocket.close(code=1008)
        return
    if view_role == "detached_chat":
        initial_chat_id = str(
            websocket.query_params.get("view_chat_id", "") or ""
        ).strip()
        if not initial_chat_id or not sessions.has_session(initial_chat_id):
            await websocket.close(code=1008)
            return
    else:
        initial_chat_id = sessions.get_active()

    await websocket.accept()
    # Successful wire traffic is intentionally not mirrored to stdout. Domain
    # events and failures already have structured operational/trace records.
    srv.hub.add(websocket)
    clients = len(getattr(srv.hub, "active", ()) or ())
    print(f"[ws] open clients={clients}", flush=True)
    logged = websocket
    session = session_factory()
    session.view_role = view_role
    session_runtimes = srv.require_runtime().session_runtimes
    try:
        session_runtimes.attach(
            initial_chat_id,
            getattr(session, "attachment_id", ""),
            session,
            logged,
        )
        orphan = (
            None
            if view_role == "detached_chat"
            else srv.require_runtime().chat.orphaned_task_payload(initial_chat_id)
        )
        hello = hello_payload(srv, orphan)
        if view_role == "detached_chat":
            hello = {
                **hello,
                "view_role": "detached_chat",
                "view_chat_id": initial_chat_id,
            }
        await logged.send_json(hello)
        if orphan and view_role != "detached_chat":
            await logged.send_json({"type": "orphaned_task", **orphan})
        await logged.send_json(srv.require_runtime().tool_settings.state())
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                print("[ws] in type=? error=invalid_json", flush=True)
                await logged.send_json({"type": "error", "error": "invalid_json"})
                continue
            mtype = str(msg.get("type") or "") if isinstance(msg, dict) else ""
            if view_role == "detached_chat":
                from chat_session import detached_message_error

                error = detached_message_error(msg, initial_chat_id) if isinstance(msg, dict) else ""
                if error:
                    await logged.send_json({"type": "error", "error": error})
                    continue
                target = str(
                    msg.get("session_id") or msg.get("chat_id") or ""
                ).strip() if isinstance(msg, dict) else ""
                if mtype == "chat" and isinstance(msg, dict) and not target:
                    msg = {**msg, "session_id": initial_chat_id}
            if mtype == "browser:host:result":
                # Dedicated response fast lane. An active model turn may be
                # awaiting this exact visible-browser frame; do not enqueue it
                # behind ordinary dispatch.
                from browser_fabric import interactive
                interactive.resolve_host_result(
                    logged,
                    str(msg.get("id") or ""),
                    msg.get("result"),
                )
                continue
            handled = await ws_dispatch.dispatch(srv, logged, session, msg)
            if not handled:
                mtype = msg.get("type") if isinstance(msg, dict) else None
                print(f"[ws] in type={mtype!s} error=unknown_type", flush=True)
                await logged.send_json({
                    "type": "error",
                    "error": f"unknown_type:{mtype}",
                })
    except WebSocketDisconnect:
        pass
    finally:
        if view_role != "detached_chat":
            try:
                from browser_fabric.interactive import unregister_host
                unregister_host(logged)
            except Exception:
                pass
        srv.hub.remove(websocket)
        disconnected_chat_id = str(
            getattr(session.active, "runtime_chat_id", "")
            or getattr(session, "viewed_session_id", "")
            or ""
        )
        if view_role == "detached_chat":
            detached_tasks = session_runtimes.detach(
                getattr(session, "attachment_id", ""),
                cancel_unobserved_foreground=False,
            )
        else:
            detached_tasks = session_runtimes.detach(
                getattr(session, "attachment_id", "")
            )
        if detached_tasks:
            await asyncio.gather(*detached_tasks, return_exceptions=True)
        # Only the last attachment owns disconnect cancellation. A remaining
        # Deck keeps the durable chat observable and can stop its admitted run.
        attachment_count = getattr(session_runtimes, "attachment_count", None)
        remaining_attachments = (
            int(attachment_count(disconnected_chat_id) or 0)
            if callable(attachment_count)
            else 0
        )
        if remaining_attachments == 0 and view_role != "detached_chat":
            await cancel_session_work(
                session,
                host=srv,
                websocket=logged,
                runtime_registry=session_runtimes,
            )
        clients = len(getattr(srv.hub, "active", ()) or ())
        print(f"[ws] close clients={clients}", flush=True)


async def activity_websocket_endpoint(
    srv: Any,
    websocket: WebSocket,
    *,
    activity_token: str,
) -> None:
    """Read-only, data-minimised presence feed for the avatar overlay.

    This socket is deliberately never attached to a ``ConnectionSession`` and
    never enters the command dispatcher.  Any application frame sent by the
    client closes the connection, so possession of this scoped token cannot be
    upgraded into chat, credential, configuration, or cancellation authority.
    """
    token = websocket.query_params.get("token", "")
    if (
        not token
        or not activity_token
        or not token.isascii()
        or not activity_token.isascii()
        or not secrets.compare_digest(token, activity_token)
    ):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    srv.hub.add_presence_subscriber(websocket)
    print("[ws:activity] open", flush=True)
    try:
        await websocket.send_json({
            "type": "hello",
            "engine_ready": bool(srv.router.engine_ready),
        })
        while True:
            event = await websocket.receive()
            if event.get("type") == "websocket.disconnect":
                break
            # This is a subscriber, not an RPC surface.  Even a harmless ping
            # is rejected rather than establishing an inbound allowlist that
            # could accidentally grow over time.
            await websocket.close(code=1008)
            break
    except WebSocketDisconnect:
        pass
    finally:
        srv.hub.remove_presence_subscriber(websocket)
        print("[ws:activity] close", flush=True)
