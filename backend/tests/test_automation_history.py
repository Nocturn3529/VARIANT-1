from __future__ import annotations

import json

from automation import history as automation_history


def _store(tmp_path, max_runs=automation_history.MAX_RUNS):
    return automation_history.AutomationHistoryStore(
        str(tmp_path / "automation_history" / "runs.json"),
        max_runs=max_runs,
    )


def test_add_persists_and_lists_newest_first(tmp_path):
    s = _store(tmp_path)
    s.add("a1", 10, 12, "ok", "done")
    s.add("a2", 20, 22, "error", "failed")

    assert [r["automation_id"] for r in s.list()] == ["a2", "a1"]

    reloaded = _store(tmp_path)
    assert [r["automation_id"] for r in reloaded.list()] == ["a2", "a1"]


def test_filter_by_automation_and_clip_summary(tmp_path):
    s = _store(tmp_path)
    s.add("a1", 1, 2, "ok", "x" * 400)
    s.add("a2", 3, 4, "ok", "other")

    rows = s.list("a1")
    assert len(rows) == 1
    assert rows[0]["automation_id"] == "a1"
    assert len(rows[0]["summary"]) <= automation_history.MAX_SUMMARY_CHARS + 3


def test_ring_cap_prunes_oldest_on_write(tmp_path):
    s = _store(tmp_path, max_runs=3)
    for i in range(5):
        s.add(f"a{i}", i, i + 1, "ok", str(i))

    rows = list(reversed(s.list(limit=10)))
    assert [r["automation_id"] for r in rows] == ["a2", "a3", "a4"]


def test_add_accepts_interrupted_status(tmp_path):
    s = _store(tmp_path)
    row = s.add("a1", 10, 12, "interrupted", "app restarted mid-run")

    assert row["status"] == "interrupted"
    assert s.list()[0]["status"] == "interrupted"


def test_add_coerces_unknown_status_to_error(tmp_path):
    s = _store(tmp_path)
    row = s.add("a1", 10, 12, "bogus", "x")

    assert row["status"] == "error"


def test_missing_or_corrupt_file_degrades_to_empty(tmp_path):
    s = _store(tmp_path)
    assert s.list() == []

    path = tmp_path / "automation_history" / "runs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    corrupt = _store(tmp_path)
    assert corrupt.list() == []
    corrupt.add("a1", 1, 2, "ok", "recovered")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["runs"][0]["automation_id"] == "a1"
