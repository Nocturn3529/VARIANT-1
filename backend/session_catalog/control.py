"""Durable emergency controls for the single VARIANT-1 runtime."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any

from core_invariants import canonical_json
from .profiles import ACTION_SURFACE


class ControlPlane:
    """Durable kernel-boot and operator mutation kill switches."""

    def __init__(self, database_path: str, initial: dict[str, Any] | None = None):
        self.path = os.path.abspath(database_path)
        self._lock = threading.RLock()
        cfg = dict(initial or {})
        defaults = {
            "stop_new_kernels": bool(cfg.get("stop_new_kernels", False)),
            "freeze_mutation": bool(cfg.get("freeze_mutation", False)),
        }
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_control (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    state_json TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO session_control(singleton, state_json, revision, updated_at) "
                "VALUES (1, ?, 1, ?)",
                (self._json(defaults), time.time()),
            )
            row = conn.execute(
                "SELECT state_json FROM session_control WHERE singleton=1"
            ).fetchone()
            existing = json.loads(row["state_json"]) if row is not None else {}
            normalized = {
                key: bool(existing.get(key, value))
                for key, value in defaults.items()
            }
            if row is not None and existing != normalized:
                conn.execute(
                    "UPDATE session_control SET state_json=?, revision=revision+1, "
                    "updated_at=? WHERE singleton=1",
                    (self._json(normalized), time.time()),
                )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @staticmethod
    def _json(value: dict[str, Any]) -> str:
        return canonical_json(value)

    def snapshot(self) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT state_json, revision, updated_at FROM session_control WHERE singleton=1"
            ).fetchone()
        if row is None:
            raise RuntimeError("session control row is absent")
        state = json.loads(row["state_json"])
        return {
            "schema": "variant1.astb.controls.v2",
            **state,
            "revision": int(row["revision"]),
            "updated_at": float(row["updated_at"]),
        }

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        allowed = {"stop_new_kernels", "freeze_mutation"}
        unknown = sorted(set(patch) - allowed)
        if unknown:
            raise ValueError(f"unknown session control(s): {', '.join(unknown)}")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state_json FROM session_control WHERE singleton=1"
            ).fetchone()
            if row is None:
                raise RuntimeError("session control row is absent")
            state = json.loads(row["state_json"])
            for key in ("stop_new_kernels", "freeze_mutation"):
                if key in patch:
                    if not isinstance(patch[key], bool):
                        raise ValueError(f"{key} must be boolean")
                    state[key] = patch[key]
            conn.execute(
                "UPDATE session_control SET state_json=?, revision=revision+1, updated_at=? "
                "WHERE singleton=1",
                (self._json(state), time.time()),
            )
            conn.commit()
        return self.snapshot()

    def profile_for_new_chat(self, chat_id: str) -> str:
        del chat_id
        return ACTION_SURFACE

    def new_kernel_allowed(self) -> bool:
        return not bool(self.snapshot()["stop_new_kernels"])

    def mutation_allowed(self) -> bool:
        return not bool(self.snapshot()["freeze_mutation"])
