"""Deterministic parsing and identifiers for extension skill resources."""

from __future__ import annotations

import re


FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)


def parse_skill(text: str) -> tuple[dict[str, str], str]:
    if text.startswith("\ufeff"):
        text = text[1:]
    match = FRONTMATTER.match(text)
    if match is None:
        return {}, text.strip()
    metadata: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split(":", 1)
        metadata[key.strip().lower()] = value.strip().strip('"').strip("'")
    return metadata, match.group(2).strip()
__all__ = ["parse_skill"]
