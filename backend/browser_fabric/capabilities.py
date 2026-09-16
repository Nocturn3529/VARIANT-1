"""Scoped hidden broker capabilities and remote handles for Browser Fabric."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any

from capability_broker import InvocationContext, current_capability_invocation
from tools import ToolError
from work_fabric.scope import effective_work_scope as _scope

from .handles import (
    element_handle_envelope,
    page_handle_envelope,
    session_handle_envelope,
    trace_handle_envelope,
)
from .models import (
    BrowserExpectedState,
    ElementRef,
    PageRef,
    SessionRecord,
    TargetRecord,
)
from .settings import browser_surface


_ELEMENT_ACTIONS = frozenset({"click", "fill", "select", "hover", "keys"})


def _context() -> InvocationContext:
    context = current_capability_invocation()
    if context is None:
        raise ToolError("browser capabilities require an admitted Python cell")
    return context


def _runtime(host: Any) -> Any:
    graph = getattr(host, "require_runtime", lambda: None)()
    runtime = getattr(graph, "browser", None)
    if runtime is None:
        raise ToolError("Browser Fabric is unavailable")
    return runtime


def _request_key(context: InvocationContext, args: Mapping[str, Any]) -> str:
    # Durable replay is opt-in. A single admitted IPython cell can issue many
    # distinct browser commands under one outer tool-call identity, so using
    # that outer ID as every command's key would create false conflicts.
    return str(
        args.get("idempotency_key")
        or context.idempotency_key
    ).strip()


def _session(runtime: Any, context: InvocationContext, session_id: Any) -> SessionRecord:
    try:
        return runtime.session(
            str(session_id or ""), scope=_scope(context)
        )
    except Exception as exc:
        raise ToolError(str(exc)) from exc


def _target(
    runtime: Any,
    context: InvocationContext,
    session_id: Any,
    target_id: Any = "",
    *,
    include_closed: bool = False,
) -> tuple[SessionRecord, TargetRecord]:
    session = _session(runtime, context, session_id)
    selected = str(target_id or session.current_target_id or "")
    if not selected:
        raise ToolError("browser session has no selected page")
    target = runtime.store.get_target(selected)
    if target.session_id != session.session_id:
        raise ToolError("browser page belongs to another session")
    if target.state == "closed" and not include_closed:
        raise ToolError("browser page is closed")
    return session, target


def _expected(value: Any) -> BrowserExpectedState | None:
    if value in (None, {}):
        return None
    if not isinstance(value, Mapping):
        raise ToolError("expected must be an object")
    allowed = {
        "session_generation", "session_revision", "target_revision",
        "document_epoch", "observation_revision",
    }
    unknown = set(value).difference(allowed)
    if unknown:
        raise ToolError(f"unknown browser expected field(s): {', '.join(sorted(unknown))}")
    values: dict[str, int | None] = {}
    for key in allowed:
        raw = value.get(key)
        values[key] = int(raw) if raw is not None else None
    return BrowserExpectedState(**values)


def _page_ref(session: SessionRecord, target: TargetRecord) -> PageRef:
    return target.page_ref(session.generation)


def _element_ref(
    runtime: Any, context: InvocationContext, value: Any,
) -> tuple[SessionRecord, TargetRecord, ElementRef]:
    if not isinstance(value, Mapping):
        raise ToolError("element must be an ElementRef or browser element handle identity")
    if str(value.get("kind") or "") == "element":
        target_id, separator, backend_ref = str(value.get("id") or "").partition(":")
        if not separator:
            raise ToolError("browser element handle ID is malformed")
        target = runtime.store.get_target(target_id)
        session = _session(runtime, context, target.session_id)
        generation = int(value.get("generation") or -1)
        revision = int(value.get("revision") or -1)
    else:
        target_id = str(value.get("target_id") or "")
        backend_ref = str(value.get("backend_ref") or "")
        target = runtime.store.get_target(target_id)
        session = _session(runtime, context, str(value.get("session_id") or target.session_id))
        generation = int(value.get("generation") or -1)
        revision = int(value.get("observation_revision") or -1)
    if target.session_id != session.session_id:
        raise ToolError("browser element belongs to another session")
    observation = runtime.store.observation_at_revision(
        target.target_id, revision
    )
    if observation is None:
        raise ToolError("browser element has no durable observation")
    if (
        generation != session.generation
        or observation.generation != session.generation
        or observation.document_epoch != target.document_epoch
    ):
        raise ToolError("stale browser element handle from another page document")
    try:
        return session, target, observation.element(backend_ref)
    except Exception as exc:
        raise ToolError(str(exc)) from exc


def _session_handle(host: Any, context: InvocationContext, record: SessionRecord) -> dict[str, Any]:
    return session_handle_envelope(
        record, broker=host.require_runtime().broker, context=context,
    )


def _page_handle(
    host: Any,
    context: InvocationContext,
    session: SessionRecord,
    target: TargetRecord,
) -> dict[str, Any]:
    envelope = page_handle_envelope(
        _page_ref(session, target), target=target,
        capabilities=session.capabilities,
        broker=host.require_runtime().broker, context=context,
    )
    envelope["$variant1_handle"]["metadata"].update(_surface(session))
    envelope["$variant1_handle"]["metadata"]["session"] = _session_handle(host, context, session)
    return envelope


def _surface(session: SessionRecord) -> dict[str, str]:
    return browser_surface(session)


def _bound_blob(host: Any, context: InvocationContext, value: Mapping[str, Any]) -> Any:
    from artifacts.blob_handles import blob_handle_envelope

    ref = str(value.get("ref") or value.get("artifact_ref") or "")
    if not ref:
        return dict(value)
    try:
        return blob_handle_envelope(host, context, ref, metadata=dict(value))
    except Exception as exc:
        # Export convenience cannot turn a completed browser effect into a failure.
        return {**value, "export_error": (str(exc) or type(exc).__name__)[:300]}


def _download_result(host: Any, context: InvocationContext, value: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(value)
    if row.get("artifact_ref"):
        row["artifact"] = _bound_blob(host, context, row)
    return row


def _deliver_image_artifact(
    runtime: Any,
    artifact: Mapping[str, Any] | None,
    *,
    producer: str,
) -> dict[str, Any] | None:
    """Promote one Browser Fabric image artifact into the next model request."""
    row = dict(artifact or {})
    ref = str(row.get("ref") or "")
    if not ref:
        return None
    try:
        from desktop.service import deliver_image

        payload = runtime.artifact_store.read_bytes(ref)
        deliver_image(
            base64.b64encode(payload).decode("ascii"),
            media_type=str(row.get("media_type") or "image/png"),
            artifact_ref=ref,
            producer=producer,
        )
    except Exception:
        # The durable artifact remains in the browser result. Image projection
        # must never turn a successful browser operation into a failed one.
        return None
    return row


async def _attach_post_action_screenshot(
    runtime: Any,
    context: InvocationContext,
    session: SessionRecord,
    target: TargetRecord,
    result: Mapping[str, Any],
    *,
    producer: str,
) -> dict[str, Any]:
    """Capture and promote one opt-in screenshot after a browser effect.

    The effect has already succeeded at this point. Resolve the latest durable
    page state before capturing (navigation and clicks may advance its
    revision), and never replace that success with a secondary screenshot
    failure.
    """
    enriched = dict(result)
    try:
        session = runtime.session(session.session_id)
        target = runtime.store.get_target(target.target_id)
        screenshot = await runtime.screenshot(
            _page_ref(session, target),
            scope=_scope(context),
        )
    except Exception as exc:
        enriched["screenshot"] = {
            "status": "unavailable",
            "error": (str(exc) or type(exc).__name__)[:300],
        }
        return enriched
    artifact = (
        screenshot.get("artifact")
        if isinstance(screenshot, Mapping)
        else None
    )
    promoted = _deliver_image_artifact(
        runtime,
        artifact if isinstance(artifact, Mapping) else None,
        producer=producer,
    )
    if promoted is not None:
        enriched["screenshot"] = promoted
    return enriched


def _observation_result(
    host: Any,
    context: InvocationContext,
    runtime: Any,
    observation: Any,
) -> dict[str, Any]:
    session, target = _target(
        runtime, context, observation.session_id, observation.target_id
    )
    element_refs = [item.ref_for(observation) for item in observation.elements]
    snapshot = observation.to_dict()
    # Retain an explicit full-data reference alongside the compact display.
    # Handles remain fully available in Python; the wire codec shares their
    # repeated descriptors instead of clipping the collection.
    data_ref = None
    data_ref_error = ''
    try:
        data_ref = runtime.artifact_store.put_json(
            snapshot, kind='browser_observation', scope=_scope(context).chat_id or observation.session_id,
        )
    except Exception as exc:
        # The complete observation still crosses the bridge as a Python value.
        # Artifact publication must not hide a successful operation.
        data_ref_error = (str(exc) or type(exc).__name__)[:300]
    html = ""
    html_truncated = False
    html_ref = str(observation.html_artifact_ref or "")
    if html_ref:
        try:
            raw_html = runtime.artifact_store.read_bytes(html_ref)
            html_truncated = len(raw_html) > 64 * 1024
            html = raw_html[: 64 * 1024].decode("utf-8", errors="replace")
        except Exception:
            # The durable artifact reference remains available in ``snapshot``.
            # A projection convenience must never make observation itself fail.
            html = ""
    screenshot_ref = str(observation.screenshot_artifact_ref or "")
    if screenshot_ref:
        _deliver_image_artifact(
            runtime,
            {"ref": screenshot_ref, "media_type": "image/png"},
            producer="browser_observation",
        )
    document = dict(observation.document)
    downloads = [_download_result(host, context, row) for row in document.get("downloads", ())]
    return {
        "schema": "variant1.browser-observation-result.v1",
        **_surface(session),
        # A read has an observation identity, but does not invent an action.
        # Navigation attaches its real operation ID after this projection.
        "operation_id": document.get("operation_id"),
        **({"document_state": document["state"], "message": document.get("message", ""),
            "observation_status": document.get("observation_status"),
            "observation_error": document.get("observation_error"),
            "download_state": document.get("download_state"), "downloads": downloads,
            "download": downloads[0] if downloads else None} if document else {}),
        "session": _session_handle(host, context, session),
        "snapshot": snapshot,
        'viewport': dict(observation.viewport),
        "data_ref": str(data_ref.ref) if data_ref is not None else None,
        **({'data_ref_error': data_ref_error} if data_ref_error else {}),
        "text": observation.text_excerpt,
        "html": html,
        "html_ref": html_ref or None,
        "html_truncated": html_truncated,
        "image": _bound_blob(host, context, {"ref": screenshot_ref}) if screenshot_ref else None,
        "page": _page_handle(host, context, session, target),
        "elements": [
            element_handle_envelope(
                ref,
                capabilities=session.capabilities,
                actions=item.actions,
                broker=host.require_runtime().broker,
                context=context,
            )
            for item, ref in zip(observation.elements, element_refs, strict=True)
        ],
        "element_refs": [item.to_dict() for item in element_refs],
    }


def _action_result(
    host: Any,
    context: InvocationContext,
    runtime: Any,
    session_id: str,
    target_id: str,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    session, target = _target(runtime, context, session_id, target_id)
    projected = dict(result)
    for key in ("artifact", "screenshot"):
        if isinstance(projected.get(key), Mapping) and projected[key].get("ref"):
            projected[key] = _bound_blob(host, context, projected[key])
    if isinstance(projected.get("download"), Mapping):
        projected["download"] = _download_result(host, context, projected["download"])
    if isinstance(projected.get("downloads"), list):
        projected["downloads"] = [_download_result(host, context, row) for row in projected["downloads"]]
    return {
        "schema": "variant1.browser-action-view.v1",
        **_surface(session),
        "url": target.url, "title": target.title,
        "session_id": session.session_id, "target_id": target.target_id,
        "session": _session_handle(host, context, session),
        "result": projected,
        "image": projected.get("screenshot") or projected.get("artifact"),
        "page": _page_handle(host, context, session, target),
    }


def _require_current_handle(
    identity: Mapping[str, Any], session: SessionRecord,
    revision: int | None = None,
) -> None:
    if int(identity.get("generation") or -1) != session.generation:
        raise ToolError(
            "stale browser handle generation; use the latest browser result"
        )
    if revision is not None and int(identity.get("revision") or -1) != int(revision):
        raise ToolError(
            "stale browser handle revision; reacquire it from session history"
        )


async def _handle_router(
    host: Any,
    context: InvocationContext,
    identity: Mapping[str, Any],
    method: str,
    arguments: dict[str, Any],
) -> Any:
    runtime = _runtime(host)
    kind = str(identity.get("kind") or "")
    method = str(method or "")

    if kind == "session":
        session = _session(runtime, context, identity.get("id"))
        # Session identity is generation-fenced. Its mutable revision is
        # resolved to the latest record so callers never need refresh ceremony.
        _require_current_handle(identity, session)
        if method == "pages":
            session = await runtime.reconcile_current_page(
                session.session_id, scope=_scope(context), create_if_empty=False,
            )
            pages = [
                _page_handle(host, context, session, target)
                for target in runtime.targets(session.session_id)
            ]
            current = next(
                (
                    page for page in pages
                    if str(page["$variant1_handle"]["id"])
                    == str(session.current_target_id or "")
                ),
                None,
            )
            return {"current": current, "items": pages}
        if method == "new_page":
            page = await runtime.new_page(
                session.session_id, url=str(arguments.get("url") or ""),
                idempotency_key=_request_key(context, arguments), scope=_scope(context),
            )
            target = runtime.store.get_target(page.target_id)
            session = runtime.session(session.session_id)
            return _page_handle(host, context, session, target)
        if method == "history":
            history_kind = str(arguments.get("kind") or "").strip().lower()
            limit = max(1, min(int(arguments.get("limit") or 100), 1_000))
            if history_kind == "events":
                return [item.to_dict() for item in runtime.events(
                    session.session_id,
                    after_sequence=max(
                        0, int(arguments.get("after_sequence") or 0)
                    ),
                    limit=min(limit, 2_000),
                )]
            if history_kind == "operations":
                return [item.to_dict() for item in runtime.operations(
                    session.session_id, limit=limit,
                )]
            if history_kind == "downloads":
                await runtime.refresh_downloads(session.session_id)
                return [_download_result(host, context, item) for item in runtime.download_history(
                    session.session_id, limit=limit,
                    operation_id=str(arguments.get("operation_id") or ""),
                    scope=_scope(context),
                )]
            if history_kind == "traces":
                return [
                    trace_handle_envelope(
                        item,
                        session_generation=session.generation,
                        broker=host.require_runtime().broker,
                        context=context,
                    )
                    for item in runtime.traces(session.session_id, limit=limit)
                ]
            raise ToolError(
                "browser session history kind must be events, operations, "
                "downloads, or traces"
            )
        if method == "start_trace":
            trace = await runtime.start_trace(
                session.session_id, name=str(arguments.get("name") or "Browser trace"),
                options=(dict(arguments.get("options") or {})
                         if isinstance(arguments.get("options"), Mapping) else None),
                scope=_scope(context),
            )
            return trace_handle_envelope(
                trace, session_generation=session.generation,
                broker=host.require_runtime().broker, context=context,
            )
        if method == "close":
            if runtime.shared_embedded_session(session):
                try:
                    from .binding import current_browser_binding

                    binding = current_browser_binding()
                    if binding is not None:
                        binding.detach(resume_url=str(
                            runtime.store.get_target(
                                session.current_target_id
                            ).url
                            if session.current_target_id else ""
                        ))
                except Exception:
                    pass
                return {
                    "released": True,
                    "shared_embedded": True,
                    "session_id": session.session_id,
                }
            record = await runtime.close_session(session.session_id, scope=_scope(context))
            return _session_handle(host, context, record)
        raise ToolError(f"unsupported browser.session method: {method}")

    if kind == "page":
        target = runtime.store.get_target(str(identity.get("id") or ""))
        session = _session(runtime, context, target.session_id)
        # A page handle is durable tab identity, not an observation snapshot.
        # Resolve its latest target revision transparently; session-generation
        # changes and element document epochs remain the stale fences.
        _require_current_handle(identity, session)
        page = _page_ref(session, target)
        if method == "observe":
            observation = await runtime.observe(
                page,
                max_chars=max(1, min(int(arguments.get("max_chars") or 200_000), 2_000_000)),
                max_elements=max(0, min(int(arguments['max_elements'] if arguments.get('max_elements') is not None else 1_000), 5_000)),
                include_html=bool(arguments.get("include_html")),
                include_screenshot=bool(arguments.get("include_screenshot")),
                idempotency_key=_request_key(context, arguments), scope=_scope(context),
            )
            return _observation_result(host, context, runtime, observation)
        if method in {"navigate", "keys", "wait", "evaluate", "set_viewport"}:
            include_screenshot = bool(arguments.get("include_screenshot"))
            operation = method
            if method == "navigate":
                url = str(arguments.get("url") or "").strip()
                navigation = str(arguments.get("action") or "").strip().lower()
                if url and navigation:
                    raise ToolError(
                        "page.navigate accepts either url or action, not both"
                    )
                if not url and navigation not in {
                    "back", "forward", "reload", "select",
                }:
                    raise ToolError(
                        "page.navigate needs url or action=back|forward|reload|select"
                    )
                if navigation == "select":
                    refreshed = await runtime.select_page(page, scope=_scope(context))
                    target = runtime.store.get_target(refreshed.target_id)
                    session = runtime.session(session.session_id)
                    if include_screenshot:
                        observation = await runtime.observe(
                            refreshed,
                            include_screenshot=True,
                            idempotency_key=_request_key(context, arguments),
                            scope=_scope(context),
                        )
                        return _observation_result(
                            host, context, runtime, observation
                        )
                    return _page_handle(host, context, session, target)
                operation = navigation or "navigate"
            params = {
                key: value for key, value in arguments.items()
                if key not in {
                    "idempotency_key", "scope", "expected",
                    "include_screenshot", "action",
                }
            }
            result = await runtime.perform(
                page, operation, params=params,
                expected=_expected(arguments.get("expected")),
                idempotency_key=_request_key(context, arguments), scope=_scope(context),
            )
            if include_screenshot:
                result = await _attach_post_action_screenshot(
                    runtime,
                    context,
                    session,
                    target,
                    result,
                    producer=f"browser_{operation}",
                )
            return _action_result(
                host, context, runtime, session.session_id, target.target_id, result
            )
        if method == "close":
            await runtime.close_page(page, scope=_scope(context))
            return {"target_id": target.target_id, "state": "closed"}
        raise ToolError(f"unsupported browser.page method: {method}")

    if kind == "element":
        session, target, element = _element_ref(runtime, context, identity)
        if method not in _ELEMENT_ACTIONS:
            raise ToolError(f"unsupported browser.element method: {method}")
        action_options = {
            key: value for key, value in arguments.items()
            if key not in {
                "idempotency_key", "scope", "expected", "text", "value",
                "values", "keys", "include_screenshot",
            }
        }
        include_screenshot = bool(arguments.get("include_screenshot"))
        if "timeout" in action_options and "timeout_ms" not in action_options:
            action_options["timeout_ms"] = action_options.pop("timeout")
        action_options["idempotency_key"] = _request_key(context, arguments)
        if arguments.get("expected") is not None:
            action_options["expected"] = _expected(arguments.get("expected"))
        if method == "click":
            result = await runtime.click(element, scope=_scope(context), **action_options)
        elif method == "fill":
            result = await runtime.fill(
                element, str(arguments.get("text") or ""), scope=_scope(context),
                **action_options,
            )
        elif method == "select":
            values = arguments.get("values", arguments.get("value", ""))
            result = await runtime.select(
                element, values, scope=_scope(context), **action_options,
            )
        elif method == "hover":
            result = await runtime.hover(element, scope=_scope(context), **action_options)
        else:
            result = await runtime.keys(
                _page_ref(session, target), str(arguments.get("keys") or ""),
                element=element, scope=_scope(context),
                **action_options,
            )
        if include_screenshot:
            result = await _attach_post_action_screenshot(
                runtime,
                context,
                session,
                target,
                result,
                producer=f"browser_{method}",
            )
        return _action_result(
            host, context, runtime, session.session_id, target.target_id, result
        )

    if kind == "trace":
        trace = runtime.store.get_trace(str(identity.get("id") or ""))
        session = _session(runtime, context, trace.session_id)
        _require_current_handle(identity, session, trace.revision)
        if method == "stop":
            trace = await runtime.stop_trace(trace.trace_id, scope=_scope(context))
            return trace_handle_envelope(
                trace, session_generation=session.generation,
                broker=host.require_runtime().broker, context=context,
            )
        raise ToolError(f"unsupported browser.trace method: {method}")

    raise ToolError("browser handle kind is unsupported")


def register_browser_fabric_tools(host: Any) -> None:
    """Install compact descriptor-routed handles behind the browser object."""

    routers = getattr(host, "remote_handle_routers", None)
    if not isinstance(routers, dict):
        raise TypeError("host.remote_handle_routers must be a dictionary")
    routers["browser"] = (
        lambda context, identity, method, arguments:
        _handle_router(host, context, identity, method, arguments)
    )


__all__ = ["register_browser_fabric_tools"]
