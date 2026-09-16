"""Lightweight mock UIA layer for deterministic desktop_control tests.

Mimics the subset of uiautomation used by desktop.tree.collect_controls,
_target_top, _find_window, and _ensure_live — no real desktop required.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass
class MockRect:
    left: int = 0
    top: int = 0
    right: int = 100
    bottom: int = 100

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


class MockValuePattern:
    def __init__(self, value: str = "", *, read_only: bool = False):
        self.Value = value
        self.IsReadOnly = bool(read_only)


class MockSelectionItemPattern:
    def __init__(self, selected: bool = False):
        self.IsSelected = selected


class MockTogglePattern:
    def __init__(self, state: int = 0):
        self.ToggleState = state


class MockControl:
    """Minimal UIA control stand-in."""

    def __init__(
        self,
        *,
        name: str = "",
        role: str = "ButtonControl",
        automation_id: str = "",
        value: str = "",
        value_read_only: bool = False,
        offscreen: bool = False,
        bounds: MockRect | None = None,
        selected: bool = False,
        toggle_state: int = 0,
        class_name: str = "",
        pid: int = 1000,
        hwnd: int = 1,
        runtime_id: tuple[int, ...] = (),
        alive: bool = True,
        keyboard_focusable: bool = True,
        has_keyboard_focus: bool = False,
        children: list["MockControl"] | None = None,
        label_children: list["MockControl"] | None = None,
    ):
        self.Name = name
        self.ControlTypeName = role
        self.AutomationId = automation_id
        self.IsOffscreen = offscreen
        self.BoundingRectangle = bounds or MockRect()
        self.ClassName = class_name or "MockClass"
        self.ProcessId = pid
        self.NativeWindowHandle = hwnd
        self._runtime_id = tuple(int(item) for item in runtime_id)
        self._alive = alive
        self._value = value
        self._value_read_only = bool(value_read_only)
        self._selected = selected
        self._toggle_state = toggle_state
        self._children = list(children or [])
        self._label_children = list(label_children or [])
        self._top_level: MockControl | None = None
        self._parent: MockControl | None = None
        self.IsKeyboardFocusable = bool(keyboard_focusable)
        self.HasKeyboardFocus = bool(has_keyboard_focus)
        for child in self._children + self._label_children:
            child._parent = self

    def Exists(self, _max_search=0, _interval=0) -> bool:
        return self._alive

    def set_alive(self, alive: bool):
        self._alive = alive

    def GetChildren(self) -> list[MockControl]:
        return list(self._children)

    def GetTopLevelControl(self) -> "MockControl":
        return self._top_level or self

    def GetParentControl(self) -> "MockControl | None":
        return self._parent

    def GetRuntimeId(self) -> list[int]:
        return list(self._runtime_id)

    def IsValuePatternAvailable(self) -> bool:
        return bool(self._value) or self.ControlTypeName == "EditControl"

    def GetValuePattern(self) -> MockValuePattern:
        return MockValuePattern(self._value, read_only=self._value_read_only)

    def IsSelectionItemPatternAvailable(self) -> bool:
        role = self.ControlTypeName.replace("Control", "")
        return role in ("ListItem", "TabItem", "TreeItem", "RadioButton")

    def GetSelectionItemPattern(self) -> MockSelectionItemPattern:
        return MockSelectionItemPattern(self._selected)

    def IsTogglePatternAvailable(self) -> bool:
        role = self.ControlTypeName.replace("Control", "")
        return role in ("CheckBox", "MenuItem")

    def GetTogglePattern(self) -> MockTogglePattern:
        return MockTogglePattern(self._toggle_state)

    def SetActive(self):
        return None


class MockUIAutomation:
    """Fake uiautomation module object."""

    def __init__(
        self,
        *,
        root_children: list[MockControl] | None = None,
        foreground: MockControl | None = None,
    ):
        self._root_children = list(root_children or [])
        self._foreground = foreground

    def GetRootControl(self) -> MockControl:
        root = MockControl(name="Desktop", role="PaneControl", hwnd=0)
        root._children = self._root_children

        def bind(node: MockControl, *, parent: MockControl, top: MockControl) -> None:
            node._parent = parent
            node._top_level = top
            for child in node._children:
                bind(child, parent=node, top=top)
            for child in node._label_children:
                bind(child, parent=node, top=top)

        for child in self._root_children:
            bind(child, parent=root, top=child)
        return root

    def GetForegroundControl(self) -> MockControl | None:
        if self._foreground is not None:
            return self._foreground
        if self._root_children:
            return self._root_children[0]
        return None

    def WalkControl(
        self,
        root: MockControl,
        *,
        includeTop: bool = False,
        maxDepth: int = 50,
    ) -> Iterator[tuple[MockControl, int]]:
        """DFS walk matching uiautomation.WalkControl yield shape."""

        def _walk(node: MockControl, depth: int):
            if includeTop:
                yield node, depth
                child_depth = depth + 1
            else:
                child_depth = depth
            if child_depth > maxDepth:
                return
            kids = node._label_children if node._label_children else node._children
            for child in kids:
                yield child, child_depth
                if child_depth < maxDepth:
                    yield from _walk(child, child_depth + 1)

        yield from _walk(root, 0)


def node_from_spec(spec: dict) -> MockControl:
    """Build a MockControl tree from a JSON-friendly dict spec."""
    role = spec.get("role") or "ButtonControl"
    if role and not role.endswith("Control"):
        role = role + "Control"
    bounds = None
    if "bounds" in spec:
        b = spec["bounds"]
        bounds = MockRect(b[0], b[1], b[2], b[3])
    children = [node_from_spec(c) for c in spec.get("children") or []]
    label_children = [node_from_spec(c) for c in spec.get("label_children") or []]
    return MockControl(
        name=spec.get("name") or "",
        role=role,
        automation_id=spec.get("automation_id") or spec.get("automationId") or "",
        value=spec.get("value") or "",
        value_read_only=bool(spec.get("value_read_only")),
        offscreen=bool(spec.get("offscreen")),
        bounds=bounds,
        selected=bool(spec.get("selected")),
        toggle_state=int(spec.get("toggle_state") or 0),
        class_name=spec.get("class_name") or "",
        pid=int(spec.get("pid") or 1000),
        hwnd=int(spec.get("hwnd") or 1),
        runtime_id=tuple(int(item) for item in spec.get("runtime_id") or ()),
        alive=spec.get("alive", True),
        keyboard_focusable=spec.get("keyboard_focusable", True),
        has_keyboard_focus=bool(spec.get("has_keyboard_focus")),
        children=children,
        label_children=label_children,
    )


def desktop_from_windows(windows: list[dict]) -> MockUIAutomation:
    """Build a mock desktop from a list of top-level window specs."""
    tops = []
    for i, w in enumerate(windows):
        spec = dict(w)
        spec.setdefault("role", "WindowControl")
        spec.setdefault("hwnd", 100 + i)
        spec.setdefault("bounds", [0, 0, 800, 600])
        tops.append(node_from_spec(spec))
    fg = tops[0] if tops else None
    return MockUIAutomation(root_children=tops, foreground=fg)


def window_tree(spec: dict) -> tuple[MockUIAutomation, MockControl]:
    """Single-window desktop; returns (auto, window_root)."""
    win = node_from_spec(spec)
    auto = MockUIAutomation(root_children=[win], foreground=win)
    return auto, win
