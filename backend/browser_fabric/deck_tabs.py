"""Deck browser tabs and their runs: run-end cleanup and fail-closed mentions.

Tabs the model opens belong to the run that opened them. When the run ends,
the Deck closes the unmarked ones (they stay in its reopen history). A tab
the user mentions in a message resolves only to that exact tab; if it closed
or changed, the model is told it is unavailable and never given another tab.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Mapping

from background_tasks import OwnedTaskSet

_LOG = logging.getLogger(__name__)
_CLEANUP_TASKS = OwnedTaskSet()
MAX_TAB_REFERENCES = 8

Request = Callable[..., Awaitable[dict[str, Any]]]


def _request() -> Request:
    from browser_fabric.interactive import request_host

    return request_host


async def cleanup_run_tabs(owner_chat_id: str, run_id: str, *, request: Request | None = None) -> None:
    """Ask the Deck to close this run's unmarked agent tabs.

    A closed or missing Deck is fine: it reconciles against the active runs
    it receives when it registers again.
    """

    if not owner_chat_id or not run_id:
        return
    try:
        await (request or _request())(
            {"action": "cleanup_run", "owner_chat_id": owner_chat_id, "run_id": run_id},
            timeout=10.0,
        )
    except Exception as exc:
        _LOG.debug("Deck tab cleanup for run %s deferred: %s", run_id, type(exc).__name__)


def schedule_run_cleanup(owner_chat_id: str, run_id: str) -> None:
    """Run-end listener entry point; must be called on the event loop."""

    _CLEANUP_TASKS.spawn(
        cleanup_run_tabs(owner_chat_id, run_id), name=f"deck-tab-cleanup:{run_id[:24]}",
    )


def _clean_reference(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, Mapping) or raw.get("kind") != "browser_tab":
        return None
    tab_id = str(raw.get("tab_id") or "").strip()[:200]
    if not tab_id:
        return None
    return {
        "tab_id": tab_id,
        "owner_chat_id": str(raw.get("owner_chat_id") or "").strip()[:200],
        "title": str(raw.get("title") or "")[:300],
        "url": str(raw.get("url") or "")[:2000],
    }


async def resolve_tab_references(
    references: Any, owner_chat_id: str, *, request: Request | None = None,
) -> str:
    """Model-facing notes for the browser tabs a user mentioned.

    Each mention resolves only to the exact tab the user accepted: same id,
    same owner chat, same title and URL. Anything else is reported as
    unavailable, so the model never substitutes another tab.
    """

    rows = [
        row for row in (
            _clean_reference(item) for item in (references if isinstance(references, list) else [])
        ) if row is not None
    ][:MAX_TAB_REFERENCES]
    if not rows:
        return ""
    live: dict[str, Mapping[str, Any]] = {}
    try:
        result = await (request or _request())(
            {"action": "tabs", "owner_chat_id": owner_chat_id}, timeout=5.0,
        )
        for tab in (result or {}).get("tabs") or ():
            if isinstance(tab, Mapping):
                live[str(tab.get("id") or tab.get("tab_id") or "")] = tab
    except Exception:
        live = {}
    notes = []
    for row in rows:
        tab = live.get(row["tab_id"])
        same = bool(
            tab is not None
            and row["owner_chat_id"] in {"", owner_chat_id}
            and str(tab.get("title") or "") == row["title"]
            and str(tab.get("url") or "") == row["url"]
        )
        label = f'"{row["title"] or row["url"]}" ({row["url"]})'
        if same:
            notes.append(
                "[Browser tab the user mentioned]\n"
                f"Title: {row['title']}\nURL: {row['url']}\n"
                "It is open in this chat's built-in browser. Use that exact page from the "
                "browser session's pages(); do not open a new tab for it."
            )
        else:
            notes.append(
                "[Browser tab the user mentioned is unavailable]\n"
                f"The tab {label} was closed or changed after it was mentioned. Do not use "
                "or open a different tab in its place; tell the user it is unavailable."
            )
    return "\n\n".join(notes)


__all__ = [
    "MAX_TAB_REFERENCES",
    "cleanup_run_tabs",
    "resolve_tab_references",
    "schedule_run_cleanup",
]
