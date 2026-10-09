"""The model note for a browser tab a user mentioned, and its transcript chip.

The note goes to the model with the user's message. The durable user row
keeps only a chip (tab id, title, URL), recovered from the note the same way
file attachment chips are, so history can show the mention after a reload.
"""

from __future__ import annotations

import re

MAX_TAB_MENTIONS = 8
_TAB_ID = re.compile(r"^[A-Za-z0-9_.-]{1,200}$")
_NOTE = re.compile(
    r"^\[Mentioned browser tab ([A-Za-z0-9_.-]{1,200})( is unavailable)?: (.*)\]\nURL: (.*)$",
    re.MULTILINE,
)


def clean_tab_id(value: object) -> str:
    text = str(value or "").strip()
    return text if _TAB_ID.match(text) else ""


def _one_line(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def tab_mention_note(tab_id: str, title: str, url: str, *, available: bool) -> str:
    head = f"[Mentioned browser tab {tab_id}{'' if available else ' is unavailable'}: {_one_line(title, 300)}]"
    body = (
        "It is open in this chat's built-in browser. Use that exact page from the "
        "browser session's pages(); do not open a new tab for it."
        if available else
        "It was closed or changed after the user mentioned it. Do not use or open a "
        "different tab in its place; tell the user it is unavailable."
    )
    return f"{head}\nURL: {_one_line(url, 2000)}\n{body}"


def tab_mention_chips(text: str) -> list[dict[str, str]]:
    """Transcript chips for the tab notes in a model suffix, in order."""

    chips: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in _NOTE.finditer(str(text or "")):
        tab_id = match.group(1)
        if tab_id in seen:
            continue
        seen.add(tab_id)
        title, url = match.group(3).strip(), match.group(4).strip()
        chips.append({
            "kind": "browser_tab", "name": title or url or tab_id,
            "tab_id": tab_id, "title": title, "url": url,
        })
    return chips[:MAX_TAB_MENTIONS]


__all__ = ["MAX_TAB_MENTIONS", "clean_tab_id", "tab_mention_chips", "tab_mention_note"]
