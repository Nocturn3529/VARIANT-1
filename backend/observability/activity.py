"""Unified observability stream: WebSocket hub + activity event broadcast.

A single feed of everything VARIANT-1 is doing in the background — chat
multi-step tasks, headless automations, and webhook runs.
Every event is broadcast to ALL connected clients (overlay, settings, and the
Activity Monitor window) so any of them can show live progress. The overlay
uses it for a lightweight "working…" indicator; the monitor renders the full
live log grouped by run.

A run is one background job. ``Variant1RunContext`` is the sole run identity and
carries run_id/source/session state through graph and tool execution.
``new_run`` stamps title/source on the bound context and returns a small
activity bag for callers that still track step counters on a dict.

Dependency-light: imports run_context only, never server.py.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections import OrderedDict, deque

from run_context import current_run_context


# The avatar overlay is a presence surface, not a second application client.
# Its dedicated subscribers receive only this small, data-minimised projection
# of the process-wide hub.  In particular, chat/session/configuration payloads
# and proactive message text never cross into the overlay renderer.
_PRESENCE_TYPES = frozenset({"activity", "engine", "proactive"})
_MAX_TOOL_RESULT_OBSERVATIONS = 256
_MAX_TOOL_RESULT_VALUE_CHARS = 80
# Latest display-safe step per chat session, so a roster opened mid-run can show
# what a child is doing before its next live frame. Process-local and bounded;
# it is a display hint, never durable evidence.
_LAST_SESSION_ACTIVITY_LIMIT = 512
_LAST_SESSION_ACTIVITY_CHARS = 200
_LAST_SESSION_ACTIVITY: "OrderedDict[str, dict]" = OrderedDict()
_LAST_SESSION_ACTIVITY_LOCK = threading.Lock()
# The current run's steps per chat, so a Deck that opens the chat mid-run can
# rebuild its live timeline (chat:session run_snapshot). Same bounds and
# guarantees as the latest-step hint: process-local and display-only.
_RUN_STEP_LIMIT = 64
_LIVE_RUNS: "OrderedDict[str, dict]" = OrderedDict()


def _bounded_tool_result_value(value, *, default: str = "") -> str:
    """Normalize one schema value without retaining result prose or bodies."""

    text = " ".join(str(value or "").split()).strip().lower()
    return text[:_MAX_TOOL_RESULT_VALUE_CHARS] or default


def _remember_session_activity(message: dict) -> None:
    """Retain the latest broadcast step for its session (display fields only)."""

    session_id = str(message.get("session_id") or "").strip()
    event = str(message.get("event") or "")
    if not session_id or not event or event.startswith("agent_runtime:"):
        return

    def text(key: str) -> str | None:
        value = message.get(key)
        if value is None:
            return None
        return " ".join(str(value).split())[:_LAST_SESSION_ACTIVITY_CHARS] or None

    entry = {
        "event": event[:_LAST_SESSION_ACTIVITY_CHARS],
        "tool": text("tool"),
        "status": text("status"),
        "title": text("title"),
        "ts": float(message.get("ts") or time.time()),
        "run_id": text("run_id"),
    }
    with _LAST_SESSION_ACTIVITY_LOCK:
        _LAST_SESSION_ACTIVITY[session_id] = entry
        _LAST_SESSION_ACTIVITY.move_to_end(session_id)
        while len(_LAST_SESSION_ACTIVITY) > _LAST_SESSION_ACTIVITY_LIMIT:
            _LAST_SESSION_ACTIVITY.popitem(last=False)


def _live_run(session_id: str, run_id: str) -> dict:
    """The live-run record for a chat, replaced when a new run starts."""

    run = _LIVE_RUNS.get(session_id)
    if run is None or run["run_id"] != run_id:
        run = {"run_id": run_id, "steps": OrderedDict(), "live": None, "revision": 0}
        _LIVE_RUNS[session_id] = run
    _LIVE_RUNS.move_to_end(session_id)
    while len(_LIVE_RUNS) > _LAST_SESSION_ACTIVITY_LIMIT:
        _LIVE_RUNS.popitem(last=False)
    return run


def _put_run_step(run: dict, step: dict) -> None:
    run["revision"] += 1
    run["steps"][step["id"]] = step
    while len(run["steps"]) > _RUN_STEP_LIMIT:
        run["steps"].popitem(last=False)


def _remember_run_step(message: dict) -> None:
    """Fold tool start/result events into the chat's live-run record."""

    session_id = str(message.get("session_id") or "").strip()
    run_id = str(message.get("run_id") or "").strip()
    event = str(message.get("event") or "")
    if not session_id or not run_id:
        return
    with _LAST_SESSION_ACTIVITY_LOCK:
        if event == "task:done":
            run = _LIVE_RUNS.get(session_id)
            if run is not None and run["run_id"] == run_id:
                _LIVE_RUNS.pop(session_id, None)
            return
        call_id = str(message.get("call_id") or "").strip()[:128]
        if event not in {"tool:start", "tool:result"} or not call_id:
            return
        run = _live_run(session_id, run_id)
        ts_ms = float(message.get("ts") or time.time()) * 1000
        step = dict(run["steps"].get(call_id) or {
            "id": call_id, "kind": "tool", "call_id": call_id, "ts": ts_ms,
        })
        tool = str(message.get("tool") or step.get("tool") or "")[:80]
        step.update(tool=tool, label=tool or "Tool")
        if event == "tool:start":
            step["status"] = "running"
            step["started_at"] = ts_ms
            preview = message.get("args_preview")
            if preview:
                step["args_preview"] = str(preview)[:600]
        else:
            step["status"] = str(message.get("status") or "ok")[:24]
            step["completed_at"] = ts_ms
            text = message.get("text")
            if text:
                step["result_preview"] = str(text)[:800]
        _put_run_step(run, step)


