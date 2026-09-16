"""MemoryRecord.normalize clamps and repairs fields in place."""

from __future__ import annotations

import pytest

from memory_schema import MAX_CONTENT, MAX_CONTEXT, MAX_TAGS, MemoryRecord


def test_normalize_whitespace_and_truncation():
    rec = MemoryRecord(
        content="  hello   world  " + "x" * 500,
        context="  learned  here  " + "y" * 300,
    )
    rec.normalize()
    expected_content = ("hello world " + "x" * 500)[:MAX_CONTENT]
    expected_context = ("learned here " + "y" * 300)[:MAX_CONTEXT]
    assert rec.content == expected_content
    assert rec.context == expected_context
    assert len(rec.content) <= MAX_CONTENT
    assert len(rec.context) <= MAX_CONTEXT


@pytest.mark.parametrize(
    "raw_type,expected",
    [
        ("PREFERENCE", "preference"),
        ("bogus", "fact"),
        ("", "fact"),
    ],
)
def test_normalize_coerces_unknown_type(raw_type, expected):
    rec = MemoryRecord(content="ok", type=raw_type)
    rec.normalize()
    assert rec.type == expected


@pytest.mark.parametrize(
    "importance,expected",
    [
        (99, 5),
        (0, 1),
        ("bad", 3),
    ],
)
def test_normalize_clamps_importance(importance, expected):
    rec = MemoryRecord(content="ok", importance=importance)
    rec.normalize()
    assert rec.importance == expected


def test_normalize_dedupes_and_limits_tags():
    rec = MemoryRecord(
        content="ok",
        tags=[" Alpha ", "alpha", "B" * 40, "", "z"],
    )
    rec.normalize()
    assert rec.tags == ["alpha", "b" * 32, "z"][:MAX_TAGS]
    assert len(rec.tags) <= MAX_TAGS


def test_from_dict_normalizes_on_load():
    rec = MemoryRecord.from_dict({"content": "  stable fact  ", "type": "UNKNOWN", "importance": 10})
    assert rec.type == "fact"
    assert rec.importance == 5
    assert rec.content == "stable fact"