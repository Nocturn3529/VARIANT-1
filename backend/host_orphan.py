"""Durable interrupted-task recovery and chat-session projections."""

from __future__ import annotations

def orphaned_task_payload(h, chat_id: str = '') -> dict | None:
    """Orphaned main-chat payload from native durable snapshots only."""
    owner=str(chat_id or h.require_runtime().sessions.get_active() or '')
    state, _err = snapshot_resume_state(h,owner)
    if not state:
        return None
    try:
        from agent_engine.snapshot_utils import run_state_resume_summary

        result=run_state_resume_summary(state)
        if result.get('session_id') and result['session_id']!=owner:
            return None
        # Scoped native lookup may prove a legacy snapshot through its thread
        # index even when that snapshot predates the chat_id field.
        result['session_id']=owner
        return result if owner else None
    except Exception as e:
        print(f"[native_snapshot] orphan payload failed: {e}", flush=True)
        return None


def snapshot_resume_state(h, chat_id: str = ""):
    """Main-chat resume source; lookup lives in agent_engine.snapshot_utils."""
    from agent_engine.snapshot_utils import load_main_chat_resume_state

    graph_revision = ""
    runtime_record = None
    runtime = h.require_runtime()
    sessions = runtime.sessions
    scoped_chat_id = str(chat_id or "").strip()
    thread_ids: tuple[str, ...] = ()
    try:
        if not scoped_chat_id:
            scoped_chat_id = str(sessions.get_active() or "")
        record = (
            runtime.session_runtimes.runtime(scoped_chat_id)
            if scoped_chat_id
            else None
        )
        if record is not None:
            runtime_record = record
            graph_revision = str(record.identity.graph_revision or "")
    except Exception:
        graph_revision = ""
    if scoped_chat_id:
        try:
            thread_ids = tuple(
                runtime.session_runtimes.repository.thread_refs(scoped_chat_id)
            )
        except Exception:
            # Native snapshots normally carry chat_id. The thread index keeps
            # lookup scoped if a run was admitted without chat metadata.
            thread_ids = ()
    state, error = load_main_chat_resume_state(
        current_model=h.active_model_name(),
        last_run_receipt=sessions.get_last_run_receipt(scoped_chat_id),
        current_graph_revision=graph_revision,
        current_session_capabilities=runtime_record,
        current_chat_id=scoped_chat_id,
        thread_ids=thread_ids,
    )
    if state and scoped_chat_id:
        try:
            covered = sessions.get_stopped_context_coverage(
                scoped_chat_id, str(state.get("run_id") or "")
            )
            if covered:
                from session_projection import repair_run_state_user_context

                state = repair_run_state_user_context(
                    sessions,
                    scoped_chat_id,
                    state,
                    covered=covered,
                )
        except Exception:
            # Resume authority belongs to the durable snapshot. A failed
            # disposable projection repair must not fabricate a safe revision
            # or discard an otherwise valid interrupted task.
            pass
    return state, error


def snapshot_follow_up_state(h, chat_id: str = ""):
    """Interrupted provider conversation for a new ordinary chat turn."""
    from agent_engine.snapshot_utils import load_main_chat_follow_up_state

    runtime = h.require_runtime()
    sessions = runtime.sessions
    scoped_chat_id = str(chat_id or "").strip()
    if not scoped_chat_id:
        scoped_chat_id = str(sessions.get_active() or "")
    thread_ids: tuple[str, ...] = ()
    if scoped_chat_id:
        try:
            thread_ids = tuple(
                runtime.session_runtimes.repository.thread_refs(scoped_chat_id)
            )
        except Exception:
            thread_ids = ()
    return load_main_chat_follow_up_state(
        current_chat_id=scoped_chat_id,
        thread_ids=thread_ids,
        snapshot_store=runtime.session_runtimes.snapshot_store,
        last_run_receipt=sessions.get_last_run_receipt(scoped_chat_id),
    )


async def check_orphaned_task(h) -> None:
    orphan = orphaned_task_payload(h)
    if orphan:
        fields = {
            "task_id": str(orphan.get("task_id") or ""),
            "status": str(orphan.get("status") or ""),
            "goal": str(orphan.get("goal") or "")[:120],
            "updated_at": str(orphan.get("updated_at") or ""),
        }
        try:
            from agent_engine.snapshot_utils import log_snapshot_event
            log_snapshot_event("orphan", **fields)
        except Exception:
            print(f"[checkpoint] orphaned in-progress task id={fields['task_id']} "
                  f"goal={fields['goal']!r} status={fields['status']} "
                  f"(updated {fields['updated_at']})", flush=True)
    else:
        try:
            from agent_engine.snapshot_utils import log_snapshot_event
            log_snapshot_event("orphan_scan", result="none")
        except Exception:
            pass


def chat_sessions_msg(h) -> dict:
    """The session-list payload used by Main Deck connections."""
    sessions = h.require_runtime().sessions
    return {"type": "chat:sessions",  # active_id = default for NEW connections
            "items": sessions.list_sessions(),
            "active_id": sessions.get_active()}


def viewed_chat_sid(h, session) -> str:
    """The durable chat session THIS connection reads/writes.

    Per-client sessions: each WebSocket binds its own viewed session the first
    time it needs one (first turn, or an explicit switch/new); a session switch
    in another window never changes this connection's write target. The shared
    ``active_id`` is only the default a connection adopts when it has no valid
    binding (fresh connection, or its session was deleted)."""
    sessions = h.require_runtime().sessions
    sid = getattr(session, "viewed_session_id", None)
    if sid and sessions.has_session(sid):
        return sid
    sid = sessions.get_active()
    session.viewed_session_id = sid
    return sid
