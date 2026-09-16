"""Canonical SQL chat-session fixtures for backend tests."""

from __future__ import annotations

from pathlib import Path

from chat_sessions import build_chat_sessions


def open_sessions(root: str | Path, *, max_messages: int = 1000):
    """Open the application session API on an isolated chat database."""

    directory = Path(root)
    directory.mkdir(parents=True, exist_ok=True)
    service = build_chat_sessions(path=str(directory / "conversations.sqlite3"))
    service.max_messages = max(1, int(max_messages))
    return service


__all__ = ["open_sessions"]
