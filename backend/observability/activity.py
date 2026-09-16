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
import time
import uuid

from run_context import current_run_context


# The avatar overlay is a presence surface, not a second application client.
# Its dedicated subscribers receive only this small, data-minimised projection
# of the process-wide hub.  In particular, chat/session/configuration payloads
# and proactive message text never cross into the overlay renderer.
_PRESENCE_TYPES = frozenset({"activity", "engine", "proactive"})
_MAX_TOOL_RESULT_OBSERVATIONS = 256
_MAX_TOOL_RESULT_VALUE_CHARS = 80


def _bounded_tool_result_value(value, *, default: str = "") -> str:
    """Normalize one schema value without retaining result prose or bodies."""

    text = " ".join(str(value or "").split()).strip().lower()
    return text[:_MAX_TOOL_RESULT_VALUE_CHARS] or default


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
    def __init__(self, *, send_timeout_s: float = 1.0):
        self.active = set()
        self.presence_subscribers = set()
        self.send_timeout_s = max(0.01, float(send_timeout_s))

    def add(self, ws):
        self.active.add(ws)

    def remove(self, ws):
        self.active.discard(ws)

    def add_presence_subscriber(self, ws):
        self.presence_subscribers.add(ws)

    def remove_presence_subscriber(self, ws):
        self.presence_subscribers.discard(ws)

    @staticmethod
    def _consume_send_result(task: asyncio.Task) -> None:
        """Retrieve a detached send result after timing it out/cancelling it."""
        try:
            task.exception()
        except (Exception, asyncio.CancelledError):
            pass

    async def broadcast(self, message: dict):
        deliveries = [(ws, message) for ws in list(self.active)]
        projected = presence_projection(message)
        if projected is not None:
            deliveries.extend(
                (ws, projected) for ws in list(self.presence_subscribers)
            )
        if not deliveries:
            return
        tasks = {
            asyncio.create_task(ws.send_json(payload)): ws
            for ws, payload in deliveries
        }
        try:
            done, pending = await asyncio.wait(
                tasks, timeout=self.send_timeout_s)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
                task.add_done_callback(self._consume_send_result)
            raise

        failed = set()
        for task in done:
            try:
                task.result()
            except (Exception, asyncio.CancelledError):
                failed.add(tasks[task])
        for task in pending:
            failed.add(tasks[task])
            task.cancel()
            task.add_done_callback(self._consume_send_result)
        self.active.difference_update(failed)
        self.presence_subscribers.difference_update(failed)


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
    try:
        value = json.dumps(args, ensure_ascii=False, default=str)
    except Exception:
        value = str(args)
    value = " ".join(value.split())
    return value[:160] + ("…" if len(value) > 160 else "")
