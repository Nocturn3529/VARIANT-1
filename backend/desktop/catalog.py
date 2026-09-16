"""Single catalog of desktop tool names used across recovery, graph, logging.

Import from here instead of redefining frozensets in nodes / recovery / ledger /
tool_runner / execution locking. Subsets are intentional (not all desktop tools mutate
UI state; not all need compact log lines).
"""

from __future__ import annotations

# Desktop seeds whose driver work is serialized by the capability broker.
DESKTOP_TOOLS = frozenset({
    "computer",
})

# Tools that share the process-wide physical desktop lock.
DESKTOP_SURFACE_TOOLS = DESKTOP_TOOLS

# Compact [desktop] outcome log lines (tool_runner).
DESKTOP_LOG_TOOLS = frozenset({
    "computer",
})


def uses_desktop_surface(name: str) -> bool:
    return str(name or "") in DESKTOP_SURFACE_TOOLS
