"""Native snapshot projection, validation, and resume helpers."""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Iterable

from agent_task import Task, TaskStatus

from .errors import DurableCheckpointUnavailable
from .run_contract import validate_checkpoint_contract
from .session_capabilities import (
    mutation_write_elevation,
    session_capabilities,
    session_capabilities_from_run_state,
)
from .snapshot_store import SnapshotHeadFilter, StoredRunSnapshot
from .sqlite_snapshot_store import SQLiteRunSnapshotStore, default_snapshot_path
from .state import RunState

_TERMINAL = frozenset({"completed", "failed", "cancelled", "error", "truncated"})
RUNSTATE_RESUME_CONTEXT_HEADER = "## TASK RESUME (system context)"

_RESUME_RE = re.compile(
    r"^(?:task:resume|resume(?:\s+(?:the\s+)?(?:previous\s+)?task)?|"
    r"continue(?:\s+(?:the\s+)?(?:previous\s+)?task)?)\s*[.!?]*$",
    re.IGNORECASE,
)
_RESUME_POLITE = re.compile(r"^\s*(?:please|pls|plz|hey|ok|okay)\s+", re.IGNORECASE)


def log_snapshot_event(event: str, **fields: Any) -> None:
    """Emit one structured snapshot line into the unified main0/main1 stream.

    Electron captures backend stdout; never raises. Prefer compact fields
    (thread_id, status, goal snippet) — never dump full messages/blobs.
    """
    try:
        from observability.trace_events import record_trace_event

        record_trace_event(f"snapshot:{event}", **fields)
    except Exception:
        # Snapshot diagnostics are evidence only, never persistence control.
        pass
    try:
        from observability.operational_log import mirror_snapshot

        mirror_snapshot(event, fields)
    except Exception:
        pass


def summarize_run_state_for_log(run_state: RunState | dict | None) -> dict:
    """Compact identity fields for snapshot log lines (safe for main0)."""
    state = dict(run_state or {})
    task = dict(state.get("task") or {}) if isinstance(state.get("task"), dict) else {}
    main = dict(state.get("main") or {}) if isinstance(state.get("main"), dict) else {}
    loop = dict(main.get("loop") or {}) if isinstance(main.get("loop"), dict) else {}
    msgs = state.get("messages") if isinstance(state.get("messages"), list) else []
    goal = str(task.get("goal") or state.get("goal") or state.get("title") or "").strip()
    step = state.get("step")
    if step is None:
        step = loop.get("step")
    return {
        "thread_id": state.get("thread_id") or "",
        "run_id": state.get("run_id") or task.get("task_id") or "",
        "source": state.get("source") or "",
        "status": _normalize_status(task.get("status") or state.get("status") or ""),
        "goal": goal[:120],
        "step": step if step is not None else "",
        "msgs": len(msgs),
        "model": task.get("model_name") or "",
    }


def is_resume_request(text: str) -> bool:
    """True when the user explicitly asks to resume an interrupted task."""
    t = _RESUME_POLITE.sub("", (text or "").strip())
    return bool(_RESUME_RE.match(t))


def prior_incomplete_run_for_thread(
    thread_id: str,
    *,
    expected_revision: str = "",
) -> RunState | None:
    """Most recent native snapshot for a thread, only when still incomplete."""
    if not thread_id:
        return None
    try:
        store = SQLiteRunSnapshotStore()
        head = store.load_head_sync(thread_id)
        prior = head.state if head is not None else None
    except Exception as e:
        log_snapshot_event("lookup_fail", thread_id=thread_id, reason=e)
        if isinstance(e, DurableCheckpointUnavailable):
            raise
        raise DurableCheckpointUnavailable(
            f"durable snapshot lookup failed for {thread_id}: {e}"
        ) from e
    if not prior or not is_incomplete_run_state(prior):
        # Common path (fresh automation / clean thread) — stay quiet.
        return None
    prior, contract_error = validate_checkpoint_contract(
        prior,
        expected_revision=str(expected_revision or ""),
        accept_supported_revision=not bool(str(expected_revision or "").strip()),
    )
    if prior is None:
        log_snapshot_event(
            "lookup_reject",
            thread_id=thread_id,
            reason=contract_error,
        )
        return None
    summary = summarize_run_state_for_log(prior)
    summary["thread_id"] = thread_id or summary.get("thread_id") or ""
    log_snapshot_event("lookup_hit", **summary)
    return prior


