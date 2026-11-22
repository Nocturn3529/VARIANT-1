"""Text boundary helpers shared by evidence projections (no runtime state)."""

from __future__ import annotations


def utf8_prefix(raw: bytes, max_bytes: int) -> bytes:
    """Keep complete UTF-8 points at a byte cap without hiding invalid input."""

    candidate = raw[:max(0, int(max_bytes))]
    try:
        candidate.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        if exc.reason == "unexpected end of data" and exc.end == len(candidate):
            return candidate[:exc.start]
        # Binary/degraded callers retain their existing replacement policy.
    return candidate


def utf8_suffix(raw: bytes, max_bytes: int) -> bytes:
    """Keep complete UTF-8 points at the end of already encoded text."""

    start = max(0, len(raw) - max(0, int(max_bytes)))
    while start < len(raw) and raw[start] & 0xC0 == 0x80:
        start += 1
    return raw[start:]


def head_tail_text(text: str, max_chars: int) -> str:
    """Fit an opening, omission notice, and ending within a character cap."""

    if len(text) <= max_chars:
        return text
    marker = "\n[… {} characters omitted …]\n"
    room = max_chars - len(marker.format(len(text)))
    if room < 2:
        marker = "…"
        room = max_chars - len(marker)
    if room <= 0:
        return marker[:max(0, max_chars)]
    marker = marker.format(len(text) - room)
    tail = max(1, room // 4)
    return text[:room - tail] + marker + text[-tail:]


def truncated_text(text: str, max_bytes: int, *, marker: str = "\n[Result truncated]") -> tuple[str, int]:
    """A byte-bounded marked preview and the number of original bytes retained."""

    raw = str(text).encode("utf-8", errors="replace")
    cap = max(0, int(max_bytes))
    if len(raw) <= cap:
        return str(text), len(raw)
    label = marker.encode("utf-8")
    if len(label) > cap:
        label = ("…" if cap >= 3 else ".." if cap >= 2 else "." if cap else "").encode("utf-8")
    prefix = utf8_prefix(raw, cap - len(label))
    return prefix.decode("utf-8", errors="replace") + label.decode("utf-8"), len(prefix)
