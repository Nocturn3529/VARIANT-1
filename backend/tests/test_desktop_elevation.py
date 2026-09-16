"""UIPI / elevated-target detection: helpers, error taxonomy, and the seams in
focus_window and the bounded UIA reread.

Background (probed live 2026-07-08): an elevated process's windows are
completely invisible to an un-elevated UIA client — Win32 EnumWindows sees the
HWND, the UIA root lists nothing. Retries can never recover, so both seams must
name the cause instead of reporting a generic "not found" / empty tree.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import desktop.context as dcontext
import desktop.targeting as dtarget
import desktop.elevation as delev
import desktop.session as dsession
import desktop_control as dc
import desktop.errors as derr
import desktop.perception_recovery as prec
import desktop.perception_flow as dflow
import desktop.runtime as druntime
import tools


_HIT = {"hwnd": 12345, "title": "Task Manager", "pid": 4242}


# ---- module helpers ----------------------------------------------------------

def test_find_elevated_window_short_circuits_when_we_are_elevated():
    with patch.object(delev, "our_process_elevated", return_value=True):
        assert delev.find_elevated_window("Task Manager") is None


def test_find_elevated_window_empty_query():
    assert delev.find_elevated_window("") is None
    assert delev.find_elevated_window(None) is None


def test_foreground_mismatch_none_when_we_are_elevated():
    with patch.object(delev, "our_process_elevated", return_value=True):
        assert delev.foreground_elevation_mismatch() is None


def test_uipi_guidance_names_window_and_alternatives():
    text = delev.uipi_guidance("Task Manager")
    assert "Task Manager" in text
    assert "ELEVATED" in text
    assert "Run VARIANT-1 elevated" in text
    assert "retry" in text.lower()


# ---- error taxonomy ------------------------------------------------------------

def test_elevated_target_error_taxonomy():
    err = derr.elevated_target_error(window="Task Manager", pid=4242, tool="read_ui")
    assert err.error_type == derr.DesktopErrorType.ELEVATED_TARGET
    assert err.severity == derr.ErrorSeverity.UNAVAILABLE
    assert not err.is_recoverable
    assert err.recovery_action == derr.RecoveryAction.USER_ESCALATE.value
    assert "4242" in err.detail
    tagged = derr.tagged_message("blocked", err)
    assert "[desktop_error:ELEVATED_TARGET]" in tagged
    assert derr.parse_error_tag(tagged) == derr.DesktopErrorType.ELEVATED_TARGET


# ---- focus_window seam ---------------------------------------------------------

class _FakeCtx:
    """Just enough context for dtarget.focus_window to reach the not-found path."""

    def __init__(self):
        self.noted = []
        self.session = dsession.DesktopSessionState(
            session_id="desktop_elevation_test",
        )

    def _ensure_gates(self):
        return None

    def _load_uia(self):
        return object()

    async def _uia(self, fn):
        return fn()

    def _note_result(self, ok):
        self.noted.append(ok)


@pytest.mark.asyncio
async def test_focus_window_names_elevated_window_instead_of_not_found():
    ctx = _FakeCtx()
    with patch.object(dtarget, "find_window", return_value=(None, [])), \
         patch.object(delev, "find_elevated_window", return_value=dict(_HIT)):
        with pytest.raises(tools.ToolError) as ei:
            await dtarget.focus_window(ctx, {"name": "Task Manager"})
    msg = str(ei.value)
    assert "[desktop_error:ELEVATED_TARGET]" in msg
    assert "Task Manager" in msg
    assert "Run VARIANT-1 elevated" in msg
    assert ctx.noted == [False]


@pytest.mark.asyncio
async def test_focus_window_unmatched_window_keeps_normal_not_found():
    ctx = _FakeCtx()
    with patch.object(dtarget, "find_window", return_value=(None, ["Notepad"])), \
         patch.object(delev, "find_elevated_window", return_value=None), \
         patch("time.sleep"):
        with pytest.raises(tools.ToolError) as ei:
            await dtarget.focus_window(ctx, {"name": "Ghost App"})
    msg = str(ei.value)
    assert "no open window matches" in msg
    assert "ELEVATED_TARGET" not in msg


# ---- bounded perception reread -------------------------------------------------

async def _noop_emit(*_a, **_k):
    return None


@pytest.fixture
def mute_activity():
    with patch.object(dcontext.DesktopControlContext, "_emit_perception", new=AsyncMock(side_effect=_noop_emit)), \
         patch.object(dcontext.DesktopControlContext, "_emit_desktop_error", new=AsyncMock(side_effect=_noop_emit)), \
         patch.object(dcontext.DesktopControlContext, "_emit_perception_quality_metrics", new=AsyncMock(side_effect=_noop_emit)):
        yield


@pytest.fixture
def fast_reread():
    cfg = prec.RecoveryConfig(
        reread_enabled=True,
        min_controls_for_uia=3,
        reread_wait_sec=0.0,
    )
    runtime = druntime.DesktopRuntime()
    runtime.recovery_cfg = cfg
    with druntime.bind_runtime(runtime):
        yield cfg


@pytest.mark.asyncio
async def test_reread_zero_controls_elevated_foreground_stops_immediately(
        mute_activity, fast_reread):
    calls = {"n": 0}

    async def mock_collect(self, max_controls=400):
        calls["n"] += 1
        return "", []

    with patch.object(dcontext.DesktopControlContext, "_uia_collect", mock_collect), \
         patch.object(delev, "foreground_elevation_mismatch", return_value=dict(_HIT)):
        title, text, controls = await dflow.perceive_ui(
            dc._control_context(), {}, tool_name="computer.observe",
        )

    assert calls["n"] == 1                      # no futile wait/refocus retries
    assert controls == []
    assert "[desktop_error:ELEVATED_TARGET]" in text
    assert "Task Manager" in text
    assert "Run VARIANT-1 elevated" in text


@pytest.mark.asyncio
async def test_zero_controls_without_elevation_gets_one_reread(
        mute_activity, fast_reread):
    calls = {"n": 0}

    async def mock_collect(self, max_controls=400):
        calls["n"] += 1
        if calls["n"] == 1:
            return "Slow App", []
        return "Slow App", [
            {"id": i, "key": f"rn:Button|B{i}#{i}", "role": "Button", "name": f"B{i}",
             "value": "", "state": "", "offscreen": False,
             "bounds": [0, 0, 10, 10], "control": None}
            for i in range(1, 6)
        ]

    with patch.object(dcontext.DesktopControlContext, "_uia_collect", mock_collect), \
         patch.object(delev, "foreground_elevation_mismatch", return_value=None), \
         patch.object(dcontext.DesktopControlContext, "_format_perception_output",
                      AsyncMock(return_value="recovered")), \
         patch.object(dcontext.DesktopControlContext, "_refocus_locked_target", AsyncMock()), \
         patch("asyncio.sleep", AsyncMock()):
        _title, text, controls = await dflow.perceive_ui(
            dc._control_context(), {}, tool_name="focus_window",
        )

    assert calls["n"] == 2
    assert len(controls) == 5
    assert "recovered" in text
