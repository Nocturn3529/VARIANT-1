from __future__ import annotations

import desktop.context as dcontext
import desktop.modals as dmodals
import desktop.session as dsession


def test_submodules_read_session_directly():
    session = dsession.DesktopSessionState(session_id="ctx_direct")
    ctx = dcontext.DesktopControlContext(session)
    session.snapshot_title = "Calc"
    session.last_snapshot = {2: {"id": 2, "role": "Button", "name": "OK"}}
    assert ctx.session.snapshot_title == "Calc"
    assert 2 in ctx.session.last_snapshot


def test_control_context_stable_id_uses_session():
    session = dsession.DesktopSessionState(session_id="ctx_stable")
    ctx = dcontext.DesktopControlContext(session)
    assert ctx._stable_id("edit-1") == 1
    assert ctx._stable_id("edit-1") == 1
    assert ctx._stable_id("btn-2") == 2
    assert session.id_registry["edit-1"] == 1


def test_control_context_modal_state_is_session_owned():
    import desktop.modal_detect as mdetect

    session = dsession.DesktopSessionState(session_id="ctx_modal")
    ctx = dcontext.DesktopControlContext(session)
    session.current_modal = mdetect.ModalSnapshot(
        present=True,
        modal_type=mdetect.ModalType.CONFIRMATION.value,
        title="Save",
        summary="",
        blocking=True,
        confidence="high",
        source="uia",
        actions=(),
    )
    assert session.current_modal.blocking is True
    assert session.current_modal.title == "Save"
    note = dmodals.modal_context_note(ctx)
    assert "Dialog detected automatically" in note
    assert "Save" in note


def test_control_context_injectables_read_from_runtime():
    import desktop.perception_recovery as prec
    import desktop.runtime as druntime

    runtime = druntime.get_runtime()
    previous = runtime.recovery_cfg
    try:
        injected = prec.RecoveryConfig(reread_enabled=False)
        runtime.recovery_cfg = injected
        session = dsession.DesktopSessionState(session_id="ctx_runtime")
        assert dcontext.DesktopControlContext(session).recovery_config() is injected
    finally:
        runtime.recovery_cfg = previous
