"""Golden / snapshot helpers for desktop perception tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "desktop_control"


def load_golden(name: str) -> dict:
    path = FIXTURES / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_controls(controls: list[dict]) -> list[dict]:
    """Strip live UIA handles for stable golden comparison."""
    out = []
    for c in controls:
        row = {
            "id": c.get("id"),
            "key": c.get("key"),
            "role": c.get("role"),
            "name": c.get("name"),
            "value": c.get("value"),
            "state": c.get("state"),
            "offscreen": c.get("offscreen"),
            "bounds": c.get("bounds"),
        }
        out.append(row)
    return sorted(out, key=lambda r: (r.get("id") or 0, r.get("key") or ""))


def collect_snapshot(title: str, controls: list[dict]) -> dict[str, Any]:
    return {"title": title, "controls": normalize_controls(controls)}


def assert_matches_golden(actual: dict, golden: dict):
    assert actual["title"] == golden["title"]
    assert actual["controls"] == golden["controls"]