def _context_run_identity() -> tuple[str, str]:
    ctx = current_run_context()
    if ctx is None:
        return "", ""
    session_id = str(
        ctx.session_id or getattr(ctx.work_scope, "chat_id", "") or ""
    ).strip()
    return session_id, str(ctx.run_id or "")


def remember_run_narration(step: dict) -> None:
    """Keep one narration step (kind "text") on the bound run's live record."""

    session_id, run_id = _context_run_identity()
    if not session_id or not run_id or not step.get("id"):
        return
    with _LAST_SESSION_ACTIVITY_LOCK:
        run = _live_run(session_id, run_id)
        _put_run_step(run, dict(step))
        live = run["live"]
        if live is not None and live[0] == step.get("segment"):
            # That call's text is now this completed narration step, not a
            # reply still being written.
            run["live"] = None


def bind_live_text(segment: int, parts: list, *, admission_id: str = "") -> None:
    """Point the bound run's live record at the streaming call's text parts."""

    session_id, run_id = _context_run_identity()
    if not session_id or not run_id:
        return
    with _LAST_SESSION_ACTIVITY_LOCK:
        run = _live_run(session_id, run_id)
        run["revision"] += 1
        run["live"] = (int(segment), parts, str(admission_id or ""))


def run_snapshot(session_id: str, run_id: str, admission_id: str = "") -> dict | None:
    """Steps so far of the chat's run ``run_id``, plus the current partial text.

    ``admission_id`` is the chat's current admission. Partial text bound by
    another admission of the same logical run (a resumed run) is left out.
    ``revision`` grows with every change, so a client can ignore an older one.
    """

    with _LAST_SESSION_ACTIVITY_LOCK:
        run = _LIVE_RUNS.get(str(session_id or ""))
        if run is None or not run_id or run["run_id"] != run_id:
            return None
        steps = [dict(step) for step in run["steps"].values()]
        segment, parts, live_admission = run["live"] or (0, [], "")
        if admission_id and live_admission and live_admission != admission_id:
            segment, parts = 0, []
        text = "".join(str(part) for part in list(parts))
        revision = int(run["revision"])
    steps.sort(key=lambda step: float(step.get("ts") or 0))
    return {
        "run_id": run_id, "admission_id": str(admission_id or ""),
        "revision": revision, "steps": steps, "segment": segment, "text": text,
    }


def last_session_activity(session_id: str) -> dict | None:
    """Return a copy of the latest retained step for one session, if any."""

    with _LAST_SESSION_ACTIVITY_LOCK:
        entry = _LAST_SESSION_ACTIVITY.get(str(session_id or ""))
        return dict(entry) if entry is not None else None


def presence_projection(message: dict) -> dict | None:
    """Return the read-only avatar projection for a hub message, if any."""
    if not isinstance(message, dict):
        return None
    mtype = str(message.get("type") or "")
    if mtype not in _PRESENCE_TYPES:
        return None
    if mtype == "activity":
        allowed = ("type", "event", "run_id", "source", "mood")
    elif mtype == "engine":
        allowed = ("type", "ready")
    else:
        allowed = ("type", "mood")
    return {key: message[key] for key in allowed if key in message}


