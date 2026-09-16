"""Desktop session service helpers.

``DesktopSessionState`` is the sole mutable desktop state container.
"""

from __future__ import annotations

import contextvars
import threading
import time
from typing import Any

from .session import (
    DesktopSessionState,
    current_desktop_session,
)

_IMAGE_SINK = contextvars.ContextVar("variant1_image_sink", default=None)
_IMAGE_PROVENANCE = contextvars.ContextVar(
    "variant1_image_provenance", default=None)

# Kernel capabilities return to the host over a loopback RPC task.  That task
# intentionally has its own ContextVar context, so it cannot inherit the outer
# provider-tool image sink/provenance binding.  The outer call id is already an
# authenticated part of every kernel invocation; retain a short-lived binding
# by that id so a nested screenshot can rejoin the exact model call that caused
# it.  Entries exist only while the outer tool is executing.
_ACTIVE_IMAGE_BINDINGS: dict[str, dict[str, Any]] = {}
_ACTIVE_IMAGE_BINDINGS_LOCK = threading.RLock()


def _current_image_holder():
    holder = _IMAGE_SINK.get()
    if holder is not None:
        return holder
    try:
        from run_context import current_run_context

        run_ctx = current_run_context()
        return getattr(run_ctx, "image_sink", None) if run_ctx else None
    except Exception:
        return None


def current_driver_state_id() -> str:
    try:
        return current_desktop_session().session_id
    except Exception:
        return ""


def install_image_sink(holder):
    return _IMAGE_SINK.set(holder)


def reset_image_sink(token) -> None:
    holder = _current_image_holder()
    if holder is not None:
        with _ACTIVE_IMAGE_BINDINGS_LOCK:
            stale = [
                call_id
                for call_id, binding in _ACTIVE_IMAGE_BINDINGS.items()
                if binding.get("holder") is holder
            ]
            for call_id in stale:
                _ACTIVE_IMAGE_BINDINGS.pop(call_id, None)
    try:
        _IMAGE_SINK.reset(token)
    except Exception:
        pass


def bind_image_provenance(call_id: str = "", tool_name: str = ""):
    """Bind the tool call whose nested capture is producing an observation."""
    marker = object()
    value = {
        "tool_call_id": str(call_id or "").strip(),
        "tool_name": str(tool_name or "").strip(),
        "_binding_marker": marker,
    }
    token = _IMAGE_PROVENANCE.set(value)
    holder = _current_image_holder()
    if holder is not None and value["tool_call_id"]:
        holder["_active_image_binding"] = marker
        holder["_active_image_call_id"] = value["tool_call_id"]
        with _ACTIVE_IMAGE_BINDINGS_LOCK:
            _ACTIVE_IMAGE_BINDINGS[value["tool_call_id"]] = {
                "holder": holder,
                "provenance": value,
                "marker": marker,
            }
    return token


def reset_image_provenance(token) -> None:
    provenance = _IMAGE_PROVENANCE.get()
    holder = _current_image_holder()
    if isinstance(provenance, dict) and holder is not None:
        marker = provenance.get("_binding_marker")
        if holder.get("_active_image_binding") is marker:
            holder.pop("_active_image_binding", None)
            holder.pop("_active_image_call_id", None)
        call_id = str(provenance.get("tool_call_id") or "").strip()
        if call_id:
            with _ACTIVE_IMAGE_BINDINGS_LOCK:
                active = _ACTIVE_IMAGE_BINDINGS.get(call_id)
                if active is not None and active.get("marker") is marker:
                    _ACTIVE_IMAGE_BINDINGS.pop(call_id, None)
    try:
        _IMAGE_PROVENANCE.reset(token)
    except Exception:
        pass


def _nested_image_binding() -> tuple[dict | None, dict | None, str]:
    """Resolve a kernel-nested capture back to its active outer tool call."""
    try:
        from capability_broker import current_capability_invocation

        invocation = current_capability_invocation()
    except Exception:
        invocation = None
    call_id = str(getattr(invocation, "outer_tool_call_id", "") or "").strip()
    if not call_id:
        return None, None, ""
    with _ACTIVE_IMAGE_BINDINGS_LOCK:
        binding = _ACTIVE_IMAGE_BINDINGS.get(call_id)
        if not isinstance(binding, dict):
            return None, None, ""
        holder = binding.get("holder")
        provenance = binding.get("provenance")
        marker = binding.get("marker")
    if not isinstance(holder, dict) or not isinstance(provenance, dict):
        return None, None, ""
    if holder.get("_active_image_binding") is not marker:
        return None, None, ""
    nested_call_id = str(getattr(invocation, "nested_call_id", "") or "")
    return holder, provenance, nested_call_id


def deliver_image(
    b64: str,
    *,
    media_type: str = "",
    artifact_ref: str = "",
    producer: str = "",
) -> None:
    """Attach one typed capture to the next model request.

    Direct provider tools use the local ContextVar binding.  Capabilities
    invoked from the persistent IPython kernel use the authenticated outer call
    id carried by ``InvocationContext`` to recover that same binding.
    """
    if not str(b64 or "").strip():
        return
    holder = _current_image_holder()
    provenance = _IMAGE_PROVENANCE.get()
    call_id = (
        str((provenance or {}).get("tool_call_id") or "").strip()
        if isinstance(provenance, dict)
        else ""
    )
    nested_call_id = ""
    # A task spawned during a tool call inherits that call's ContextVar. It may
    # outlive the call and emit a later desktop frame with a stale causal id.
    # Accept images only while the exact originating binding is still active.
    direct_binding_active = bool(
        isinstance(holder, dict)
        and isinstance(provenance, dict)
        and call_id
        and holder.get("_active_image_binding")
        is provenance.get("_binding_marker")
    )
    if not direct_binding_active:
        holder, provenance, nested_call_id = _nested_image_binding()
        call_id = str((provenance or {}).get("tool_call_id") or "").strip()
    if not isinstance(holder, dict) or not isinstance(provenance, dict) or not call_id:
        return
    if holder.get("image"):
        try:
            from observability import context_lineage

            context_lineage.add_current_run_transform(
                kind="image_superseded",
                reason="newer_observation",
                input_count=2,
                output_count=1,
                affected_count=1,
                image_count=1,
            )
        except Exception:
            pass
    holder["image"] = {
        "data_b64": b64,
        "media_type": str(media_type or ""),
        "origin": "tool_result",
        "tool_call_id": call_id,
        "tool_call_ids": [call_id],
        "tool_name": str(producer or provenance.get("tool_name") or ""),
        "artifact_ref": str(artifact_ref or ""),
        "capture": {
            "status": "captured",
            "captured_at": round(time.time(), 3),
            "encoded_bytes": len(str(b64 or "")),
            "nested_call_id": nested_call_id,
        },
    }


def queue_desktop_error(session: DesktopSessionState, err: Any) -> None:
    session.pending_desktop_errors.append(err)
    session.mark_updated()


def take_focus_loss_warning(session: DesktopSessionState) -> str:
    warning = session.focus_loss_warning or ""
    session.focus_loss_warning = ""
    session.mark_updated()
    return warning
