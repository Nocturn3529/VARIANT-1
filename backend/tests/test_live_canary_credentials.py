"""Credential-state handoff for isolated live-evaluation configs."""

from __future__ import annotations

import json
from pathlib import Path
import sys


EVAL_ROOT = Path(__file__).resolve().parents[2] / "experiments" / "live-canary"
if str(EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT))

from run_canary import sync_newer_xai_oauth  # noqa: E402


def _config(expires_at: int, marker: str) -> dict:
    return {
        "mode": "local",
        "unrelated": {"preserve": True},
        "cloud": {
            "provider": "xai",
            "oauth": {
                "xai": {
                    "access_token": f"dpapi:access-{marker}",
                    "refresh_token": f"dpapi:refresh-{marker}",
                    "expires_at": expires_at,
                    "auth_flow": "pkce",
                },
            },
        },
    }


def test_newer_rotated_xai_record_is_copied_without_other_settings(tmp_path):
    active = tmp_path / "active.json"
    candidate = tmp_path / "candidate.json"
    active.write_text(json.dumps(_config(100, "old")), encoding="utf-8")
    candidate.write_text(json.dumps(_config(200, "new")), encoding="utf-8")

    assert sync_newer_xai_oauth(
        candidate, active_config_path=active,
    ) is True

    saved = json.loads(active.read_text(encoding="utf-8"))
    assert saved["mode"] == "local"
    assert saved["unrelated"] == {"preserve": True}
    assert saved["cloud"]["oauth"]["xai"] == (
        _config(200, "new")["cloud"]["oauth"]["xai"]
    )


def test_older_or_incomplete_xai_record_cannot_replace_active(tmp_path):
    active = tmp_path / "active.json"
    older = tmp_path / "older.json"
    incomplete = tmp_path / "incomplete.json"
    active.write_text(json.dumps(_config(200, "active")), encoding="utf-8")
    older.write_text(json.dumps(_config(100, "old")), encoding="utf-8")
    missing = _config(300, "missing")
    missing["cloud"]["oauth"]["xai"]["refresh_token"] = ""
    incomplete.write_text(json.dumps(missing), encoding="utf-8")

    assert sync_newer_xai_oauth(older, active_config_path=active) is False
    assert sync_newer_xai_oauth(incomplete, active_config_path=active) is False
    saved = json.loads(active.read_text(encoding="utf-8"))
    assert saved["cloud"]["oauth"]["xai"]["expires_at"] == 200
