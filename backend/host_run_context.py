"""Run-context factory helpers extracted from the server composition root.

Prefer ``AppHost`` (``APP``).
"""

from __future__ import annotations

from project_context import chat_project_context
from run_context import Variant1RunContext, current_run_context
from work_fabric.scope import coerce_work_scope


def make_run_context(
    h,
    source: str,
    title: str,
    *,
    session=None,
    chat_transport=None,
    desktop_snapshot: dict | None = None,
    metadata: dict | None = None,
    inherit_parent: bool = True,
    isolate_desktop: bool = False,
) -> Variant1RunContext:
    parent = current_run_context() if inherit_parent else None
    runtime_metadata = dict(metadata or {})
    parent_metadata = dict(getattr(parent, "metadata", {}) or {}) if parent else {}
    for key in (
        "working_directory", "project_root", "project_roots", "project_environment",
    ):
        if key not in runtime_metadata and parent_metadata.get(key):
            runtime_metadata[key] = parent_metadata[key]
    if session is not None:
        chat_id = str(
            runtime_metadata.get("chat_id")
            or getattr(getattr(session, "active", None), "runtime_chat_id", "")
            or getattr(getattr(session, "active", None), "turn_session_id", "")
            or getattr(session, "viewed_session_id", "")
            or ""
        ).strip()
        if not chat_id:
            try:
                chat_id = str(
                    h.require_runtime().sessions.get_active()
                    or ""
                ).strip()
            except Exception:
                chat_id = ""
        if chat_id:
            runtime_metadata["chat_id"] = chat_id
            if not runtime_metadata.get("runtime_identity"):
                try:
                    runtime_metadata["runtime_identity"] = (
                        h.require_runtime().session_runtimes.ensure_runtime(
                            chat_id
                        ).to_dict()
                    )
                except Exception:
                    pass
    if not runtime_metadata.get("working_directory"):
        project = chat_project_context(
            h, str(runtime_metadata.get("chat_id") or ""),
        )
        runtime_metadata["working_directory"] = project.cwd
        runtime_metadata["project_root"] = project.cwd
        runtime_metadata["project_roots"] = list(project.roots)
    raw_scope = runtime_metadata.get("work_scope")
    if not raw_scope and parent is not None:
        raw_scope = getattr(parent, "work_scope", None)
    scope = coerce_work_scope(raw_scope)
    scope_updates = {}
    if runtime_metadata.get("chat_id") and not scope.chat_id:
        scope_updates["chat_id"] = str(runtime_metadata["chat_id"])
    if scope_updates:
        scope = scope.with_updates(**scope_updates)
    runtime_metadata["work_scope"] = scope.to_dict(include_empty=False)
    desktop_binding = None
    try:
        from desktop_fabric.binding import DesktopBinding, ensure_desktop_binding

        if isolate_desktop:
            desktop_binding = DesktopBinding(
                owner_kind=("chat" if source == "chat" else "run"),
                scope=scope,
            )
        else:
            desktop_binding = ensure_desktop_binding(
                desktop_snapshot,
                source=source,
                scope=scope,
            )
    except Exception:
        desktop_binding = None
    context = Variant1RunContext.create(
        source=source,
        work_scope=scope,
        title=title,
        session_id=str(runtime_metadata.get("chat_id") or ""),
        parent_run_id=(parent.run_id if parent else ""),
        chat_session=session,
        chat_transport=chat_transport,
        desktop_binding=desktop_binding,
        activity_sink=h.emit_activity,
        cancellation=(lambda: bool(getattr(session, "interrupt", False))) if session is not None else None,
        metadata=runtime_metadata,
    )
    if desktop_binding is not None and desktop_binding.owner_kind == "run" and not desktop_binding.owner_id:
        desktop_binding.owner_id = context.run_id
    return context


def require_bound_run_context(source: str, *, operation: str) -> Variant1RunContext | None:
    """Fail closed for isolated background paths that must not inherit user context."""
    ctx = current_run_context()
    if ctx is None:
        print(f"[run_context] {source}:{operation} blocked: no Variant1RunContext bound", flush=True)
        return None
    if ctx.source != source:
        print(
            f"[run_context] {source}:{operation} blocked: bound context source is {ctx.source!r}",
            flush=True,
        )
        return None
    if source == "automation" and not ctx.metadata.get("_server_bound_kind"):
        print(f"[run_context] {source}:{operation} blocked: missing server-bound marker", flush=True)
        return None
    return ctx
