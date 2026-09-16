"""Strict parser for internal JSON-only model calls.

User-facing assistant turns do not pass through this module. The agent loop
consumes provider text and native tool calls as typed values.
"""

from __future__ import annotations

import json


def parse_object(raw: str):
    """Return a JSON object only when the entire response is valid JSON.

    A single Markdown JSON fence is accepted because several internal prompts
    explicitly allow it. Prose scanning, suffix repair, and action extraction
    are intentionally absent.
    """
    text = str(raw or "").strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    elif text.startswith("```") and text.endswith("```"):
        text = text[3:-3].strip()
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None
