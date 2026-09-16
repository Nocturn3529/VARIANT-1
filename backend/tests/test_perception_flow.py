"""Tests for deterministic UIA perception and its single bounded reread."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import desktop.context as dcontext
import desktop.perception_flow as dflow
import desktop.perception_recovery as prec
import desktop.runtime as druntime
import desktop_control as dc


def _ctrl(n: int, name: str = "Btn") -> dict:
    return {
        "id": n,
        "key": f"rn:Button|{name}#{n}",
        "role": "Button",
        "name": name,
        "value": "",
        "state": "",
        "offscreen": False,
        "bounds": [0, 0, 10, 10],
        "control": None,
    }


@pytest.fixture
def desktop_runtime():
    return druntime.DesktopRuntime()


@pytest.fixture
def context(desktop_runtime):
    with druntime.bind_runtime(desktop_runtime):
        return dc._control_context()


@pytest.fixture
def quiet_activity():
    with (
        patch.object(dcontext.DesktopControlContext, "_emit_perception", new=AsyncMock()),
        patch.object(dcontext.DesktopControlContext, "_emit_desktop_error", new=AsyncMock()),
        patch.object(
            dcontext.DesktopControlContext,
            "_emit_perception_quality_metrics",
            new=AsyncMock(),
        ),
    ):
        yield


@pytest.fixture
def immediate_reread(desktop_runtime):
    cfg = prec.RecoveryConfig(
        reread_enabled=True,
        min_controls_for_uia=3,
        reread_wait_sec=0.0,
    )
    desktop_runtime.recovery_cfg = cfg
    yield cfg


@pytest.mark.asyncio
async def test_healthy_tree_is_read_once(context, quiet_activity, immediate_reread):
    controls = [_ctrl(i, f"B{i}") for i in range(1, 6)]
    collect = AsyncMock(return_value=("Healthy App", controls))
    formatter = AsyncMock(return_value="formatted body")

    with (
        patch.object(context, "_uia_collect", collect),
        patch.object(context, "_format_perception_output", formatter),
    ):
        title, text, result = await dflow.perceive_ui(
            context, {}, tool_name="focus_window",
        )

    assert title == "Healthy App"
    assert result == controls
    assert "formatted body" in text
    collect.assert_awaited_once()


@pytest.mark.asyncio
async def test_skip_reread_is_explicit(context, quiet_activity, immediate_reread):
    collect = AsyncMock(return_value=("Sparse", [_ctrl(1)]))
    with (
        patch.object(context, "_uia_collect", collect),
        patch.object(context, "_format_perception_output", AsyncMock(return_value="once")),
    ):
        await dflow.perceive_ui(
            context, {"skip_reread": True}, tool_name="computer.observe",
        )
    collect.assert_awaited_once()


@pytest.mark.asyncio
async def test_sparse_tree_gets_exactly_one_reread(
    context, quiet_activity, immediate_reread,
):
    calls = 0

    async def collect(_max_controls=400):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "Slow App", [_ctrl(1)]
        return "Slow App", [_ctrl(i) for i in range(1, 6)]

    formatter = AsyncMock(return_value="recovered body")
    with (
        patch.object(context, "_uia_collect", collect),
        patch.object(context, "_format_perception_output", formatter),
        patch.object(context, "_refocus_locked_target", AsyncMock()),
        patch("desktop.perception_flow.asyncio.sleep", AsyncMock()),
    ):
        _title, text, controls = await dflow.perceive_ui(
            context, {}, tool_name="focus_window",
        )

    assert calls == 2
    assert len(controls) == 5
    assert "recovered body" in text
    assert formatter.await_args.kwargs["force_full"] is True
    assert "UIA reread recovered" in formatter.await_args.kwargs["suffix"]


@pytest.mark.asyncio
async def test_sparse_reread_never_escalates_to_pixels_or_a_model(
    context, quiet_activity, immediate_reread,
):
    collect = AsyncMock(return_value=("Canvas App", [_ctrl(1)]))
    formatter = AsyncMock(return_value="still sparse")
    with (
        patch.object(context, "_uia_collect", collect),
        patch.object(context, "_format_perception_output", formatter),
        patch.object(context, "_refocus_locked_target", AsyncMock()),
        patch("desktop.perception_flow.asyncio.sleep", AsyncMock()),
    ):
        _title, text, controls = await dflow.perceive_ui(
            context, {}, tool_name="focus_window",
        )

    assert collect.await_count == 2
    assert len(controls) == 1
    suffix = formatter.await_args.kwargs["suffix"]
    assert "UIA reread remained sparse" in suffix
    assert "ground" not in text.lower()
    assert "click" not in suffix.lower()


@pytest.mark.asyncio
async def test_disabled_reread_reads_once(context, quiet_activity, desktop_runtime):
    cfg = prec.RecoveryConfig(reread_enabled=False)
    collect = AsyncMock(return_value=("App", [_ctrl(1)]))
    desktop_runtime.recovery_cfg = cfg
    with (
        patch.object(context, "_uia_collect", collect),
        patch.object(context, "_format_perception_output", AsyncMock(return_value="x")),
    ):
        await dflow.perceive_ui(context, {}, tool_name="focus_window")
    collect.assert_awaited_once()