def subagent_thread_id(*, parent_thread_id: str, task: str, instructions: str = "") -> str:
    """Stable durable-checkpoint thread for one delegated sub-goal.

    Scoped to the parent run's thread plus a hash of task+instructions so a
    different sub-goal on the same parent starts fresh while an interrupted
    retry of the same delegation can resume.
    """
    parent = (parent_thread_id or "orphan").strip()
    payload = f"{(task or '').strip()}\n{(instructions or '').strip()}"
    digest = hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"subagent:{parent}:{digest}"


def run_state_resume_summary(run_state: RunState) -> dict:
    """Compact orphan/resume payload derived from native RunState."""
    state = dict(run_state or {})
    task = dict(state.get("task") or {})
    payload = {
        'session_id':str(state.get('chat_id') or (state.get('work_scope') or {}).get('chat_id') or ''),
        "task_id": str(task.get("task_id") or state.get("run_id") or ""),
        "goal": str(task.get("goal") or state.get("goal") or state.get("title") or ""),
        "status": _normalize_status(task.get("status") or state.get("status") or TaskStatus.IN_PROGRESS.value),
        "updated_at": float(state.get("updated_at") or time.time()),
    }
    return payload


def validate_run_state_for_resume(
    run_state: RunState | None,
    *,
    max_age_s: float = 60 * 60 * 24,
    current_model: str = "",
    current_graph_revision: str = "",
    current_session_capabilities: Any = None,
) -> tuple[RunState | None, str]:
    """Validate a durable native main-chat RunState for manual resume."""
    if not run_state:
        return None, "no snapshot"
    state = dict(run_state)
    state, contract_error = validate_checkpoint_contract(
        state,
        expected_source="chat",
        expected_revision=current_graph_revision,
    )
    if state is None:
        return None, contract_error
    task = dict(state.get("task") or {})
    if not is_incomplete_run_state(state):
        return None, "checkpoint is not in progress"
    updated = float(state.get("updated_at") or state.get("created_at") or time.time())
    age = max(0.0, time.time() - updated)
    if age > max_age_s:
        return None, f"older than {max_age_s / 3600:.0f}h ({age / 3600:.1f}h)"
    saved_model = str(task.get("model_name") or "").strip()
    cur_model = str(current_model or "").strip()
    if saved_model and cur_model and saved_model != cur_model:
        return None, f"model changed ({saved_model!r} -> {cur_model!r})"
    saved_capabilities = session_capabilities_from_run_state(state)
    # Normalize in memory only. Existing checkpoint blobs remain untouched.
    state["session_capabilities"] = saved_capabilities
    if current_session_capabilities is not None:
        current_capabilities = session_capabilities(current_session_capabilities)
        try:
            pending_tools = pending_tool_loop_from_run_state(state)
        except ValueError as exc:
            return None, f"checkpoint tool boundary is inconsistent: {exc}"
        if pending_tools is not None and mutation_write_elevation(
            state, current_capabilities
        ):
            return None, (
                "mutation write authority increased after this pending tool call was "
                "checkpointed; turn mutation off or abandon the interrupted run before "
                "continuing"
            )
    return state, ""


