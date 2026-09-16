"""Composition root for VARIANT-1's durable local chat transcript service."""

from __future__ import annotations

import os
from typing import Any

from .repository import ConversationRepository
from .service import ChatSessionService


def build_chat_sessions(
    host: Any | None = None,
    *,
    path: str | None = None,
    data_dir: str | None = None,
) -> ChatSessionService:
    if path and data_dir:
        raise ValueError("pass either path or data_dir, not both")
    if path is None and data_dir is None and host is not None:
        app_root = str(getattr(host, "data_dir", "") or "").strip()
        if app_root:
            data_dir = os.path.join(app_root, "data")
    return ChatSessionService(ConversationRepository(path, data_dir=data_dir))


__all__ = ["ChatSessionService", "build_chat_sessions"]
