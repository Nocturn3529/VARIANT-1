"""Frozen exports stay bounded and never publish unverified partial evidence."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from context_export import ContextExportCancelled, export_view


class Unavailable(RuntimeError):
    code = "context_source_unavailable"


class Evidence:
    def __init__(self, text, failure=None):
        self.text, self.failure = text, failure
        self.revoked = False

    def status(self, chat, view):
        assert (chat, view) == ("chat", "view")
        return {"view_id": view, "coverage": "fixture retained evidence"}

    def _view(self, chat, view):
        self.status(chat, view)
        if self.revoked:
            raise RuntimeError("View deleted")

    def iter_records(self, chat, view, page_size):
        self.status(chat, view)
        assert page_size <= 100
        yield {"source_id": "cell:one", "kind": "cell"}

    def iter_expansion(self, chat, view, source, *, part, chunk_chars):
        assert chunk_chars <= 16384
        if part == "output":
            raise Unavailable("no recorded output")
        for offset in range(0, len(self.text), chunk_chars):
            yield {"text": self.text[offset:offset + chunk_chars], "offset": offset,
                   "eof": offset + chunk_chars >= len(self.text)}
        if self.failure:
            raise self.failure


def test_jsonl_export_preserves_chunked_unicode_and_reports_omissions(tmp_path):
    text = ("Straße € 🧠\n" * 12000) + "final correction"
    target = tmp_path / "context.jsonl"
    receipt = export_view(Evidence(text), "chat", "view", path=str(target))
    rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    for part in ("source", "result"):
        assert "".join(row["text"] for row in rows if row["type"] == "content" and row["part"] == part) == text
    assert rows[-1]["type"] == "complete" and receipt["omission_count"] == 1
    assert receipt["source_count"] == 1 and receipt["part_count"] == 2
    assert receipt["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert not list(tmp_path.glob(".variant1-context-*"))


def test_late_integrity_failure_preserves_existing_destination(tmp_path):
    target = tmp_path / "context.jsonl"
    target.write_bytes(b"existing export")
    with pytest.raises(RuntimeError, match="integrity"):
        export_view(Evidence("partial evidence", RuntimeError("integrity failed at EOF")), "chat", "view",
                    path=str(target), overwrite=True)
    assert target.read_bytes() == b"existing export"
    assert not list(tmp_path.glob(".variant1-context-*"))


def test_cancellation_and_revocation_do_not_publish_partial_file(tmp_path):
    target = tmp_path / "context.jsonl"
    evidence = Evidence("records")
    calls = 0
    def stop():
        nonlocal calls
        calls += 1
        return calls > 3
    with pytest.raises(ContextExportCancelled):
        export_view(evidence, "chat", "view", path=str(target), cancelled=stop)
    assert not target.exists() and not list(tmp_path.glob(".variant1-context-*"))
    evidence.revoked = True
    with pytest.raises(RuntimeError, match="deleted"):
        export_view(evidence, "chat", "view", path=str(target))
    assert not target.exists() and not list(tmp_path.glob(".variant1-context-*"))


def test_exclusive_publish_rejects_file_created_during_export(tmp_path):
    target = tmp_path / "context.jsonl"
    evidence = Evidence("records")
    def validate(chat, view):
        target.write_bytes(b"concurrent file")
    evidence._view = validate
    with pytest.raises(FileExistsError):
        export_view(evidence, "chat", "view", path=str(target))
    assert target.read_bytes() == b"concurrent file"
    assert not list(tmp_path.glob(".variant1-context-*"))


def test_markdown_preserves_embedded_fences_and_chunk_boundaries(tmp_path):
    text = "```\n" + "a" * 20000 + "\n# recorded heading\n~~~\n"
    target = tmp_path / "context.md"
    export_view(Evidence(text), "chat", "view", path=str(target), format="markdown")
    assert "    " + "a" * 20000 + "\n" in target.read_text(encoding="utf-8")
    assert "    # recorded heading\n" in target.read_text(encoding="utf-8")