def task_from_run_state(run_state: RunState) -> Task:
    """Rebuild a live Task directly from durable native RunState."""
    state = dict(run_state or {})
    task_state = dict(state.get("task") or {})
    task_id = str(task_state.get("task_id") or state.get("run_id") or "").strip()
    goal = str(task_state.get("goal") or state.get("goal") or state.get("title") or "").strip()
    if not task_id or not goal:
        raise ValueError("invalid RunState resume task")
    try:
        status = TaskStatus(_normalize_status(task_state.get("status") or state.get("status")))
    except ValueError:
        status = TaskStatus.IN_PROGRESS
    task = Task(
        goal=goal,
        status=status,
        context=dict(task_state.get("context") or {}),
        id=task_id,
        started_at=float(task_state.get("checkpoint_created_at") or state.get("created_at") or time.time()),
    )
    return task


def build_resume_context_from_run_state(
    run_state: RunState,
    task: Task | None = None,
) -> str:
    """System-style continuation context built directly from RunState."""
    state = dict(run_state or {})
    task_state = dict(state.get("task") or {})
    goal = str(task_state.get("goal") or state.get("goal") or state.get("title") or "")
    lines = [
        RUNSTATE_RESUME_CONTEXT_HEADER,
        "You are continuing an interrupted task - not starting a new one.",
        f"Original goal: {goal}",
    ]
    context = dict(task_state.get("context") or {})
    if context:
        items = list(context.items())[-4:]
        lines.append("Learned context: " + ", ".join(f"{k}={v!r}" for k, v in items))
    lines.append(
        "Continue from where you left off. Do not redo work already finished; "
        "use the restored tool results to finish the original goal."
    )
    return "\n".join(lines)


def restore_messages_from_run_state(
    run_state: RunState,
    static_system: str,
    *,
    resume_context: str = "",
) -> list:
    """Restore checkpointed messages while refreshing the system prefix."""
    system_content = f"{static_system}\n\n{resume_context}" if resume_context else static_system
    restored = list((run_state or {}).get("messages") or [])
    raw_n = len(restored)
    if not restored:
        log_snapshot_event("restore_messages", raw_msgs=0, out_msgs=1, note="empty_snapshot")
        return [{"role": "system", "content": system_content}]
    if restored[0].get("role") == "system":
        restored[0] = {"role": "system", "content": system_content}
    else:
        restored = [{"role": "system", "content": system_content}] + restored
    log_snapshot_event(
        "restore_messages",
        raw_msgs=raw_n,
        out_msgs=len(restored),
        has_resume_ctx=bool(resume_context),
        **{k: v for k, v in summarize_run_state_for_log(run_state).items()
           if k in ("thread_id", "run_id", "status", "goal")},
    )
    return restored


def interrupted_messages_for_follow_up(run_state: RunState, *, outcomes: list[dict] | None = None) -> list[dict]:
    """Carry a stopped run's complete internal graph into its next chat turn.

    The new turn owns a fresh task/run identity; this function carries only the
    provider conversation. A checkpoint can end after an assistant tool call,
    so append exact non-executed results before the new user message rather
    than replaying an action the user already stopped.
    """

    messages = [
        dict(row)
        for row in list((run_state or {}).get("messages") or [])
        if isinstance(row, dict)
    ]
    if messages and messages[0].get("role") == "system":
        messages = messages[1:]
    pending = pending_tool_loop_from_run_state(run_state)
    if pending is not None:
        import tool_calling

        actions = [
            dict(action)
            for action in list(pending.get("actions") or [])
            if isinstance(action, dict)
        ]
        if outcomes is None or len(outcomes) != len(actions) or any(
            outcome.get("call_id") != action.get("id")
            for action, outcome in zip(actions, outcomes)
        ):
            raise ValueError("stopped calls require exact reconciled outcomes")
        messages.extend(tool_calling.format_tool_result_messages(
            actions,
            outcomes=outcomes,
        ))
    return messages


