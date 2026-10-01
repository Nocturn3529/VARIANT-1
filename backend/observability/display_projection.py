"""Credential-safe human-facing projections; never modifies execution values."""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping

from model_runtime.secret_egress import SECRET_TEXT_PATTERNS

_resolver: Callable | None = None
_SECRET_FIELD = re.compile(
    r"^(?:api[_-]?key|password|passwd|client[_-]?secret|access[_-]?token|"
    r"refresh[_-]?token|authorization|proxy[_-]?authorization|cookie|secret)$", re.I
)
_DISPLAY_FIELDS = frozenset({
    "args_preview", "argsPreview", "result_preview", "resultPreview", "text",
    "title", "label", "detail", "evidence", "tool", "raw_status", "rawStatus",
})
_DISPLAY_AUTH = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}")
_JSON_SECRET = re.compile(
    r'(?i)("(?:api[_-]?key|password|passwd|client[_-]?secret|access[_-]?token|'
    r'refresh[_-]?token|authorization|cookie|secret)"\s*:\s*)"(?:\\.|[^"\\])*"'
)
REDACTED = "[credential redacted]"
UNAVAILABLE = "[display unavailable: credential sanitation failed]"


def set_display_secret_resolver(resolver: Callable) -> None:
    global _resolver
    if not callable(resolver):
        raise TypeError("display secret resolver must be callable")
    _resolver = resolver


def _project(value, secrets, *, depth=0):
    if depth > 12:
        return "[display nesting omitted]"
    if isinstance(value, str):
        text = value
        for secret in secrets:
            text = text.replace(secret, REDACTED)
        for _, pattern in SECRET_TEXT_PATTERNS:
            text = pattern.sub(REDACTED, text)
        text = _DISPLAY_AUTH.sub(REDACTED, text)
        text = _JSON_SECRET.sub(lambda match: match[1] + '"' + REDACTED + '"', text)
        return text
    if isinstance(value, Mapping):
        return {str(key): REDACTED if _SECRET_FIELD.fullmatch(str(key)) else
                _project(item, secrets, depth=depth + 1)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_project(item, secrets, depth=depth + 1) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _project(str(value), secrets, depth=depth + 1)


def safe_display(value):
    """Resolve managed values per projection; retain no plaintext cache."""
    try:
        rows = _resolver() if _resolver is not None else ()
        secrets = sorted({secret for _, secret in (rows or ())
                          if isinstance(secret, str) and len(secret) >= 4}, key=len, reverse=True)
        return _project(value, secrets)
    except Exception:
        return UNAVAILABLE


def safe_display_fields(fields: Mapping) -> dict:
    display = {key: value for key, value in fields.items() if key in _DISPLAY_FIELDS}
    if not display:
        return dict(fields)
    safe = safe_display(display)
    return {**fields, **(safe if isinstance(safe, dict) else
                        {key: UNAVAILABLE for key in display})}