class WSHub:
    """Fan out hub messages; each socket gets its own ordered send queue.

    A slow socket never loses later messages because one send was slow: it is
    removed only when a send fails, it is removed explicitly, or it falls
    ``max_queued`` messages behind, in which case it is closed so the client
    reconnects and resynchronizes.
    """

    def __init__(self, *, send_timeout_s: float = 1.0, max_queued: int = 2048):
        self.active = set()
        self.presence_subscribers = set()
        # broadcast() waits at most this long for delivery; slower sockets
        # keep their queue and catch up in order.
        self.send_timeout_s = max(0.01, float(send_timeout_s))
        self.max_queued = max(1, int(max_queued))
        self._outboxes: dict = {}
        self._drainers: dict = {}
        self._closing: set = set()

    def add(self, ws):
        self.active.add(ws)

    def remove(self, ws):
        self.active.discard(ws)
        if ws not in self.presence_subscribers:
            self._discard_outbox(ws)

    def add_presence_subscriber(self, ws):
        self.presence_subscribers.add(ws)

    def remove_presence_subscriber(self, ws):
        self.presence_subscribers.discard(ws)
        if ws not in self.active:
            self._discard_outbox(ws)

    def _discard_outbox(self, ws) -> None:
        for _payload, future in self._outboxes.pop(ws, ()):
            if not future.done():
                future.set_result(False)
        drainer = self._drainers.pop(ws, None)
        if drainer is not None and drainer is not asyncio.current_task():
            drainer.cancel()

    def _drop(self, ws, *, close: bool = False) -> None:
        self.active.discard(ws)
        self.presence_subscribers.discard(ws)
        self._discard_outbox(ws)
        closer = getattr(ws, "close", None) if close else None
        if callable(closer):
            async def _close():
                try:
                    await closer()
                except Exception:
                    pass  # The socket is already gone or closing.
            task = asyncio.create_task(_close())
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)

    def _enqueue(self, ws, payload):
        outbox = self._outboxes.setdefault(ws, deque())
        if len(outbox) >= self.max_queued:
            # Too far behind to catch up: close so the client resyncs on
            # reconnect instead of silently missing messages.
            self._drop(ws, close=True)
            return None
        future = asyncio.get_running_loop().create_future()
        outbox.append((payload, future))
        drainer = self._drainers.get(ws)
        if drainer is None or drainer.done():
            self._drainers[ws] = asyncio.create_task(self._drain(ws))
        return future

    async def _drain(self, ws) -> None:
        outbox = self._outboxes.get(ws)
        try:
            while outbox:
                payload, future = outbox[0]
                await ws.send_json(payload)
                outbox.popleft()
                if not future.done():
                    future.set_result(True)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed send means the socket is closed or broken.
            self._drop(ws)
        finally:
            if self._drainers.get(ws) is asyncio.current_task():
                self._drainers.pop(ws, None)

    async def broadcast(self, message: dict):
        deliveries = [(ws, message) for ws in list(self.active)]
        projected = presence_projection(message)
        if projected is not None:
            deliveries.extend(
                (ws, projected) for ws in list(self.presence_subscribers)
            )
        futures = [
            future for future in (
                self._enqueue(ws, payload) for ws, payload in deliveries
            )
            if future is not None
        ]
        if futures:
            await asyncio.wait(futures, timeout=self.send_timeout_s)


HUB = WSHub()


def new_run(source, title):
    """Begin an activity run under the bound ``Variant1RunContext``.

    Returns a small mutable bag ``{id, source, title, step}`` for callers that
    still update step counters. Identity for ``emit_activity`` comes only from
    the bound context — there is no process-global fallback ContextVar.
    """
    ctx = current_run_context()
    title_s = (title or "").strip()[:200]
    run_id = ctx.run_id if ctx else uuid.uuid4().hex[:12]
    run = {
        "id": run_id,
        "source": source,
        "title": title_s,
        "step": 0,
    }
    if ctx is not None:
        ctx.source = source
        ctx.metadata["title"] = title_s
        # Optional mirror for debugging / Activity Monitor correlation.
        ctx.metadata["_activity_run"] = run
    else:
        # Soft fail: production paths bind Variant1RunContext before new_run.
        print(
            f"[activity] new_run without Variant1RunContext source={source!r}",
            flush=True,
        )
    return run


