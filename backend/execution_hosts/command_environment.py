"""Agent-command defaults and trusted Windows helper locations."""

from __future__ import annotations

import ntpath
import os
from collections.abc import Mapping


def windows_system_executable(name: str) -> str:
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    if not ntpath.isabs(root):
        root = r"C:\Windows"
    if name == "powershell.exe":
        return ntpath.join(root, "System32", "WindowsPowerShell", "v1.0", name)
    if name == "taskkill.exe":
        return ntpath.join(root, "System32", name)
    raise ValueError("unsupported system helper")


def helper_environment() -> dict[str, str]:
    return {**os.environ, "NoDefaultCurrentDirectoryInExePath": "1"}


def noninteractive_environment(delta: Mapping[str, str]) -> dict[str, str]:
    return {
        "NO_COLOR": "1", "TERM": "dumb", "CLICOLOR": "0", "FORCE_COLOR": "0",
        "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "GIT_EDITOR": "true",
        "GIT_SEQUENCE_EDITOR": "true", "GIT_ASKPASS": "true", "SSH_ASKPASS_REQUIRE": "never",
        "EDITOR": "true", "VISUAL": "true", "PAGER": "cat", "DEBIAN_FRONTEND": "noninteractive",
        **{str(key): str(value) for key, value in delta.items()},
    }
