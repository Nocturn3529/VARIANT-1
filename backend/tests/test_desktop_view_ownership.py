from __future__ import annotations

from types import SimpleNamespace

import pytest

from desktop.input_primitives import _click_direct
from desktop_fabric.adapter import AdapterObservation, WindowsDesktopAdapter
from desktop_fabric.capabilities import _screen_point, _view_result
from desktop_fabric.models import (
    DesktopElement, DesktopObservation, DesktopStaleReference, WindowRecord,
)
from desktop_fabric.service import DesktopFabric, DesktopRecoveryReport
from work_fabric.scope import WorkScope


def _window() -> WindowRecord:
    return WindowRecord(
        window_id="win_view", app_id="app_view", hwnd=99, pid=7,
        pid_started_at=10.0, title="Editor", bounds=(10, 20, 810, 620),
    )


def _element(*, runtime_id=(42, 1), generation=1) -> DesktopElement:
    return DesktopElement(
        element_ref="el_editor", observation_id="obs_old",
        window_id="win_view", window_generation=1,
        element_generation=generation, runtime_id=tuple(runtime_id),
        backend_key="1", role="Edit", name="Editor", bounds=(130, 260, 700, 550),
    )


def _adapter() -> WindowsDesktopAdapter:
    return WindowsDesktopAdapter(desktop_control=object(), catalog=object())


class _Control:
    def __init__(self, runtime_id):
        self.runtime_id = tuple(runtime_id)

    def GetRuntimeId(self):
        return list(self.runtime_id)


def test_reused_backend_key_cannot_retarget_a_different_live_uia_control(monkeypatch):
    adapter = _adapter()
    old = _Control((42, 1))
    replacement = _Control((42, 2))
    record = {"id": 1, "key": "aid:editor#1", "control": old}
    monkeypatch.setattr(
        "desktop.action_resolve._ensure_live",
        lambda _ctx, _auto, _record: replacement,
    )

    with pytest.raises(DesktopStaleReference, match="identity changed"):
        adapter._ensure_exact_element(None, None, None, _element(), record)


def test_same_chat_view_can_reuse_an_exact_runtime_id_after_re_resolution(monkeypatch):
    adapter = _adapter()
    old = _Control((42, 1))
    rebound = _Control((42, 1))
    record = {"id": 1, "key": "aid:editor#1", "control": old}
    monkeypatch.setattr(
        "desktop.action_resolve._ensure_live",
        lambda _ctx, _auto, _record: rebound,
    )

    assert adapter._ensure_exact_element(None, None, None, _element(), record) is rebound


def test_runtime_id_free_view_requires_its_current_held_control(monkeypatch):
    adapter = _adapter()
    adapter._generations["win_view"] = 1
    old = _Control(())
    replacement = _Control(())
    record = {"id": 1, "key": "aid:editor#1", "control": old}
    monkeypatch.setattr(
        "desktop.action_resolve._ensure_live",
        lambda _ctx, _auto, _record: old,
    )
    assert adapter._ensure_exact_element(
        None, None, None, _element(runtime_id=()), record,
    ) is old

    monkeypatch.setattr(
        "desktop.action_resolve._ensure_live",
        lambda _ctx, _auto, _record: replacement,
    )
    with pytest.raises(DesktopStaleReference, match="cannot be proven"):
        adapter._ensure_exact_element(
            None, None, None, _element(runtime_id=()), record,
        )

    adapter._generations["win_view"] = 2
    with pytest.raises(DesktopStaleReference, match="cannot be proven"):
        adapter._ensure_exact_element(
            None, None, None, _element(runtime_id=()), record,
        )


@pytest.mark.asyncio
async def test_adapter_carries_live_window_rect_even_without_capture(monkeypatch):
    class Context:
        def __init__(self, _session):
            pass

        async def _uia(self, call):
            return call()

    monkeypatch.setattr("desktop.context.DesktopControlContext", Context)
    monkeypatch.setattr(
        "desktop.vision_capture.win32_window_rect",
        lambda hwnd: (100, 200, 900, 800) if hwnd == 99 else None,
    )
    session = SimpleNamespace(
        last_snapshot={}, active_window={"hwnd": 99}, current_modal=None,
        session_id="fabric-view", snapshot_title="Editor",
    )
    observation = await _adapter()._extract_observation_key(
        "win_view", session, completeness="uia-only",
    )
    assert observation.metadata["window_rect"] == [100, 200, 900, 800]


