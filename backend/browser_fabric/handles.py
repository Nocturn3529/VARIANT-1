"""Descriptor-routed IPython envelopes for reconstructable browser handles."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from work_fabric.handles import remote_handle_envelope

from .models import ElementRef, PageRef, SessionRecord, TargetRecord, TraceRecord
from .keyboard import BROWSER_KEYS_GUIDANCE
from .settings import browser_surface


def _method(
    name: str,
    description: str,
    params: list[dict[str, Any]] | None = None,
    *,
    variadic_kwargs: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "params": list(params or ()),
        "returns": "object",
        "variadic_kwargs": bool(variadic_kwargs),
    }


_INCLUDE_SCREENSHOT = {
    "name": "include_screenshot",
    "type": "bool",
    "required": False,
    "default": False,
}
_SESSION_METHODS = [
    _method(
        "pages",
        "Return this session's current page and bounded page-handle list.",
    ),
    _method("new_page", "Open a tab and return a BrowserPage handle. Call page.observe()/navigate() directly; page.page is the same handle and page.session is its parent session.", [{
        "name": "url", "type": "str", "required": False, "default": "",
    }], variadic_kwargs=True),
    _method("history", "Read session events, operations, downloads, or traces. Downloads refresh native progress; filter by the initiating action's operation_id before retrying a download.", [
        {"name": "kind", "type": "str", "required": True},
        {"name": "after_sequence", "type": "int", "required": False, "default": 0},
        {"name": "limit", "type": "int", "required": False, "default": 100},
        {"name": "operation_id", "type": "str", "required": False, "default": ""},
    ]),
    _method("start_trace", "Start a browser trace.", [
        {"name": "name", "type": "str", "required": False, "default": "Browser trace"},
        {"name": "options", "type": "dict", "required": False, "default": None},
    ]),
    _method("close", "Close the browser session."),
]
_PAGE_METHODS = [
    _method('set_viewport', 'Set this page viewport in CSS pixels, or mode=auto to restore automatic sizing.', [
        {'name': 'width', 'type': 'int', 'required': False, 'default': None},
        {'name': 'height', 'type': 'int', 'required': False, 'default': None},
        {'name': 'mode', 'type': 'str', 'required': False, 'default': 'fixed'},
    ]),
    _method("observe", "Return BrowserObservation with .page, .session, .elements and .snapshot. A verified attachment-only target instead returns its completed download and document_state; no document is implied.", [
        {"name": "max_chars", "type": "int", "required": False, "default": 200000},
        {"name": "max_elements", "type": "int", "required": False, "default": 1000},
        {"name": "include_html", "type": "bool", "required": False, "default": False},
        {"name": "include_screenshot", "type": "bool", "required": False, "default": False},
    ]),
    _method("navigate", "Navigate to a URL, move back/forward, reload, or select this page.", [
        {"name": "url", "type": "str", "required": False, "default": ""},
        {"name": "action", "type": "str", "required": False, "default": ""},
        _INCLUDE_SCREENSHOT,
    ], variadic_kwargs=True),
    _method("keys", "Send keys to this page. " + BROWSER_KEYS_GUIDANCE, [{
        "name": "keys", "type": "str", "required": True,
    }, _INCLUDE_SCREENSHOT], variadic_kwargs=True),
    _method("wait", "Wait for a page condition.", [
        _INCLUDE_SCREENSHOT,
    ], variadic_kwargs=True),
    _method("evaluate", "Evaluate JavaScript in this page. For portable expressions, invoke functions, e.g. (() => document.title)(); return JSON data, not functions or DOM nodes. The action result exposes value, page, operation_id and download state.", [
        {"name": "expression", "type": "str", "required": True},
        {"name": "arg", "type": "any", "required": False, "default": None},
        _INCLUDE_SCREENSHOT,
    ], variadic_kwargs=True),
    _method("close", "Close this page."),
]
_ELEMENT_METHODS = [
    _method("click", "Click this element.", [
        _INCLUDE_SCREENSHOT,
    ], variadic_kwargs=True),
    _method("fill", "Fill this element.", [{
        "name": "text", "type": "str", "required": True,
    }, _INCLUDE_SCREENSHOT], variadic_kwargs=True),
    _method("select", "Select option values.", [{
        "name": "values", "type": "any", "required": True,
    }, _INCLUDE_SCREENSHOT], variadic_kwargs=True),
    _method("hover", "Hover this element.", [
        _INCLUDE_SCREENSHOT,
    ], variadic_kwargs=True),
    _method("keys", "Send keys to this element. " + BROWSER_KEYS_GUIDANCE, [{
        "name": "keys", "type": "str", "required": True,
    }, _INCLUDE_SCREENSHOT], variadic_kwargs=True),
]
_TRACE_METHODS = [
    _method("stop", "Stop this trace and retain its artifact."),
]


def _method_subset(
    methods: Iterable[dict[str, Any]], names: Iterable[str],
) -> list[dict[str, Any]]:
    allowed = {str(name) for name in names}
    return [dict(method) for method in methods if str(method.get("name") or "") in allowed]


def _session_methods(
    capabilities: Iterable[str], *, state: str,
) -> list[dict[str, Any]]:
    if str(state or "") == "closed":
        return []
    supported = {str(item) for item in capabilities}
    names = {"pages", "history", "close"}
    if "tabs" in supported:
        names.add("new_page")
    if "trace" in supported:
        names.add("start_trace")
    return _method_subset(_SESSION_METHODS, names)


def _page_methods(capabilities: Iterable[str]) -> list[dict[str, Any]]:
    supported = {str(item) for item in capabilities}
    names: set[str] = set()
    if supported.intersection({"navigate", "back", "forward", "reload", "tabs"}):
        names.add("navigate")
    names.update(
        name for name in (
            "observe", "keys", "wait", "evaluate", "set_viewport",
        )
        if name in supported
    )
    if "tabs" in supported:
        names.add("close")
    return _method_subset(_PAGE_METHODS, names)


def _element_methods(
    capabilities: Iterable[str], actions: Iterable[str],
) -> list[dict[str, Any]]:
    supported = {str(item) for item in capabilities}
    advertised = {str(item) for item in actions}
    names: set[str] = set()
    names.update(
        name for name in ("click", "fill", "select", "hover", "keys")
        if name in supported and name in advertised
    )
    return _method_subset(_ELEMENT_METHODS, names)


def session_handle_envelope(
    session: SessionRecord, *, broker: Any, context: Any,
) -> dict[str, Any]:
    return remote_handle_envelope(
        service="browser", kind="session", handle_id=session.session_id,
        generation=session.generation, revision=session.revision,
        metadata={
            "profile_id": session.profile_id,
            **browser_surface(session),
            "state": session.state,
            "current_target_id": session.current_target_id,
            "capabilities": list(session.capabilities),
        },
        methods=_session_methods(session.capabilities, state=session.state),
        broker=broker, context=context,
    )


def page_handle_envelope(
    page: PageRef,
    *,
    target: TargetRecord,
    capabilities: Iterable[str] | None = None,
    broker: Any,
    context: Any,
) -> dict[str, Any]:
    if page.session_id != target.session_id or page.target_id != target.target_id:
        raise ValueError("PageRef and TargetRecord identify different browser pages")
    return remote_handle_envelope(
        service="browser", kind="page", handle_id=page.target_id,
        generation=page.generation, revision=page.target_revision,
        metadata={
            "session_id": page.session_id,
            "title": target.title[:500],
            "url": target.url[:4000],
            "state": target.state,
            "document_epoch": page.document_epoch,
            "observation_revision": page.observation_revision,
            "viewport": dict(getattr(target, 'viewport', {}) or {}),
        },
        methods=(
            _page_methods(capabilities)
            if capabilities is not None
            else _PAGE_METHODS
        ),
        broker=broker, context=context,
    )


def element_handle_envelope(
    element: ElementRef,
    *,
    capabilities: Iterable[str] | None = None,
    actions: Iterable[str] | None = None,
    broker: Any,
    context: Any,
) -> dict[str, Any]:
    return remote_handle_envelope(
        service="browser", kind="element",
        handle_id=f"{element.target_id}:{element.backend_ref}",
        generation=element.generation, revision=element.observation_revision,
        metadata={
            "session_id": element.session_id,
            "target_id": element.target_id,
            "backend_ref": element.backend_ref,
            "document_epoch": element.document_epoch,
            "role": element.role[:80],
            "name": element.name[:500],
            "actions": list(actions or ()),
        },
        methods=(
            _element_methods(capabilities, actions or ())
            if capabilities is not None and actions is not None
            else _ELEMENT_METHODS
        ),
        broker=broker, context=context,
    )


def trace_handle_envelope(
    trace: TraceRecord,
    *,
    session_generation: int,
    broker: Any,
    context: Any,
) -> dict[str, Any]:
    return remote_handle_envelope(
        service="browser", kind="trace", handle_id=trace.trace_id,
        generation=int(session_generation), revision=trace.revision,
        metadata={
            "session_id": trace.session_id,
            "name": trace.name,
            "state": trace.state,
            "artifact_ref": trace.artifact_ref,
            "bytes": trace.bytes,
        },
        methods=(_TRACE_METHODS if trace.state == "recording" else ()),
        broker=broker, context=context,
    )


__all__ = [
    "element_handle_envelope", "page_handle_envelope", "session_handle_envelope",
    "trace_handle_envelope",
]