def pending_tool_loop_from_run_state(
    run_state: RunState,
    *,
    worker: bool = False,
) -> dict | None:
    """Restore a checkpointed tool boundary without calling the model first.

    A sync checkpoint taken after a model step legitimately ends with an
    assistant ``tool_calls`` message and no tool results.  That transcript may
    only continue through the corresponding tool node.  Return the saved loop
    routing facts when they still match the provider-visible calls exactly;
    reject an inconsistent checkpoint rather than sending an unmatched call to
    a provider.
    """
    state = dict(run_state or {})
    messages = list(state.get("messages") or [])
    if not messages:
        return None
    assistant = messages[-1] if isinstance(messages[-1], dict) else {}
    calls = assistant.get("tool_calls") if assistant.get("role") == "assistant" else None
    if not isinstance(calls, list) or not calls:
        return None

    if worker:
        saved = dict(state.get("worker") or {})
    else:
        main = dict(state.get("main") or {})
        saved = dict(main.get("loop") or {})
    if saved.get("route") != "tools":
        raise ValueError(
            "checkpoint ends with unmatched tool calls but does not route to tools"
        )
    actions = list(saved.get("actions") or [])
    if len(actions) != len(calls):
        raise ValueError(
            "checkpoint tool actions do not match the unmatched provider calls"
        )
    for index, (call, action) in enumerate(zip(calls, actions)):
        if not isinstance(call, dict) or not isinstance(action, dict):
            raise ValueError("checkpoint tool boundary contains a malformed call")
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        call_id = str(call.get("id") or f"call_{index}")
        action_id = str(action.get("id") or f"call_{index}")
        call_name = str(function.get("name") or "")
        action_name = str(action.get("tool") or "")
        try:
            call_args = json.loads(str(function.get("arguments") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"checkpoint tool call {call_id!r} has invalid arguments"
            ) from exc
        action_args = action.get("args") if isinstance(action.get("args"), dict) else {}
        if (
            not call_name
            or call_id != action_id
            or call_name != action_name
            or not isinstance(call_args, dict)
            or call_args != action_args
        ):
            raise ValueError(
                f"checkpoint tool action {action_id!r} does not match its provider call"
            )

    return {
        **saved,
        "route": "tools",
        "actions": [dict(action) for action in actions],
        "turn_disposition": str(saved.get("turn_disposition") or "tools"),
        "stop_reason": str(saved.get("stop_reason") or "tool_use"),
    }


def infer_disclosed_tools_from_run_state(run_state: RunState, *, enabled_names: set | None = None) -> list[str]:
    """Return tool names already disclosed before interruption from RunState."""
    tools = dict((run_state or {}).get("tools") or {})
    names = list(tools.get("disclosed_names") or [])
    if enabled_names is not None:
        names = [n for n in names if n in enabled_names]
    return names


def is_terminal_commit_only_state(run_state: RunState | dict | None) -> bool:
    """True when a chat has an exact answer and only durability remains.

    A checkpoint can land after the terminal model step but before
    ``main_finalize`` projects ``awaiting_transcript``. That boundary is just
    as commit-only as an explicit awaiting-transcript head: replaying the model
    or draining active input would change an answer that already exists.
    """

    state = dict(run_state or {})
    if not state:
        return False
    output = dict(state.get("output") or {})
    if bool(output.get("transcript_committed")):
        return False
    if str(state.get("status") or "").lower() == "awaiting_transcript":
        return True
    if str(state.get("source") or "") != "chat":
        return False
    main = dict(state.get("main") or {})
    loop = dict(main.get("loop") or {})
    return bool(
        str(loop.get("route") or "") == "finalize"
        and (
            str(loop.get("reply") or "").strip()
            or str(loop.get("terminal_reason") or "").strip()
            or bool(loop.get("interrupted"))
        )
    )


def is_incomplete_run_state(run_state: RunState) -> bool:
    """True when RunState represents a resumable, non-terminal main chat task."""
    if not run_state:
        return False
    if is_terminal_commit_only_state(run_state):
        return True
    top_status = str(run_state.get("status") or "").lower()
    task = dict(run_state.get("task") or {})
    goal = str(task.get("goal") or run_state.get("goal") or "").strip()
    task_id = str(task.get("task_id") or run_state.get("run_id") or "").strip()
    if not goal or not task_id:
        return False
    status = str(task.get("status") or run_state.get("status") or "").lower()
    output = dict(run_state.get("output") or {})
    completion = str(output.get("completion_status") or "").lower()
    if status in _TERMINAL:
        return False
    if completion in {"ok", "failed", "cancelled"} and status == "completed":
        return False
    return True


def load_main_chat_resume_state(
    *,
    current_model: str = "",
    current_graph_revision: str = "",
    current_session_capabilities: Any = None,
    current_chat_id: str = "",
    thread_ids: Iterable[str] = (),
    last_run_receipt: dict | None = None,
) -> tuple[RunState | None, str]:
    """Load and validate the active chat's latest native durable snapshot."""
    try:
        db_path = default_snapshot_path()
        clean_chat_id = str(current_chat_id or "").strip()
        state = _latest_main_chat_snapshot(
            current_chat_id=clean_chat_id,
            thread_ids=thread_ids,
        )
        if state and _settled_stop(state, last_run_receipt):
            return None, "the prior run was explicitly stopped; start an ordinary follow-up"
        if state is not None and not is_incomplete_run_state(state):
            state = None
        if not state:
            log_snapshot_event(
                "resume_scan",
                result="no_snapshot",
                db=db_path,
                chat_id=clean_chat_id,
            )
            return None, "no snapshot"
        candidate = summarize_run_state_for_log(state)
        state, err = validate_run_state_for_resume(
            state,
            current_model=current_model,
            current_graph_revision=current_graph_revision,
            current_session_capabilities=current_session_capabilities,
        )
        if state:
            log_snapshot_event(
                "resume_load_ok",
                db=db_path,
                current_model=current_model or "",
                **candidate,
            )
            return state, ""
        log_snapshot_event(
            "resume_load_reject",
            reason=err or "invalid checkpoint",
            db=db_path,
            current_model=current_model or "",
            **candidate,
        )
        return None, err or "invalid snapshot"
    except Exception as e:
        log_snapshot_event("resume_load_fail", reason=e)
        return None, str(e)


def _latest_main_chat_snapshot(
    *,
    current_chat_id: str = "",
    thread_ids: Iterable[str] = (),
    snapshot_store=None,
    return_snapshot: bool = False,
) -> RunState | None:
    """Return the latest exact chat-owned snapshot before policy validation."""

    store = snapshot_store or SQLiteRunSnapshotStore()
    clean_chat_id = str(current_chat_id or "").strip()
    indexed_threads = tuple(dict.fromkeys(
        str(item or "").strip()
        for item in thread_ids
        if str(item or "").strip()
    ))
    candidate_heads = []
    if clean_chat_id:
        head = store.load_latest_for_chat_sync(clean_chat_id, source="chat")
        if head is not None:
            candidate_heads.append(head)
    if not candidate_heads and indexed_threads:
        for thread_id in indexed_threads:
            head = store.load_head_sync(thread_id)
            if head is not None:
                candidate_heads.append(head)
        candidate_heads.sort(
            key=lambda item: float(item.updated_at or 0.0),
            reverse=True,
        )
    if not clean_chat_id and not indexed_threads:
        candidate_heads = list(store.list_heads_sync(
            SnapshotHeadFilter(source="chat", limit=1)
        ))
    if not candidate_heads:
        return None
    return candidate_heads[0] if return_snapshot else candidate_heads[0].state


def _settled_stop(state: RunState, receipt: dict | None) -> bool:
    return bool(receipt and receipt.get("run_id") == state.get("run_id")
                and receipt.get("status") == "cancelled" and receipt.get("settled") is True)


def is_follow_up_evidence_state(state: RunState, receipt: dict | None = None) -> bool:
    return bool(state and state.get("messages") and (
        state.get("status") == "cancelled"
        or (state.get("output") or {}).get("snapshot_terminal_status") == "cancelled"
        or _settled_stop(state, receipt)
    ))


def load_main_chat_follow_up_state(
    *,
    current_chat_id: str = "",
    thread_ids: Iterable[str] = (),
    snapshot_store=None,
    last_run_receipt: dict | None = None,
) -> tuple[StoredRunSnapshot | None, str]:
    """Select an immutable stopped snapshot candidate for a fresh ordinary turn.

    Unlike true execution resume, evidence carry is intentionally independent
    of the selected model, graph revision, and current mutation authority. It
    never replays the saved task or a pending action. Acceptance must reconcile
    durable call evidence and publish through the canonical context projection.
    """

    clean_chat_id = str(current_chat_id or "").strip()
    try:
        candidate = _latest_main_chat_snapshot(
            current_chat_id=clean_chat_id,
            thread_ids=thread_ids,
            snapshot_store=snapshot_store,
            return_snapshot=True,
        )
        if candidate is None or not is_follow_up_evidence_state(candidate.state, last_run_receipt):
            return None, "no interrupted context"
        state = candidate.state
        state, contract_error = validate_checkpoint_contract(
            state,
            expected_source="chat",
            accept_supported_revision=True,
        )
        if state is None:
            return None, contract_error or "invalid interrupted context"
        # Validate an unmatched provider call now. The follow-up projection
        # will replace it with a non-executed result, never replay the action.
        pending_tool_loop_from_run_state(state)
        log_snapshot_event(
            "follow_up_context_load_ok",
            chat_id=clean_chat_id,
            **summarize_run_state_for_log(state),
        )
        return candidate, ""
    except Exception as exc:
        log_snapshot_event(
            "follow_up_context_load_fail",
            chat_id=clean_chat_id,
            reason=exc,
        )
        return None, str(exc)


def latest_run_state_for_thread(store: Any, thread_id: str) -> RunState | None:
    """Return the latest native RunState for one exact thread."""
    if not thread_id:
        return None
    try:
        item = store.load_head_sync(thread_id)
    except Exception as e:
        raise DurableCheckpointUnavailable(
            f"durable snapshot lookup failed for {thread_id}: {e}"
        ) from e
    if item is None:
        return None
    try:
        return item.state
    except Exception as e:
        raise DurableCheckpointUnavailable(
            f"durable snapshot restore failed for {thread_id}: {e}"
        ) from e


def mutation_elevation_blocked_by_threads(
    thread_ids: Any,
    snapshot_store: Any = None,
) -> tuple[bool, str]:
    """Fail-closed Off→On preflight for a chat's durable checkpoint threads.

    Completed history never blocks the toggle. Only a latest incomplete state
    parked at a provider/tool boundary with mutation writes disabled can be
    replayed with greater authority, so only that exact state is rejected.
    """

    source_threads = (
        (thread_ids,)
        if isinstance(thread_ids, str)
        else (thread_ids or ())
    )
    threads = tuple(dict.fromkeys(
        str(item or "").strip()
        for item in source_threads
        if str(item or "").strip()
    ))
    if not threads:
        return False, ""
    if snapshot_store is None:
        try:
            snapshot_store = SQLiteRunSnapshotStore()
        except Exception as exc:
            return True, f"mutation authority snapshot preflight failed: {exc}"
    for thread_id in threads:
        try:
            state = latest_run_state_for_thread(snapshot_store, thread_id)
        except Exception as exc:
            return True, (
                "mutation authority snapshot preflight failed for "
                f"{thread_id}: {exc}"
            )
        if state is None or not is_incomplete_run_state(state):
            continue
        try:
            pending = pending_tool_loop_from_run_state(state)
        except ValueError as exc:
            return True, (
                f"incomplete checkpoint {thread_id} has an unsafe tool boundary: {exc}"
            )
        if (
            pending is not None
            and not session_capabilities_from_run_state(state)[
                "mutation_write_enabled"
            ]
        ):
            return True, (
                f"incomplete checkpoint {thread_id} has a pending tool call saved "
                "with mutation writes off; resume or abandon it before enabling "
                "mutation"
            )
    return False, ""


def _normalize_status(value: Any) -> str:
    status = str(value or TaskStatus.IN_PROGRESS.value)
    if status == "running":
        return TaskStatus.IN_PROGRESS.value
    return status
