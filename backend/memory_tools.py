"""Model extraction and bounded recall over the approved-memory store."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re
from typing import Any, Awaitable, Callable

import tools
from internal_json import parse_object
from memory_schema import MemoryRecord


MEMORY_EXTRACT_SYS = (
    "Extract only durable information explicitly stated by the user. Return JSON "
    "with profile (short identity facts) and memories (objects with content, type, "
    "context, importance, tags, subject). Never infer from assistant text."
)
RETRIEVE_DEFAULT = 3
RETRIEVE_MAX = 6
RETRIEVE_MAX_CHARS = 3200
_DURABLE_MEMORY_SIGNAL = re.compile(
    r"\b(?:remember\s+(?:that|this)|keep\s+in\s+mind|from\s+now\s+on|"
    r"my\s+name\s+is|call\s+me|i\s+(?:prefer|like|dislike|hate)\b|"
    r"please\s+always\b|i\s+never\b)",
    re.IGNORECASE,
)


@dataclass
class MemoryPorts:
    store: Any
    sessions: Any
    router: Any
    hub: Any
    engine_status_msg: Callable[[], dict]


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _date(value: Any) -> str:
    import time

    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(value or 0)))
    except Exception:
        return "unknown-date"


def should_extract_durable_memory(user_text: str) -> bool:
    return bool(_DURABLE_MEMORY_SIGNAL.search(
        " ".join(str(user_text or "").split())
    ))


async def remember_explicit(
    ports: MemoryPorts, text: str, *, session_id: str = ""
) -> bool:
    content = _clip(text, 400)
    if not content:
        return False
    ports.store.remember_explicit(
        str(session_id or ""),
        content,
        metadata={
            "type": "fact",
            "context": "Explicitly saved by the user with /remember.",
            "importance": 5,
            "subject": "user",
            "source": "manual",
            "tags": ["user-authored", "explicit"],
            "source_ids": ([f"chat:{session_id}"] if session_id else []),
        },
        actor="user",
    )
    try:
        await ports.hub.broadcast(ports.engine_status_msg())
    except Exception:
        pass
    return True


async def retrieve_memory(ports: MemoryPorts, args: dict) -> str:
    query = str((args or {}).get("query") or "").strip()
    if not query:
        raise tools.ToolError("memory query requires a non-empty query")
    try:
        limit = max(1, min(RETRIEVE_MAX, int((args or {}).get("limit") or RETRIEVE_DEFAULT)))
    except (TypeError, ValueError):
        limit = RETRIEVE_DEFAULT
    chat_id = str((args or {}).get("_chat_id") or "")
    candidates: list[dict[str, Any]] = []
    for rank, row in enumerate(ports.store.retrieve(chat_id, query, limit=limit * 3)):
        candidates.append({
            "score": 100 - rank,
            "source": "approved",
            "date": _date(row.get("created_at")),
            "text": _clip(row.get("content"), 520),
        })
    history_unavailable = False
    try:
        hits = ports.sessions.search(query, limit=limit * 2)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("conversation memory search failed")
        history_unavailable = True
        hits = []
    for rank, hit in enumerate(hits):
        candidates.append({
            "score": 70 - rank,
            "source": "conversation",
            "date": _date(hit.get("ts")),
            "text": _clip(
                f"{hit.get('title') or 'past chat'} - "
                f"{hit.get('role') or 'message'}: {hit.get('snippet') or ''}",
                520,
            ),
        })
    candidates.sort(key=lambda item: float(item["score"]), reverse=True)
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        key = str(item["text"]).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        selected.append(item)
        if len(selected) >= limit:
            break
    if not selected:
        if history_unavailable:
            return f"No approved memory matched {query!r}. Conversation history could not be searched."
        return f"No stored memory matched {query!r}."
    lines = [f"Memory matches for {query!r}:"]
    if history_unavailable:
        lines.append("Conversation history could not be searched; these matches are from approved memory only.")
    for item in selected:
        line = f"- [{item['date']}] [{item['source']}] {item['text']}"
        if len("\n".join([*lines, line])) > RETRIEVE_MAX_CHARS:
            break
        lines.append(line)
    return "\n".join(lines)


async def _extract_json_response(
    router: Any,
    system_prompt: str,
    user_content: str,
    *,
    max_tokens: int,
) -> dict:
    parts = []
    async for token in router.stream(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        sampling={"temperature": 0.1, "max_tokens": max_tokens},
        json_mode=True,
        reasoning_budget=0,
        reasoning_sink=lambda _chunk: None,
    ):
        parts.append(token)
    value = parse_object("".join(parts)) or {}
    return value if isinstance(value, dict) else {}


async def extract_and_store(
    ports: MemoryPorts,
    user_text: str,
    reply: str,
    *,
    evidence: dict | None = None,
) -> list[MemoryRecord]:
    chat_id = str((evidence or {}).get("session_id") or "")
    if not should_extract_durable_memory(user_text):
        return []
    router = ports.router
    if not router.engine_ready and not (
        router.mode == "cloud" and router.cloud_route_ready()
    ):
        return []
    try:
        obj = await _extract_json_response(
            router,
            MEMORY_EXTRACT_SYS,
            f"USER_AUTHORED_MESSAGE:\n{user_text}",
            max_tokens=256,
        )
        source_ids = [f"chat:{chat_id}"] if chat_id else []
        proposals: list[MemoryRecord] = []
        for raw in list(obj.get("profile") or ())[:2]:
            record = MemoryRecord(
                content=_clip(raw, 400), type="profile",
                context="Proposed from an explicit user-authored statement.",
                importance=5, confidence=0.8, subject="user", source="chat",
                tags=["profile-candidate"], source_ids=source_ids,
            ).normalize()
            if record.valid:
                proposals.append(record)
        for raw in list(obj.get("memories") or ())[:6]:
            row = raw if isinstance(raw, dict) else {"content": str(raw)}
            record = MemoryRecord(
                content=row.get("content", ""), type=row.get("type", "fact"),
                context=row.get("context", ""), importance=row.get("importance", 3),
                tags=row.get("tags", []), subject=row.get("subject", "user"),
                source="chat", source_ids=source_ids,
            ).normalize()
            if record.valid:
                proposals.append(record)
        unique: list[MemoryRecord] = []
        seen: set[str] = set()
        for record in proposals:
            key = record.dedup_key()
            if key and key not in seen:
                seen.add(key)
                unique.append(record)
        return unique
    except Exception as exc:
        print(f"[variant1-backend] memory extraction skipped: {exc}", flush=True)
        return []


async def consolidate_memory_once(ports: MemoryPorts) -> int:
    return ports.store.consolidate_exact_duplicates(actor="system:dedup")


async def consolidation_loop(
    consolidate_once: Callable[[], Awaitable[int]],
) -> None:
    await asyncio.sleep(600)
    while True:
        try:
            await consolidate_once()
        except Exception as exc:
            print(f"[memory] consolidation loop error: {exc}", flush=True)
        await asyncio.sleep(6 * 3600)


__all__ = [
    "MemoryPorts",
    "consolidate_memory_once",
    "consolidation_loop",
    "extract_and_store",
    "remember_explicit",
    "retrieve_memory",
    "should_extract_durable_memory",
]
