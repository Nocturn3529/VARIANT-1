"""Durable store construction for the composition root.

Keeps path math and ``*Store(...)`` constructors out of ``server.py``.
``AppHost`` owns the returned durable stores; this module only constructs them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from automation import history as automation_history
from automation import scheduler as agent_scheduler
from automation import store as automations
from speech import local_stt as voice


@dataclass(frozen=True)
class DurableStores:
    """Snapshot of path-backed stores built at import time."""

    sessions: Any
    scheduler: Any
    automation_history: Any
    automations: Any
    voice: Any


def build_durable_stores(
    *,
    app_root: str,
    data_dir: str,
    config_dir: str,
    router_cfg: dict,
    session_service: Any = None,
) -> DurableStores:
    """Construct path-backed hub stores (no tool registration side effects)."""
    if session_service is None:
        raise RuntimeError("SQL Conversation session service is required")
    sessions = session_service
    scheduler = agent_scheduler.LLMScheduler()
    auto_hist = automation_history.AutomationHistoryStore(
        os.path.join(data_dir, "data", "automation_history", "runs.json"))
    automations_path = (
        os.environ.get("VARIANT1_AUTOMATIONS")
        or os.path.join(config_dir, "automations.json")
    )
    automation_store = automations.AutomationStore(automations_path)
    whisper = voice.WhisperServer(
        router_cfg.get("voice", {}) or {}, app_root, data_root=data_dir,
    )
    return DurableStores(
        sessions=sessions,
        scheduler=scheduler,
        automation_history=auto_hist,
        automations=automation_store,
        voice=whisper,
    )