async def emit_activity(event: str, **fields) -> None:
    """Timestamp and broadcast one activity event to every client.

    event: task:start | task:step | task:thinking | tool:start |
           tool:result | task:done | note
    Common fields: source, run_id, title, step, tool, args_preview, text,
                   status ("running"/"ok"/"error"/"skipped"),
                   surface ("main"|"side"|"both") for Codex-like main vs side.
    task:done status: ok | failed | error | cancelled.
    Terminal ``task:done`` events may carry the tools used by the run.
    Broadcasting never raises into the caller — a dead socket is just dropped.

    Run identity (run_id / source / desktop session) is read only from the
    bound ``Variant1RunContext``. Untagged emits are allowed but lack run_id.
    """
    from observability.display_projection import safe_display_fields
    fields = safe_display_fields(fields)
    ctx = current_run_context()
    msg = {"type": "activity", "event": event, "ts": round(time.time(), 3)}
    try:
        from coworker import activity_surface
        msg.setdefault("surface", activity_surface(event))
    except Exception:
        msg.setdefault("surface", "side")
    if ctx is not None:
        if str(event or "") == "tool:start" and fields.get("tool"):
            calls = list((ctx.metadata or {}).get("_tool_call_names") or [])
            if len(calls) < 256:
                calls.append(str(fields.get("tool") or "")[:80])
                ctx.metadata["_tool_call_names"] = calls
        if (str(event or "") == "tool:result" and fields.get("tool")
                and fields.get("call_id")):
            observations = list(
                (ctx.metadata or {}).get("_tool_result_observations") or []
            )
            if len(observations) < _MAX_TOOL_RESULT_OBSERVATIONS:
                observations.append({
                    "status": _bounded_tool_result_value(
                        fields.get("status"), default="unknown"
                    ),
                    "error_code": _bounded_tool_result_value(
                        fields.get("error_code")
                    ),
                })
                ctx.metadata["_tool_result_observations"] = observations
            else:
                ctx.metadata["_tool_result_observations_truncated"] = min(
                    1_000_000,
                    int(ctx.metadata.get("_tool_result_observations_truncated") or 0)
                    + 1,
                )
        if str(event or "") == "task:done" and fields.get("status"):
            ctx.metadata["_terminal_status"] = str(fields.get("status") or "")[:24]
        if str(event or "").startswith("tool:") and fields.get("tool"):
            used = list((ctx.metadata or {}).get("tools_used") or [])
            tool_name = str(fields.get("tool") or "")
            if tool_name and tool_name not in used:
                used.append(tool_name)
                ctx.metadata["tools_used"] = used
        msg.setdefault("run_id", ctx.run_id)
        msg.setdefault("source", ctx.source)
        session_id = str(
            ctx.session_id or getattr(ctx.work_scope, "chat_id", "") or ""
        ).strip()
        if session_id:
            msg.setdefault("session_id", session_id)
        binding_id = ctx.desktop_binding_id
        if binding_id:
            msg.setdefault("desktop_binding_id", binding_id)
    msg.update({k: v for k, v in fields.items() if v is not None})
    try:
        _remember_session_activity(msg)
        _remember_run_step(msg)
    except Exception:
        pass
    # Graph events are recorded by agent_engine.state.queue_event so their
    # checkpoint-tail projection and external trace share one source.  Record
    # all other activity here, including tool call/result lifecycle events.
    trace_activity = not str(event or "").startswith("agent_runtime:")
    # The tool runner records the authoritative start with call_id and an
    # argument hash.  The UI's older start notification intentionally omits
    # that identity and would otherwise create a duplicate trace span.
    if str(event or "") == "tool:start" and not fields.get("call_id"):
        trace_activity = False
    if trace_activity:
        try:
            from observability.trace_events import record_activity_message

            record_activity_message(msg)
        except Exception:
            pass
    try:
        await HUB.broadcast(msg)
    except Exception:
        pass


def clip(s: str, n: int) -> str:
    """Collapse whitespace and clip a result string for the activity feed."""
    s = " ".join(str(s).split())
    return s[:n] + ("…" if len(s) > n else "")


def args_preview(args: dict) -> str:
    """Short one-line argument preview for tool activity events."""
    from observability.display_projection import safe_display
    args = safe_display(args)
    try:
        value = json.dumps(args, ensure_ascii=False, default=str)
    except Exception:
        value = str(args)
    value = " ".join(value.split())
    return value[:160] + ("…" if len(value) > 160 else "")
