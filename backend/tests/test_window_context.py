"""Unit tests for window-context stack ownership and ordering."""

from __future__ import annotations

import desktop.window_context as wc


def test_push_pop_restore_previous():
    stack = wc.WindowContextStack()
    first = wc.WindowContext(hwnd=1, title="Notepad", query="Notepad")
    second = wc.WindowContext(hwnd=2, title="Chrome", query="Chrome")
    stack.push(first, ctrl="ctrl_a")
    stack.push(second, ctrl="ctrl_b")
    assert stack.depth() == 2
    assert stack.get_current().title == "Chrome"

    popped, current = stack.restore_previous()
    assert popped is not None and popped.title == "Chrome"
    assert current is not None and current.title == "Notepad"
    assert stack.depth() == 1
    assert stack.get_current_control() == "ctrl_a"


def test_replace_single_window_mode():
    stack = wc.WindowContextStack()
    stack.push(wc.WindowContext(title="A", query="A"), ctrl="a")
    stack.push(wc.WindowContext(title="B", query="B"), ctrl="b")
    stack.replace(wc.WindowContext(title="C", query="C"), ctrl="c")
    assert stack.depth() == 1
    assert stack.get_current().title == "C"


def test_switch_to_reorders_top():
    stack = wc.WindowContextStack()
    stack.push(wc.WindowContext(title="Bottom", query="b"), ctrl=0)
    stack.push(wc.WindowContext(title="Middle", query="m"), ctrl=1)
    stack.push(wc.WindowContext(title="Top", query="t"), ctrl=2)
    selected = stack.switch_to(0)
    assert selected is not None and selected.title == "Bottom"
    assert stack.get_current().title == "Bottom"
    assert [row.title for row in stack.get_stack()] == ["Middle", "Top", "Bottom"]


def test_format_stack_marks_current():
    stack = wc.WindowContextStack()
    stack.push(wc.WindowContext(title="App1", query="App1"))
    stack.push(wc.WindowContext(title="App2", query="App2"))
    text = stack.format_stack()
    assert "App1" in text
    assert "App2 (current)" in text


def test_pushed_activity_fields_include_stack():
    context = wc.WindowContext(title="Discord", query="Discord", hwnd=99)
    fields = wc.pushed_activity_fields(
        context,
        depth=2,
        stack_summary="Notepad -> Discord (current)",
    )
    assert fields["depth"] == 2
    assert "Discord" in fields["text"]
    assert "stack:" in fields["text"].lower() or "Notepad" in fields["stack"]
