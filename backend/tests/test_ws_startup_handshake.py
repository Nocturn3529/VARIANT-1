"""Startup handshake: every typed Deck hydration command must be registered.

``INITIAL_DECK_COMMANDS`` is the frontend source of truth. A missing backend
handler returns unknown_type; a handler exception used to tear down the socket.
Both are regressions pinned here.

Hydration commands:
  chat:sessions, config:get, model:list
Questions hydrate through a chat-scoped request after the viewed chat is known.
"""

from __future__ import annotations

import pathlib
import re

import ws_dispatch

ROOT = pathlib.Path(__file__).resolve().parents[2]
INITIAL_HYDRATION_TS = (
    ROOT / "frontend" / "main-deck" / "src" / "runtime" / "initialHydration.ts"
)

# Canonical list — keep in sync with the typed runtime hydration constant.
ON_OPEN_TYPES = (
    "chat:sessions",
    "config:get",
    "model:list",
)


def test_on_open_types_are_registered():
    missing = [name for name in ON_OPEN_TYPES if name not in ws_dispatch.HANDLERS]
    assert not missing, (
        "Main Deck onOpen sends types with no backend handler: " + ", ".join(missing)
    )


def test_typed_hydration_list_matches_canonical():
    """A typed-runtime hydration change must update the backend contract."""
    src = INITIAL_HYDRATION_TS.read_text(encoding="utf-8")
    m = re.search(
        r'export\s+const\s+INITIAL_DECK_COMMANDS\s*=\s*\[([^\]]+)\]\s*as\s+const',
        src,
        re.MULTILINE,
    )
    assert m, (
        "INITIAL_DECK_COMMANDS not found in initialHydration.ts — "
        "update this contract test if the typed source shape changed"
    )
    found = re.findall(r'["\']([a-z0-9:._-]+)["\']', m.group(1))
    assert found, "typed hydration command list parsed empty"
    assert set(found) == set(ON_OPEN_TYPES), (
        f"typed hydration commands {found} != test list {list(ON_OPEN_TYPES)}. "
        "Update ON_OPEN_TYPES and ensure handlers are registered."
    )


def test_on_open_handlers_are_callable():
    for name in ON_OPEN_TYPES:
        fn = ws_dispatch.HANDLERS[name]
        assert callable(fn), name
