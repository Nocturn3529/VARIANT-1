"""Exact provenance extents for ephemeral host context in user messages."""

from __future__ import annotations

import copy
from typing import Any, Mapping


HOST_CONTEXT_EXTENTS_REVISION = 1
HOST_CONTEXT_PREFIX_KEY = "variant1_host_context_prefix_chars"
HOST_CONTEXT_SUFFIX_KEY = "variant1_host_context_suffix_chars"


def _extent(row: Mapping[str, Any], key: str) -> int:
    if key not in row:
        return 0
    value = row[key]
    if type(value) is not int or value < 0:
        raise ValueError(f"invalid recorded host-context extent: {key}")
    return value


def strip_recorded_host_context(row: Mapping[str, Any]) -> dict[str, Any]:
    """Remove only construction-time host extents from one copied message."""

    clean = copy.deepcopy(dict(row))
    prefix = _extent(clean, HOST_CONTEXT_PREFIX_KEY)
    suffix = _extent(clean, HOST_CONTEXT_SUFFIX_KEY)
    clean.pop(HOST_CONTEXT_PREFIX_KEY, None)
    clean.pop(HOST_CONTEXT_SUFFIX_KEY, None)
    if not prefix and not suffix:
        return clean
    content = clean.get("content")
    if not isinstance(content, str) or prefix + suffix > len(content):
        raise ValueError("invalid recorded host-context extent")
    stop = len(content) - suffix if suffix else len(content)
    clean["content"] = content[prefix:stop]
    return clean


def canonical_user_overlay(
    messages: list[dict],
    canonical_messages: list[dict],
    *,
    extent_revision: int | None,
) -> list[dict]:
    """Restore user content from verified canonical order without marker guessing.

    Extent-aware projections must reduce exactly to canonical user content.  A
    legacy projection may prepend application context to that exact content;
    canonical coverage, exact suffix correspondence, and ordered one-to-one
    matching authorize replacing only the user field.
    Assistant summaries and tool call/result boundaries remain unchanged.
    """

    if extent_revision is not None and (
        type(extent_revision) is not int
        or extent_revision != HOST_CONTEXT_EXTENTS_REVISION
    ):
        raise ValueError("invalid host-context extent revision")
    projected = [strip_recorded_host_context(row) for row in messages]
    # A fully host-authored resume/state row becomes empty after extent removal
    # and has no canonical user counterpart.
    projected = [
        row for row in projected
        if row.get("content") or row.get("tool_calls") or row.get("role") == "tool"
    ]
    projected_users = [row for row in projected if row.get("role") == "user"]
    canonical_users = [
        row for row in canonical_messages
        if row.get("role") == "user" and isinstance(row.get("content"), str)
    ]
    if len(projected_users) != len(canonical_users):
        raise ValueError("projected user history does not match canonical coverage")
    extent_aware = extent_revision == HOST_CONTEXT_EXTENTS_REVISION
    for projected_row, canonical_row in zip(projected_users, canonical_users):
        projected_content = projected_row.get("content")
        canonical_content = canonical_row.get("content")
        if not isinstance(projected_content, str) or not isinstance(canonical_content, str):
            raise ValueError("unsupported canonical user content")
        if extent_aware:
            if projected_content != canonical_content:
                raise ValueError("extent-aware user history differs from canonical coverage")
        elif projected_content != canonical_content:
            if not canonical_content or not projected_content.endswith(canonical_content):
                raise ValueError("legacy user history lacks canonical suffix correspondence")
        projected_row["content"] = canonical_content
    return projected


__all__ = [
    "HOST_CONTEXT_EXTENTS_REVISION",
    "HOST_CONTEXT_PREFIX_KEY",
    "HOST_CONTEXT_SUFFIX_KEY",
    "canonical_user_overlay",
    "strip_recorded_host_context",
]
