"""Automated tests for desktop_control core perception and targeting logic."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

import desktop.action_resolve as dactions
from desktop.catalog import DESKTOP_LOG_TOOLS, DESKTOP_SURFACE_TOOLS
import desktop.constants as dconst
import desktop.context as dcontext
import desktop.input_primitives as dinput
import desktop.session as ds
import desktop.targeting as dtarget
import desktop.tree as dtree
import desktop.window_pick as dwindow
import desktop_control as dc
import desktop.errors as derr
import tools
from support.mock_uia import MockControl, MockRect, desktop_from_windows, node_from_spec, window_tree
from support.snapshots import assert_matches_golden, collect_snapshot, load_golden


def test_public_surface_excludes_implementation_modules():
    namespace = {}
    exec("from desktop_control import *", namespace)
    exported = {name for name in namespace if not name.startswith("__")}
    assert exported == set(dc.__all__)
    assert not {
        "dinput", "dflow", "grnd", "cmv", "functools", "platform", "tools",
    } & exported


def test_computer_is_the_only_desktop_surface_and_log_root():
    assert DESKTOP_SURFACE_TOOLS == {"computer"}
    assert DESKTOP_LOG_TOOLS == {"computer"}


def _collect_with_modal_patch(auto, **kwargs):
    with patch("desktop.modal_detect.scan_modal_uia", return_value=None):
        return dtree.collect_controls(dc._control_context(), auto, **kwargs)


# ---- golden / snapshot: _collect_controls ------------------------------------


@pytest.mark.parametrize(
    "scenario",
    ["simple_notepad", "dense_sidebar", "unnamed_listitems", "selection_states"],
)
def test_collect_controls_matches_golden(scenario):
    spec = load_golden(scenario)
    # Rebuild tree from golden title + infer structure from scenario name fixtures
    trees = {
        "simple_notepad": {
            "name": "Notepad",
            "role": "WindowControl",
            "children": [
                {"role": "Button", "name": "Save", "automation_id": "saveBtn"},
                {"role": "Edit", "name": "", "value": "hello world"},
                {"role": "Text", "name": "Status"},
            ],
        },
        "dense_sidebar": {
            "name": "Discord",
            "role": "WindowControl",
            "children": [
                {"role": "ListItem", "name": f"Channel {i}", "automation_id": f"ch{i}"}
                for i in range(1, 8)
            ] + [
                {"role": "Button", "name": "Send"},
                {"role": "Edit", "name": "Message", "value": ""},
            ],
        },
        "unnamed_listitems": {
            "name": "Chat App",
            "role": "WindowControl",
            "children": [
                {
                    "role": "ListItem",
                    "name": "",
                    "label_children": [{"role": "Text", "name": "Alice"}],
                },
                {
                    "role": "ListItem",
                    "name": "",
                    "label_children": [{"role": "Text", "name": "Bob"}],
                },
                {"role": "Button", "name": "Reply"},
            ],
        },
        "selection_states": {
            "name": "Settings",
            "role": "WindowControl",
            "children": [
                {"role": "TabItem", "name": "General", "selected": True},
                {"role": "TabItem", "name": "Privacy", "selected": False},
                {"role": "CheckBox", "name": "Enable notifications", "toggle_state": 1},
                {"role": "RadioButton", "name": "Dark mode", "selected": True},
            ],
        },
    }
    auto, win = window_tree(trees[scenario])
    title, controls = _collect_with_modal_patch(auto, top=win, max_depth=50)
    actual = collect_snapshot(title, controls)
    assert_matches_golden(actual, spec)


def test_collect_controls_keeps_readable_state_but_skips_unreadable_roles():
    auto, win = window_tree({
        "name": "App",
        "role": "WindowControl",
        "children": [
            {"role": "Pane", "name": "Container"},
            {"role": "Text", "name": "Label only"},
            {"role": "Button", "name": "OK"},
        ],
    })
    title, controls = _collect_with_modal_patch(auto, top=win)
    assert title == "App"
    assert [control["role"] for control in controls] == ["Text", "Button"]
    assert controls[0]["name"] == "Label only"
    assert controls[0]["actionable"] is False
    assert controls[1]["actionable"] is True


def test_collect_controls_stable_ids_across_reads():
    auto, win = window_tree({
        "name": "App",
        "role": "WindowControl",
        "children": [
            {"role": "Button", "name": "A", "automation_id": "a"},
            {"role": "Button", "name": "B", "automation_id": "b"},
        ],
    })
    _, c1 = _collect_with_modal_patch(auto, top=win)
    _, c2 = _collect_with_modal_patch(auto, top=win)
    assert [x["id"] for x in c1] == [x["id"] for x in c2]
    assert [x["key"] for x in c1] == [x["key"] for x in c2]


def test_collect_controls_dedupes_identical_siblings():
    auto, win = window_tree({
        "name": "App",
        "role": "WindowControl",
        "children": [
            {"role": "Button", "name": "Item", "automation_id": ""},
            {"role": "Button", "name": "Item", "automation_id": ""},
        ],
    })
    _, controls = _collect_with_modal_patch(auto, top=win)
    keys = [c["key"] for c in controls]
    assert keys == ["rn:Button|Item#1", "rn:Button|Item#2"]


def test_collect_controls_collapses_proven_identified_edit_parent_duplicate():
    bounds = MockRect(20, 30, 420, 62)
    inner = MockControl(
        name="Replace", role="EditControl", value="ready", bounds=bounds,
    )
    identified = MockControl(
        name="Replace", role="EditControl", automation_id="ReplaceTextBox",
        value="ready", bounds=bounds, children=[inner],
    )
    win = MockControl(
        name="Editor", role="WindowControl", children=[identified],
    )
    auto = desktop_from_windows([])

    _, controls = _collect_with_modal_patch(auto, top=win)

    edits = [item for item in controls if item["role"] == "Edit"]
    assert len(edits) == 1
    assert edits[0]["key"] == "aid:ReplaceTextBox#1"
    assert edits[0]["control"] is identified
    assert edits[0]["value"] == "ready"


def test_collect_controls_joins_fresh_parent_wrapper_by_runtime_id():
    bounds = MockRect(20, 30, 420, 62)
    inner = MockControl(
        name="Find", role="EditControl", value="needle", bounds=bounds,
        runtime_id=(42, 900, 2),
    )
    identified = MockControl(
        name="Find", role="EditControl", automation_id="TextBox",
        value="needle", bounds=bounds, runtime_id=(42, 900, 1),
        children=[inner],
    )
    # Installed UIA returns a fresh Python wrapper from GetParentControl().
    # Its runtime identity, rather than object identity, joins it to the
    # already-walked parent.
    parent_wrapper = MockControl(
        name="Find", role="EditControl", automation_id="TextBox",
        value="needle", bounds=bounds, runtime_id=(42, 900, 1),
    )
    inner.GetParentControl = lambda: parent_wrapper
    win = MockControl(
        name="Editor", role="WindowControl", children=[identified],
    )
    auto = desktop_from_windows([])

    _, controls = _collect_with_modal_patch(auto, top=win)

    edits = [item for item in controls if item["role"] == "Edit"]
    assert len(edits) == 1
    assert edits[0]["control"] is identified


def test_collect_controls_does_not_join_fresh_wrappers_by_shared_hwnd():
    bounds = MockRect(20, 30, 420, 62)
    inner = MockControl(
        name="Find", role="EditControl", value="needle", bounds=bounds,
        hwnd=8080,
    )
    identified = MockControl(
        name="Find", role="EditControl", automation_id="TextBox",
        value="needle", bounds=bounds, hwnd=8080, children=[inner],
    )
    unrelated_wrapper = MockControl(
        name="Find", role="EditControl", automation_id="TextBox",
        value="needle", bounds=bounds, hwnd=8080,
    )
    inner.GetParentControl = lambda: unrelated_wrapper
    win = MockControl(
        name="Editor", role="WindowControl", children=[identified],
    )
    auto = desktop_from_windows([])

    _, controls = _collect_with_modal_patch(auto, top=win)

    edits = [item for item in controls if item["role"] == "Edit"]
    assert len(edits) == 2


def test_collect_controls_keeps_related_edit_when_value_or_capability_differs():
    bounds = MockRect(20, 30, 420, 62)
    inner = MockControl(
        name="Replace", role="EditControl", value="inner", bounds=bounds,
    )
    identified = MockControl(
        name="Replace", role="EditControl", automation_id="ReplaceTextBox",
        value="outer", bounds=bounds, children=[inner],
    )
    win = MockControl(
        name="Editor", role="WindowControl", children=[identified],
    )
    auto = desktop_from_windows([])

    _, controls = _collect_with_modal_patch(auto, top=win)

    edits = [item for item in controls if item["role"] == "Edit"]
    assert [item["value"] for item in edits] == ["outer", "inner"]


def test_collect_controls_reads_working_value_getter_without_availability_flag():
    editor = MockControl(
        name="Find", role="EditControl", automation_id="TextBox",
        value="Status: draft",
    )
    editor.IsValuePatternAvailable = False
    win = MockControl(
        name="Editor", role="WindowControl", children=[editor],
    )
    auto = desktop_from_windows([])

    _, controls = _collect_with_modal_patch(auto, top=win)

    assert controls[0]["value"] == "Status: draft"


def test_collect_controls_reads_selection_and_toggle_without_availability_flags():
    tab = MockControl(
        name="General", role="TabItemControl", selected=True,
    )
    checkbox = MockControl(
        name="Enabled", role="CheckBoxControl", toggle_state=1,
    )
    tab.IsSelectionItemPatternAvailable = False
    checkbox.IsTogglePatternAvailable = False
    win = MockControl(
        name="Settings", role="WindowControl", children=[tab, checkbox],
    )
    auto = desktop_from_windows([])

    _, controls = _collect_with_modal_patch(auto, top=win)

    assert [(item["name"], item["state"]) for item in controls] == [
        ("General", "selected"),
        ("Enabled", "on"),
    ]


def test_collect_controls_folds_only_proven_readonly_item_columns():
    row = MockControl(
        name="report.txt",
        role="ListItemControl",
        children=[
            MockControl(
                name="Name", role="EditControl",
                automation_id="System.ItemNameDisplay", value="report.txt",
                value_read_only=False, keyboard_focusable=False,
            ),
            MockControl(
                name="Date modified", role="EditControl",
                automation_id="System.DateModified", value="September 7",
                value_read_only=True,
            ),
            MockControl(
                name="Type", role="EditControl",
                automation_id="System.ItemTypeText", value="Text Document",
                value_read_only=True,
            ),
        ],
    )
    rename_row = MockControl(
        name="draft.txt",
        role="ListItemControl",
        children=[
            MockControl(
                name="Name", role="EditControl",
                automation_id="System.ItemNameDisplay", value="draft.txt",
                value_read_only=False, keyboard_focusable=True,
                has_keyboard_focus=True,
            ),
        ],
    )
    win = MockControl(
        name="Picker", role="WindowControl", children=[row, rename_row],
    )
    auto = desktop_from_windows([])

    _, controls = _collect_with_modal_patch(auto, top=win)

    rows = [item for item in controls if item["role"] == "ListItem"]
    assert len(rows) == 2
    assert rows[0]["name"] == "report.txt"
    assert rows[0]["text"] == (
        "Date modified: September 7 · Type: Text Document"
    )
    edits = [item for item in controls if item["role"] == "Edit"]
    assert len(edits) == 2
    assert [item["control"] for item in edits] == [
        row.GetChildren()[0], rename_row.GetChildren()[0],
    ]
    assert [item["value"] for item in edits] == [
        "report.txt", "draft.txt",
    ]


def test_collect_controls_respects_max_controls():
    children = [{"role": "Button", "name": f"B{i}"} for i in range(20)]
    auto, win = window_tree({"name": "App", "role": "WindowControl", "children": children})
    _, controls = _collect_with_modal_patch(auto, top=win, max_controls=5)
    assert len(controls) == 5


# ---- window targeting & focus matching ----------------------------------------


def test_choose_window_picks_largest_real_window():
    cands = [
        {"name": "VARIANT-1", "ctype": "WindowControl", "classname": "", "offscreen": False,
         "pid": dconst.UI_PID or 99999, "area": 500000},
        {"name": "Notepad", "ctype": "WindowControl", "classname": "", "offscreen": False,
         "pid": 2000, "area": 400000},
        {"name": "Calculator", "ctype": "WindowControl", "classname": "", "offscreen": False,
         "pid": 3000, "area": 100000},
    ]
    pick = dwindow.choose_window(cands)
    assert pick["name"] == "Notepad"


def test_find_window_case_insensitive_substring():
    notepad = node_from_spec({"name": "Untitled - Notepad", "role": "WindowControl",
                              "bounds": [0, 0, 600, 400]})
    calc = node_from_spec({"name": "Calculator", "role": "WindowControl",
                           "bounds": [0, 0, 300, 400]})
    auto = desktop_from_windows([
        {"name": "Untitled - Notepad", "role": "WindowControl", "bounds": [0, 0, 600, 400]},
        {"name": "Calculator", "role": "WindowControl", "bounds": [0, 0, 300, 400]},
    ])
    pick, titles = dtarget.find_window(dc._control_context(), auto, "notepad")
    assert pick is not None
    assert "Notepad" in pick["name"]
    assert "Calculator" in titles


def test_find_window_skips_variant1_overlay():
    auto = desktop_from_windows([
        {"name": "VARIANT-1", "role": "WindowControl", "pid": dconst.UI_PID or 99999,
         "bounds": [0, 0, 800, 600]},
        {"name": "Real App", "role": "WindowControl", "bounds": [0, 0, 400, 300]},
    ])
    pick, titles = dtarget.find_window(dc._control_context(), auto, "variant1")
    assert pick is None
    assert "Real App" in titles


def test_target_top_uses_foreground_when_not_skippable():
    fg = node_from_spec({"name": "Foreground App", "role": "WindowControl", "pid": 2000})
    bg = node_from_spec({"name": "Background", "role": "WindowControl", "pid": 3000})
    auto = desktop_from_windows([
        {"name": "Background", "role": "WindowControl", "pid": 3000},
    ])
    auto._foreground = fg
    top = dwindow.target_top(auto)
    assert top.Name == "Foreground App"


def test_resolve_target_uses_locked_window_when_alive():
    locked = MockControl(name="Locked App", role="WindowControl", hwnd=42)
    other = MockControl(name="Other", role="WindowControl", hwnd=43)
    auto = desktop_from_windows([{"name": "Other", "role": "WindowControl"}])
    auto._foreground = other
    dtarget.set_target(
        ds.current_desktop_session(), locked, title="Locked App", query="locked",
    )
    with patch("desktop.modal_detect.scan_modal_uia", return_value=None):
        resolved = dtarget.resolve_target(dc._control_context(), auto)
    assert resolved is locked


def test_resolve_target_records_focus_loss_when_handle_dead():
    dead = MockControl(name="Gone App", role="WindowControl", alive=False)
    fg = node_from_spec({"name": "Foreground", "role": "WindowControl"})
    auto = desktop_from_windows([{"name": "Foreground", "role": "WindowControl"}])
    auto._foreground = fg
    dtarget.set_target(
        ds.current_desktop_session(), dead, title="Gone App", query="gone",
    )
    with patch("desktop.modal_detect.scan_modal_uia", return_value=None):
        resolved = dtarget.resolve_target(dc._control_context(), auto)
    assert resolved.Name == "Foreground"
    pending = ds.current_desktop_session().pending_focus_loss
    assert pending is not None
    assert "Gone App" in pending.get("last_title", "")


def test_locked_target_alive_reasons():
    alive_ctrl = MockControl(alive=True)
    dead_ctrl = MockControl(alive=False)
    assert dtarget.locked_target_alive(alive_ctrl) == (True, "")
    assert dtarget.locked_target_alive(dead_ctrl) == (False, "window_closed")
    assert dtarget.locked_target_alive(None) == (False, "no_handle")


# ---- _ensure_live handle recovery ---------------------------------------------


def test_ensure_live_refreshes_stale_handle_by_key():
    btn_spec = {"role": "Button", "name": "Submit", "automation_id": "submit"}
    auto, win = window_tree({
        "name": "Form",
        "role": "WindowControl",
        "children": [btn_spec],
    })
    with patch("desktop.modal_detect.scan_modal_uia", return_value=None):
        _, controls = dtree.collect_controls(dc._control_context(), auto, top=win)
    rec = dict(controls[0])
    stale = rec["control"]
    stale.set_alive(False)

    fresh_auto, fresh_win = window_tree({
        "name": "Form",
        "role": "WindowControl",
        "children": [btn_spec],
    })
    with patch("desktop.modal_detect.scan_modal_uia", return_value=None):
        live = dactions._ensure_live(dc._control_context(), fresh_auto, rec)

    assert live is not None
    assert live.Exists(0, 0) is True
    assert rec["control"] is live


def test_ensure_live_stops_when_stale_handle_cannot_be_re_resolved():
    session = ds.DesktopSessionState(session_id="desktop_stale_missing")
    session.snapshot_title = "Form"
    ctx = dcontext.DesktopControlContext(session)
    stale = MockControl(name="Submit", role="Button", alive=False)
    rec = {
        "id": 9,
        "key": "aid:submit#1",
        "role": "Button",
        "name": "Submit",
        "control": stale,
    }
    auto, win = window_tree({
        "name": "Form",
        "role": "WindowControl",
        "children": [{"role": "Button", "name": "Different", "automation_id": "other"}],
    })
    session.target_window = win

    with patch("desktop.modal_detect.scan_modal_uia", return_value=None), \
            pytest.raises(tools.ToolError, match="input was not sent") as exc:
        dactions._ensure_live(ctx, auto, rec)

    assert derr.parse_error_tag(str(exc.value)) == derr.DesktopErrorType.STALE_HANDLE


# ---- bounded UIA formatting helpers ------------------------------------------


def test_sig_includes_selection_state():
    off = {"role": "TabItem", "name": "A", "value": "", "state": "unselected", "offscreen": False}
    on = {"role": "TabItem", "name": "A", "value": "", "state": "selected", "offscreen": False}
    ctx = dc._control_context()
    assert dtree.sig(ctx, off) != dtree.sig(ctx, on)


def test_diff_lines_stable_id_semantics():
    ctx = dc._control_context()
    prior = {1: dtree.sig(ctx, {"role": "Button", "name": "OK", "value": "", "state": "", "offscreen": False})}
    controls = [
        {"id": 1, "role": "Button", "name": "OK", "value": "clicked", "state": "", "offscreen": False},
        {"id": 2, "role": "ListItem", "name": "New", "value": "", "state": "", "offscreen": False},
    ]
    lines, total = dtree.diff_lines(ctx, prior, controls)
    assert total == 2
    assert any(line.startswith("~") for line in lines)
    assert any(line.startswith("+") for line in lines)


def test_format_controls_prompt_subset_hides_overflow():
    controls = [
        {"id": i, "role": "Button", "name": f"B{i}", "value": "", "state": "", "offscreen": False}
        for i in range(1, 30)
    ]
    text = dc._control_context().format_controls(controls, "Big App", cap=5)
    assert "(+24 more actionable controls omitted.)" in text


# ---- error classification hooks (desktop_control integration) -----------------


def test_focus_loss_queues_typed_desktop_error():
    fg = node_from_spec({"name": "Fallback", "role": "WindowControl"})
    auto = desktop_from_windows([{"name": "Fallback", "role": "WindowControl"}])
    auto._foreground = fg
    dead = MockControl(name="Lost", role="WindowControl", alive=False)
    dtarget.set_target(ds.current_desktop_session(), dead, title="Lost", query="lost")
    dtarget.resolve_target(dc._control_context(), auto)
    errors = ds.current_desktop_session().pending_desktop_errors
    assert errors
    err = errors[-1]
    assert err.error_type in (derr.DesktopErrorType.WINDOW_CLOSED, derr.DesktopErrorType.STALE_HANDLE)


def test_role_strips_control_suffix():
    ctx = dc._control_context()
    assert ctx._role("ButtonControl") == "Button"
    assert ctx._role("Edit") == "Edit"


def test_focused_type_sends_literal_text_directly(monkeypatch):
    from types import SimpleNamespace
    sent = []
    def send(*events):
        sent.extend(events)
        return len(events)
    auto = SimpleNamespace(KeyboardInput=lambda vk, scan, flags: (vk, scan, flags), SendInput=send)
    monkeypatch.setattr(dinput, '_send_input_batch', lambda events: send(*events))
    text = 'ALPHA\r\nQuantity: 7\nStatus: approved\nZażółć 東京 🙂 {Ctrl}\tend'
    receipt = dinput._type_text_direct(auto, text)
    units = bytearray()
    for down, up in zip(sent[::2], sent[1::2]):
        assert up == (down[0], down[1], down[2] | 2)
        code = {13:10, 9:9}.get(down[0], down[1])
        units.extend(code.to_bytes(2, 'little'))
    assert units.decode('utf-16-le') == text.replace('\r\n', '\n')
    assert receipt['typed_characters'] == len(text)
    assert dinput._type_text_direct(auto, '')['input_events'] == 0


def test_partial_literal_input_is_not_replayed(monkeypatch):
    from types import SimpleNamespace
    import pytest
    calls = []
    def send(*events):
        calls.append(events)
        return len(events) if len(calls) == 1 else 0
    auto = SimpleNamespace(KeyboardInput=lambda *v: v, SendInput=send)
    monkeypatch.setattr(dinput, '_send_input_batch', lambda events: send(*events))
    with pytest.raises(Exception, match='partially delivered'):
        dinput._type_text_direct(auto, 'X' * 300)
    assert len(calls) == 2


def test_repeated_desktop_results_do_not_create_a_synthetic_stop_state():
    session = ds.DesktopSessionState(session_id="desktop_repeated_results")
    ctx = dcontext.DesktopControlContext(session)

    for _ in range(10):
        ctx._note_result(False)

    assert not hasattr(session, "action_failure_count")
