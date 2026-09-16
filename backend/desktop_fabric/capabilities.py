"""Compact IPython computer-use surface over the authoritative Desktop Fabric.

The model receives plain window/view mappings and task verbs. Strong identity,
semantic-versus-physical routing, modal transitions, evidence capture, locking,
and durable receipts remain host implementation details.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from typing import Any, Awaitable, Callable

from capability_broker import InvocationContext, current_capability_invocation
from tools import ToolError
from work_fabric.scope import WorkScope, coerce_work_scope

from .models import (
    DesktopFabricError,
    DesktopObservation,
    DesktopOperation,
    WindowRecord,
)


_MAX_WINDOWS = 200
_MAX_ELEMENTS = 400
_VIEW_SCHEMAS = {"variant1.desktop-view-result.v2"}
_CLICK_ACTIONS = frozenset({
    "invoke", "select", "toggle", "expand", "collapse", "scroll_into_view",
})


def _context() -> InvocationContext:
    context = current_capability_invocation()
    if context is None:
        raise ToolError("computer use requires an admitted Python cell")
    return context


def _scope(context: InvocationContext | None = None) -> WorkScope:
    resolved = context or _context()
    scope = coerce_work_scope(resolved.work_scope)
    if resolved.chat_id and not scope.chat_id:
        scope = scope.with_updates(chat_id=resolved.chat_id)
    if scope.empty:
        raise ToolError("computer use requires a non-empty WorkScope")
    return scope


def _runtime(host: Any) -> Any:
    require_runtime = getattr(host, "require_runtime", None)
    composed = require_runtime() if callable(require_runtime) else None
    runtime = getattr(composed, "desktop", None)
    if runtime is None:
        raise ToolError("Desktop Fabric is unavailable")
    return runtime


def _domain(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except DesktopFabricError as exc:
        raise ToolError(f"{type(exc).__name__}: {exc}") from exc


async def _domain_async(awaitable: Awaitable[Any]) -> Any:
    try:
        return await awaitable
    except DesktopFabricError as exc:
        raise ToolError(f"{type(exc).__name__}: {exc}") from exc


def _window_mapping(record: WindowRecord) -> dict[str, Any]:
    return {
        "window_id": record.window_id,
        "app": record.app_id,
        "title": record.title,
        "hwnd": record.hwnd,
        "pid": record.pid,
        "bounds": list(record.bounds) if record.bounds else None,
        "foreground": bool(record.foreground),
        "minimized": bool(record.minimized),
        "live": bool(record.live),
    }


def _raw_window_identity(value: Any) -> Any:
    raw = value
    if isinstance(raw, Mapping):
        if str(raw.get("schema") or "") in _VIEW_SCHEMAS:
            nested = raw.get("window")
            if isinstance(nested, Mapping):
                raw = nested
            else:
                raw = raw.get("window_id") or raw.get("id")
        elif isinstance(raw.get("window"), Mapping):
            raw = raw["window"]
        if isinstance(raw, Mapping) and "$variant1_handle" in raw:
            raw = raw.get("$variant1_handle")
        if isinstance(raw, Mapping):
            raw = (
                raw.get("window_id")
                or raw.get("id")
                or raw.get("hwnd")
            )
    return raw


def _window_reference(runtime: Any, value: Any) -> str:
    raw = _raw_window_identity(value)
    text = str(raw or "").strip()
    if not text or len(text) > 4096 or "\x00" in text:
        raise ToolError("computer window reference is invalid")
    if not text.isdecimal():
        return text
    runtime.refresh_catalog()
    matches = [
        item for item in runtime.windows(include_missing=False)
        if int(item.hwnd or 0) == int(text)
    ]
    if len(matches) == 1:
        return matches[0].window_id
    if len(matches) > 1:
        raise ToolError("native window handle is ambiguous")
    raise ToolError(f"no open window has native handle {text}")


def _compact_element(
    element: Any, *, origin_x: int = 0, origin_y: int = 0,
) -> dict[str, Any]:
    backend_key = str(element.backend_key or "")
    control_id: Any = int(backend_key) if backend_key.isdecimal() else backend_key
    bounds = None
    if element.bounds:
        left, top, right, bottom = element.bounds
        bounds = [
            int(left) - int(origin_x),
            int(top) - int(origin_y),
            int(right) - int(origin_x),
            int(bottom) - int(origin_y),
        ]
    return {
        "id": control_id or None,
        "ref": element.element_ref,
        "role": element.role,
        "name": element.name,
        "text": element.text or None,
        "value": element.value if element.value or "value" in element.patterns else None,
        "state": element.states.get("state") or None,
        "offscreen": element.states.get("offscreen"),
        "actionable": bool(element.actionable),
        "bounds": bounds,
    }


def _screenshot_mapping(observation: DesktopObservation) -> dict[str, Any] | None:
    if not observation.image_ref:
        return None
    capture = dict(observation.capture or {})
    transform = dict(capture.get("coordinate_transform") or {})
    origin = list(transform.get("screen_origin") or (0, 0))
    size = list(transform.get("capture_size") or ())
    width = int(capture.get("width") or (size[0] if len(size) > 0 else 0) or 0)
    height = int(capture.get("height") or (size[1] if len(size) > 1 else 0) or 0)
    return {
        "id": observation.observation_id,
        "image_ref": observation.image_ref,
        "width": width or None,
        "height": height or None,
        "origin_x": int(origin[0]) if len(origin) > 0 else 0,
        "origin_y": int(origin[1]) if len(origin) > 1 else 0,
    }


def _observed_window_rect(
    observation: DesktopObservation,
) -> tuple[int, int, int, int] | None:
    transform = dict((observation.capture or {}).get("coordinate_transform") or {})
    raw = transform.get("window_rect")
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        left, top, right, bottom = (int(value) for value in raw)
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _view_result(
    window: WindowRecord,
    observation: DesktopObservation,
    *,
    include_text: bool,
    action: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    selected = list(observation.elements[:_MAX_ELEMENTS]) if include_text else []
    screenshot = _screenshot_mapping(observation)
    observed_rect = _observed_window_rect(observation)
    if screenshot is not None:
        origin_x = int(screenshot.get("origin_x") or 0)
        origin_y = int(screenshot.get("origin_y") or 0)
        coordinate_space = "screenshot"
    else:
        window_bounds = observed_rect or window.bounds or (0, 0, 0, 0)
        origin_x, origin_y = int(window_bounds[0]), int(window_bounds[1])
        coordinate_space = "window"
    window_mapping = _window_mapping(window)
    if observed_rect is not None:
        window_mapping["bounds"] = list(observed_rect)
    result = {
        "schema": "variant1.desktop-view-result.v2",
        "id": observation.observation_id,
        "observation_id": observation.observation_id,
        "window_id": observation.window_id,
        "window": window_mapping,
        "title": window.title,
        "hwnd": window.hwnd,
        "pid": window.pid,
        "mode": observation.mode,
        "text_included": bool(include_text),
        "screenshot_included": screenshot is not None,
        "screenshot": screenshot,
        "coordinate_space": coordinate_space,
        "controls": [
            _compact_element(item, origin_x=origin_x, origin_y=origin_y)
            for item in selected
        ],
        "control_page": {
            "returned": len(selected),
            "total": len(observation.elements),
            "has_more": len(selected) < len(observation.elements),
        },
    }
    if action:
        result["action"] = dict(action)
        result["action_status"] = action.get("status")
        result["input_sent"] = action.get("input_sent")
        result["action_error"] = action.get("error") or None
    if not include_text:
        result["controls_hint"] = (
            "Controls were omitted. Use view = computer.observe(window=view.window, "
            "include_text=True) before reading view.controls."
        )
    return result


def _deliver_image(runtime: Any, observation: DesktopObservation) -> None:
    if not observation.image_ref:
        return
    try:
        from desktop.service import deliver_image
        from artifacts.scopes import cas_scope_id

        context = _context()
        observation.require_scope(_scope(context))
        runtime.artifact_store.grant(
            observation.image_ref, cas_scope_id(_scope(context)),
            source_scope=f"desktop:window:{observation.window_id}", verify=False,
        )

        payload = runtime.artifact_store.read_bytes(observation.image_ref)
        deliver_image(
            base64.b64encode(payload).decode("ascii"),
            media_type="image/png",
            artifact_ref=observation.image_ref,
            producer="computer",
        )
    except Exception:
        return


def _view_observation(runtime: Any, view: Any) -> DesktopObservation:
    if not isinstance(view, Mapping):
        raise ToolError("computer action needs the complete view returned by computer.focus or observe, not its window/id string. Example: v = computer.focus(name='Notepad'); computer.click(v, target='File'). Controls are dictionaries: v.controls[0]['name'].")
    if str(view.get("schema") or "") not in _VIEW_SCHEMAS:
        raise ToolError("computer action view has an unsupported schema")
    observation_id = str(
        view.get("observation_id") or view.get("id") or ""
    ).strip()
    if not observation_id:
        raise ToolError("computer action view has no observation identity")
    observation = _domain(
        lambda: runtime.repository.get_observation(observation_id, scope=_scope())
    )
    expected_window = _window_reference(runtime, view)
    if observation.window_id != expected_window:
        raise ToolError("computer action view/window identity mismatch")
    return observation


def _view_preferences(view: Mapping[str, Any]) -> tuple[bool, bool]:
    return (
        bool(view.get("screenshot_included") or view.get("screenshot")),
        bool(view.get("text_included") or view.get("controls")),
    )


def _screen_point(
    runtime: Any, view: Mapping[str, Any], x: Any, y: Any,
) -> tuple[int, int]:
    observation = _view_observation(runtime, view)
    screenshot = _screenshot_mapping(observation)
    try:
        relative_x, relative_y = int(x), int(y)
    except (TypeError, ValueError) as exc:
        raise ToolError("coordinate input needs integer x and y") from exc
    if screenshot is not None:
        origin_x = int(screenshot.get("origin_x") or 0)
        origin_y = int(screenshot.get("origin_y") or 0)
        width = int(screenshot.get("width") or 0)
        height = int(screenshot.get("height") or 0)
        label = "screenshot"
    else:
        bounds = _observed_window_rect(observation)
        if bounds is None:
            raise ToolError("coordinate input needs a view with live window geometry; observe again")
        left, top, right, bottom = bounds
        origin_x, origin_y = left, top
        width, height = max(0, right - left), max(0, bottom - top)
        label = "window"
    if relative_x < 0 or relative_y < 0:
        raise ToolError(f"coordinate input is outside the {label}")
    if (width and relative_x >= width) or (height and relative_y >= height):
        raise ToolError(f"coordinate input is outside the {label}")
    return (
        origin_x + relative_x,
        origin_y + relative_y,
    )


def _target(value: Any) -> Any:
    if isinstance(value, int) and not isinstance(value, bool):
        return {"backend_key": str(value)}
    if isinstance(value, str) and value.strip().isdecimal():
        return {"backend_key": value.strip()}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        return value
    if value is None:
        return None
    raise ToolError("computer target must be a control id or control from the supplied view")


def _window_candidates(runtime: Any, name: str) -> list[WindowRecord]:
    runtime.refresh_catalog()
    rows = runtime.windows(query=name, include_missing=False)
    exact = [row for row in rows if row.title.casefold() == name.casefold()]
    return exact or rows


async def _focus(runtime: Any, arguments: Mapping[str, Any]) -> dict[str, Any]:
    supplied = arguments.get("window")
    name = str(arguments.get("name") or "").strip()
    if supplied is not None:
        window_id = _window_reference(runtime, supplied)
    elif name:
        matches = _window_candidates(runtime, name)
        if len(matches) != 1:
            titles = [item.title for item in matches[:10]]
            if not matches:
                available = [item.title for item in runtime.windows()[:10]]
                raise ToolError(
                    f"no open window matches {name!r}; open windows: {available}"
                )
            raise ToolError(
                f"window name {name!r} matched {len(matches)} windows: {titles}"
            )
        window_id = matches[0].window_id
    else:
        window_id = _domain(lambda: runtime.current_window()).window_id
    _result, window, observation = await _domain_async(
        runtime.focus_session({"window_id": window_id}, scope=_scope())
    )
    return _view_result(window, observation, include_text=True)


async def _observe(runtime: Any, arguments: Mapping[str, Any]) -> dict[str, Any]:
    supplied = arguments.get("window")
    window_id = (
        _window_reference(runtime, supplied)
        if supplied is not None
        else _domain(lambda: runtime.current_window()).window_id
    )
    screenshot = bool(arguments.get("include_screenshot", True))
    include_text = bool(arguments.get("include_text", False))
    observation = await _domain_async(runtime.observe(
        window_id, mode="uia", include_image=screenshot, scope=_scope(),
    ))
    window = _domain(lambda: runtime.repository.get_window(observation.window_id))
    if screenshot:
        _deliver_image(runtime, observation)
    return _view_result(window, observation, include_text=include_text)


def _input_sent(operation: DesktopOperation) -> bool | None:
    for receipt in (operation.evidence, operation.dispatch):
        if isinstance(receipt.get("input_sent"), bool):
            return receipt['input_sent']
    if operation.state == "unknown_effect":
        return None
    if operation.state == "failed":
        return False
    if operation.evidence.get("delivery_possible") is False:
        return False
    return operation.state in {"dispatched", "observed", "verified", "no_effect"}


async def _action_view(
    runtime: Any,
    operation: DesktopOperation,
    *,
    source_view: Mapping[str, Any],
    public_action: str,
) -> dict[str, Any]:
    screenshot, include_text = _view_preferences(source_view)
    observation: DesktopObservation | None = None
    observation_error = ""
    if operation.after_observation_id:
        try:
            observation = runtime.repository.get_observation(
                operation.after_observation_id, scope=_scope())
        except DesktopFabricError:
            observation = None
    if observation is None:
        try:
            observation = await _domain_async(runtime.observe(
                operation.window_id, mode="uia", include_image=screenshot, scope=_scope(),
            ))
        except Exception as exc:
            # The canonical input receipt already exists. Losing its window
            # during a close must not erase whether input was delivered.
            observation_error = str(exc)[:1000]
    window = _domain(lambda: runtime.repository.get_window(
        observation.window_id if observation is not None else operation.window_id))
    if screenshot and observation is not None:
        _deliver_image(runtime, observation)
    action = {
        "name": public_action,
        "status": operation.state,
        "input_sent": _input_sent(operation),
    }
    if operation.error:
        action["error"] = operation.error
    if observation is None:
        return {
            "schema": "variant1.desktop-view-result.v2", "id": None, "observation_id": None,
            "window_id": operation.window_id, "window": _window_mapping(window), "title": window.title,
            "mode": "uia", "controls": [], "control_page": {"returned": 0, "total": 0, "has_more": False},
            "screenshot": None, "screenshot_included": False, "text_included": False,
            "action": {**action, "receipt_id": operation.operation_id},
            "action_status": operation.state, "input_sent": action['input_sent'],
            "action_error": operation.error or None, "observation_status": "unavailable",
            "observation_error": observation_error, "fresh_observation_required": True,
        }
    try:
        from observability.trace_events import record_trace_event

        record_trace_event(
            "desktop:operation_result",
            status=operation.state,
            operation_id=operation.operation_id,
            action=public_action,
            input_sent=action["input_sent"],
            has_error=bool(operation.error),
            window_id=operation.window_id,
        )
    except Exception:
        pass
    return _view_result(
        window, observation, include_text=include_text, action=action,
    )


async def _perform_action(
    runtime: Any,
    context: InvocationContext,
    operation: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    view = arguments.get("view")
    observation = _view_observation(runtime, view)
    window_id = observation.window_id
    target = None
    action = operation
    action_arguments: dict[str, Any] = {}

    if operation == "click":
        target = _target(arguments.get("target"))
        semantic_action = str(arguments.get("action") or "").strip().casefold()
        if semantic_action and semantic_action not in _CLICK_ACTIONS:
            raise ToolError(
                "computer.click action must be invoke, select, toggle, expand, "
                "collapse, or scroll_into_view"
            )
        has_point = arguments.get("x") is not None or arguments.get("y") is not None
        if target is not None and has_point:
            raise ToolError(
                "computer.click accepts either target or x/y, not both. "
                "Use computer.click(view, target=view.controls[0]) or "
                "computer.click(view, x=100, y=50) with coordinates from that view.",
                code="desktop_invalid_target", cause_class="model",
            )
        if semantic_action:
            if target is None:
                raise ToolError("computer.click semantic action needs a target")
            if arguments.get("button") is not None or arguments.get("count") is not None:
                raise ToolError(
                    "computer.click semantic action does not accept button or count",
                    code="desktop_invalid_target", cause_class="model",
                )
            action = semantic_action
        elif target is None:
            x, y = _screen_point(runtime, view, arguments.get("x"), arguments.get("y"))
            action_arguments.update({"x": x, "y": y})
        if not semantic_action:
            action_arguments["button"] = str(arguments.get("button") or "left")
            action_arguments["double"] = int(arguments.get("count") or 1) == 2
    elif operation == "type_text":
        action = "type_text"
        action_arguments["text"] = str(arguments.get("text") or "")
    elif operation == "press_key":
        action = "send_keys"
        action_arguments["keys"] = arguments.get("keys")
    elif operation == "set_value":
        action = "set_value"
        target = _target(arguments.get("target"))
        action_arguments["value"] = str(arguments.get("value") or "")
    elif operation == "scroll":
        x, y = _screen_point(runtime, view, arguments.get("x"), arguments.get("y"))
        delta = int(arguments.get("delta") or 0)
        if delta == 0:
            raise ToolError("computer.scroll delta must be nonzero")
        action_arguments.update({
            "x": x,
            "y": y,
            "direction": "down" if delta > 0 else "up",
            "amount": max(1, min(20, abs(delta) // 100 or 1)),
        })
    elif operation == "drag":
        from_x, from_y = _screen_point(
            runtime, view, arguments.get("from_x"), arguments.get("from_y"))
        to_x, to_y = _screen_point(
            runtime, view, arguments.get("to_x"), arguments.get("to_y"))
        action_arguments.update({
            "x1": from_x, "y1": from_y,
            "x2": to_x, "y2": to_y,
        })
    else:
        raise ToolError(f"unsupported computer operation: {operation}")

    screenshot, _include_text = _view_preferences(view)
    desktop_operation = await _domain_async(runtime.act(
        window_id,
        action,
        target=target,
        delivery="auto",
        arguments=action_arguments,
        expect={},
        scope=_scope(context),
        idempotency_key=str(context.idempotency_key or ""),
        include_image_evidence=screenshot,
        source_observation=observation,
    ))
    return await _action_view(
        runtime,
        desktop_operation,
        source_view=view,
        public_action=action,
    )


async def desktop_computer_operation(
    host: Any, arguments: Mapping[str, Any],
) -> Any:
    """Dispatch one compact ``computer`` object method."""

    operation = str(arguments.get("operation") or "").strip().casefold()
    runtime = _runtime(host)
    if operation == "list_windows":
        runtime.refresh_catalog()
        query = str(arguments.get("query") or "")
        rows = []
        for item in runtime.windows(query=query, include_missing=False):
            if not str(item.title or "").strip():
                continue
            if item.bounds:
                left, top, right, bottom = item.bounds
                if right <= left or bottom <= top:
                    if not item.minimized:
                        continue
            rows.append(item)
            if len(rows) >= _MAX_WINDOWS:
                break
        return [_window_mapping(item) for item in rows]
    if operation == "focus":
        result = await _focus(runtime, arguments)
    elif operation == "observe":
        result = await _observe(runtime, arguments)
    else:
        result = await _perform_action(runtime, _context(), operation, arguments)
    screenshot = result.get("screenshot") if isinstance(result, Mapping) else None
    if isinstance(screenshot, Mapping) and screenshot.get("image_ref"):
        from artifacts.blob_handles import blob_handle_envelope

        try:
            result["image"] = blob_handle_envelope(
                host, _context(), str(screenshot["image_ref"]), metadata=dict(screenshot),
            )
        except Exception as exc:
            result["image_export_error"] = (str(exc) or type(exc).__name__)[:300]
    return result


__all__ = ["desktop_computer_operation"]
