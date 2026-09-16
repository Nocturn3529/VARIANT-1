"""Shared desktop-control constants."""

from __future__ import annotations

import os

ACTIONABLE_ROLES = {
    "Button", "Edit", "ListItem", "MenuItem", "Hyperlink", "CheckBox",
    "RadioButton", "ComboBox", "TabItem", "TreeItem", "SplitButton",
    "Slider", "Spinner", "Menu", "Document",
}
READABLE_ROLES = {"Text", "StatusBar"}
MAX_READABLE_CONTROLS = 40
MAX_CONTROLS = 400
MAX_DEPTH = 50

_SHELL_NAMES = {"Program Manager", "Taskbar", ""}
_OVERLAY_CLASSES = {"CEF-OSC-WIDGET"}
_OVERLAY_NAME_SUBSTR = (
    "nvidia geforce overlay",
    "geforce overlay",
    "discord overlay",
    "steam overlay",
)

_ROLE_WEIGHT = {
    "Canvas": 9,   # drawing surface — must survive prompt caps for drag targeting
    "Button": 8, "Edit": 8, "ComboBox": 7, "ListItem": 6, "MenuItem": 6,
    "TabItem": 5, "Hyperlink": 5, "CheckBox": 5, "RadioButton": 5, "SplitButton": 5,
    "TreeItem": 4, "Slider": 4, "Spinner": 3, "Menu": 2, "Document": 1,
    "Text": 0, "StatusBar": 0,
}

_LABEL_DERIVE_ROLES = {
    "ListItem", "TreeItem", "MenuItem", "Button", "Hyperlink", "TabItem",
    "CheckBox", "RadioButton",
}

try:
    UI_PID = int(os.environ.get("VARIANT1_UI_PID", "0") or 0)
except (ValueError, TypeError):
    UI_PID = 0
