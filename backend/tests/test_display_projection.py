import asyncio

import pytest

from observability import display_projection as display
from observability import activity
from chat_sessions.service import _compact_steps


@pytest.fixture(autouse=True)
def no_managed_values(monkeypatch):
    monkeypatch.setattr(display, "_resolver", None)


def test_managed_and_structural_credentials_are_projected_without_mutating_inputs(monkeypatch):
    monkeypatch.setattr(display, "_resolver", lambda: [("managed", "opaque-managed-value")])
    original = {"code": 'print("opaque-managed-value")', "nested": {"password": "plain"},
                "text": 'Bearer abcd1234abcdefgh5678 and "password": "unmanaged-value"'}
    projected = display.safe_display(original)
    assert "opaque-managed-value" not in projected["code"]
    assert projected["nested"]["password"] == display.REDACTED
    assert "abcd1234abcdefgh5678" not in projected["text"]
    assert "unmanaged-value" not in projected["text"]
    assert original["nested"]["password"] == "plain"
    assert "opaque-managed-value" in original["code"]


def test_display_sanitation_fails_closed_on_resolver_failure(monkeypatch):
    def broken():
        raise RuntimeError("sensitive diagnostics must not be echoed")
    monkeypatch.setattr(display, "_resolver", broken)
    assert display.safe_display_fields({"text": "private", "status": "ok"}) == {
        "text": display.UNAVAILABLE, "status": "ok"}


def test_activity_and_persistence_sanitize_before_clipping(monkeypatch):
    secret = "xai-" + "SyntheticCredential1234567890"
    captured = []
    class Hub:
        async def broadcast(self, message):
            captured.append(message)
    monkeypatch.setattr(activity, "HUB", Hub())
    asyncio.run(activity.emit_activity("tool:result", tool="audit", call_id="audit", text=secret))
    assert secret not in captured[0]["text"]
    preview = activity.args_preview({"password": "private", "code": secret})
    assert "private" not in preview and secret not in preview
    saved = _compact_steps([{"label": secret, "kind": "tool", "status": "cancelled",
        "args_preview": preview, "result_preview": secret,
        "evidence": [{"kind": "url", "value": 'https://example.test/?token=' + 'a' * 20}]}])
    assert secret not in str(saved)
    assert saved[0]["status"] == "cancelled"
    assert 'a' * 20 not in saved[0]["evidence"][0]["value"]


@pytest.mark.parametrize("status", ["cancelled", "interrupted", "timed_out", "skipped", "degraded", "unknown"])
def test_saved_outcome_preserves_non_success(status):
    assert _compact_steps([{"label": "Action", "status": status}])[0]["status"] == status


def test_compaction_keeps_the_terminal_tail_and_accumulates_omissions():
    original = [{"id": str(i), "label": "Action", "status": "error" if i == 59 else "ok"} for i in range(60)]
    saved = _compact_steps(original)
    assert len(saved) == 48 and saved[0]["id"] == "12"
    assert saved[-1]["status"] == "error"
    assert saved[0]["omitted_before"] == 12
    saved.extend({"id": str(i), "label": "Later", "status": "ok"} for i in range(60, 62))
    again = _compact_steps(saved)
    assert again[0]["id"] == "14" and again[0]["omitted_before"] == 14
    long = _compact_steps([{"label": "Result", "result_preview": "X" * 2000}])[0]["result_preview"]
    assert len(long) <= 800 and "2000 characters total" in long