@pytest.mark.asyncio
async def test_ui_a_only_view_uses_live_geometry_for_bounds_and_pointer_points(monkeypatch):
    window = _window()
    repository = SimpleNamespace(record_observation=lambda observation: observation)
    adapter = SimpleNamespace(validate_window=lambda _record: {"live": True})
    fabric = DesktopFabric(
        repository=repository, artifact_store=object(), adapter=adapter,
        backend_instance_id="test", recovery_report=DesktopRecoveryReport("test"),
    )
    live = AdapterObservation(
        uia=({
            "id": 1, "key": "aid:editor#1", "role": "Edit", "name": "Editor",
            "bounds": [130, 260, 700, 550], "runtime_id": [42, 1],
            "actionable": True,
        },),
        uia_generation=1,
        metadata={"window_rect": [100, 200, 900, 800]},
    )
    observation = await fabric._record_observation(
        window, live, mode="uia", include_image=False,
        scope=WorkScope(chat_id="chat-view"),
    )
    view = _view_result(window, observation, include_text=True)
    assert view["screenshot"] is None
    assert view["window"]["bounds"] == [100, 200, 900, 800]
    assert view["controls"][0]["bounds"] == [30, 60, 600, 350]
    monkeypatch.setattr(
        "desktop_fabric.capabilities._view_observation",
        lambda _runtime, _view: observation,
    )
    assert _screen_point(None, view, 30, 60) == (130, 260)
    monkeypatch.setattr(
        "desktop.vision_capture.win32_window_rect",
        lambda _hwnd: (120, 220, 920, 820),
    )
    with pytest.raises(DesktopStaleReference, match="geometry changed"):
        _adapter().validate_source_geometry(window, observation)


def test_no_image_view_without_live_geometry_does_not_use_catalog_bounds(monkeypatch):
    observation = DesktopObservation(
        observation_id="obs_old", window_id="win_view", window_generation=1,
        mode="uia", elements=(), scope=WorkScope(chat_id="chat-view"),
    )
    view = _view_result(_window(), observation, include_text=False)
    monkeypatch.setattr(
        "desktop_fabric.capabilities._view_observation",
        lambda _runtime, _view: observation,
    )
    with pytest.raises(Exception, match="live window geometry"):
        _screen_point(None, view, 30, 60)


@pytest.mark.asyncio
async def test_stale_identity_is_rejected_before_a_dispatch_receipt():
    window = _window()
    element = _element()
    observation = DesktopObservation(
        observation_id="obs_old", window_id=window.window_id,
        window_generation=window.generation, mode="uia", elements=(element,),
        scope=WorkScope(chat_id="chat-view"),
    )
    calls = []

    async def preflight(_record, **_kwargs):
        calls.append("preflight")

    async def reject(_record, _element):
        calls.append("identity")
        raise DesktopStaleReference("changed")

    adapter = SimpleNamespace(
        validate_window=lambda _record: {"live": True},
        preflight=preflight, validate_target_identity=reject,
    )
    repository = SimpleNamespace(prepare_operation=lambda **_kwargs: calls.append("receipt"))
    fabric = DesktopFabric(
        repository=repository, artifact_store=object(), adapter=adapter,
        backend_instance_id="test", recovery_report=DesktopRecoveryReport("test"),
    )
    with pytest.raises(DesktopStaleReference, match="changed"):
        await fabric.act(
            window, "click", target=element, scope=WorkScope(chat_id="chat-view"),
            include_image_evidence=False, source_observation=observation,
        )
    assert calls == ["preflight", "identity"]


def test_unavailable_middle_click_never_falls_through_to_left_click():
    calls = []
    auto = SimpleNamespace(Click=lambda *_args: calls.append("left"))
    with pytest.raises(Exception, match="middle click is unavailable"):
        _click_direct(auto, {"x": 1, "y": 2, "button": "middle"})
    assert calls == []
