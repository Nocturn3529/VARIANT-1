from __future__ import annotations

from types import SimpleNamespace

import pytest

from browser_fabric.preferences import BrowserPreferences
from work_fabric.scope import WorkScope


class PreferenceStore:
    def __init__(self):
        self.rows = {
            "@default": {
                "selection": {"mode": "embedded"},
                "state": {},
                "revision": 0,
            },
        }

    def browser_preference(self, owner):
        row = self.rows.setdefault(
            owner,
            {"selection": None, "state": {}, "revision": 0},
        )
        return {
            "selection": (
                dict(row["selection"])
                if isinstance(row["selection"], dict)
                else None
            ),
            "state": dict(row["state"]),
            "revision": int(row["revision"]),
        }

    def update_browser_preference(
        self, owner, *, selection=None, state=None, expected_revision=None,
    ):
        row = self.rows.setdefault(
            owner,
            {"selection": None, "state": {}, "revision": 0},
        )
        if selection is not None:
            row["selection"] = dict(selection)
        if state is not None:
            row["state"] = dict(state)
        row["revision"] += 1
        return self.browser_preference(owner)


class Fabric:
    def __init__(self):
        self.store = PreferenceStore()
        self._adapters = {"embedded-session": object()}
        self.record = SimpleNamespace(
            session_id="embedded-session",
            kind="embedded",
            metadata={},
            profile_id="embedded",
            scope=WorkScope(chat_id="chat-a"),
        )

    def session(self, session_id):
        assert session_id == self.record.session_id
        return self.record


@pytest.mark.asyncio
async def test_default_embedded_observation_marks_chat_ready_without_override():
    fabric = Fabric()
    preferences = BrowserPreferences(fabric)
    assert fabric.store.browser_preference("chat-a")["selection"] is None

    await preferences.observation(
        "chat-a",
        SimpleNamespace(
            session_id="embedded-session",
            url="https://example.test/",
            elements=[],
        ),
    )

    state = preferences.state("chat-a")
    assert state["selection"] == {"mode": "embedded"}
    assert state["selection_source"] == "default"
    assert state["state"] == "ready"
    assert state["browser_session_id"] == "embedded-session"


@pytest.mark.asyncio
async def test_default_embedded_failure_is_not_silently_discarded():
    fabric = Fabric()
    preferences = BrowserPreferences(fabric)

    await preferences.connection_failed(
        "chat-a",
        fabric.record,
        RuntimeError("embedded connection failed"),
    )

    state = preferences.state("chat-a")
    assert state["selection"] == {"mode": "embedded"}
    assert state["state"] == "connection_failed"
    assert state["message"] == "embedded connection failed"
