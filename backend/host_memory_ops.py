"""Host adapters for the one approved-memory store."""

from __future__ import annotations

import memory_tools
from llm_usage import observe_usage_category
from memory_schema import MemoryRecord
from run_context import current_run_context


def _store(h):
    service = h.require_runtime().memory.store
    if service is None:
        raise RuntimeError("memory store is unavailable")
    return service


def _chat_id() -> str:
    context = current_run_context()
    return str(
        getattr(context, "session_id", "")
        or getattr(getattr(context, "work_scope", None), "chat_id", "")
        or ""
    )


async def mem_query(h, text, n=5):
    return [
        str(row.get("content") or "")[:600]
        for row in _store(h).retrieve(_chat_id(), str(text or ""), limit=max(1, min(3, int(n or 3))))
    ]


async def mem_prefetch(h, text, n=2):
    return await mem_query(h, text, max(1, min(2, int(n or 2))))


async def mem_add(h, text, mtype="fact", **fields):
    service = _store(h)
    source = str(fields.get("source") or "manual")
    result = service.remember_explicit(
        _chat_id(),
        str(text or ""),
        metadata={"type": str(mtype or "fact"), **dict(fields)},
        actor="user",
    )
    return str(result["item_id"])


async def mem_add_record(h, rec: MemoryRecord) -> str:
    record = rec.normalize()
    result = _store(h).remember_explicit(
        _chat_id(),
        record.content,
        metadata={
            "type": record.type,
            "context": record.context,
            "importance": record.importance,
            "confidence": record.confidence,
            "subject": record.subject,
            "source": record.source,
            "tags": list(record.tags),
            "source_ids": list(record.source_ids),
        },
        actor="host",
    )
    return str(result["item_id"])


async def mem_list(h, *, limit=200, offset=0):
    return _store(h).list_items(limit=limit, offset=offset)


async def mem_delete(h, mid):
    item = _store(h).get_item(str(mid))
    if item is not None:
        _store(h).tombstone(
            str(mid), expected_revision=int(item["version"]),
            actor="user", reason="deleted from Memory UI",
        )


async def remember_explicit(h, text: str, *, session_id: str = "") -> bool:
    return await memory_tools.remember_explicit(
        h.memory_ports(), text, session_id=session_id
    )


def profile_items(h):
    return _store(h).profile_items()


async def extract_and_store(
    h, user_text: str, reply: str, *, evidence: dict | None = None
):
    evidence = dict(evidence or {})
    chat_id = str(evidence.get("session_id") or _chat_id()).strip()
    if not chat_id:
        raise ValueError("memory extraction requires evidence.session_id")
    evidence["session_id"] = chat_id
    with observe_usage_category("memory"):
        records = await memory_tools.extract_and_store(
            h.memory_ports(), user_text, reply, evidence=evidence
        )
    lifecycle = _store(h)
    if not records:
        return []
    proposal_ids: list[str] = []
    for rec in records:
        if lifecycle.find_duplicate(rec.content) is not None:
            continue
        proposed = lifecycle.propose(
            chat_id,
            kind="add",
            content=rec.content,
            scope="user",
            provenance=(rec.source_ids[0] if rec.source_ids else f"chat:{chat_id}"),
            confidence=rec.confidence,
            metadata={
                "type": rec.type,
                "context": rec.context,
                "importance": rec.importance,
                "subject": rec.subject,
                "source": rec.source,
                "tags": list(rec.tags),
                "source_ids": list(rec.source_ids),
                "dedup_key": rec.dedup_key(),
            },
        )
        proposal_id = str(proposed["proposal_id"])
        proposal_ids.append(proposal_id)
    if proposal_ids:
        items = lifecycle.list_proposals(state="pending", limit=100)
        try:
            await h.hub.broadcast({
                "type": "memory:proposals",
                "items": items,
                "count": len(items),
            })
        except Exception:
            # Proposals are durable; reconnect/refresh remains a safe fallback.
            pass
    return proposal_ids


__all__ = [
    "extract_and_store", "mem_add", "mem_add_record", "mem_delete",
    "mem_list", "mem_prefetch", "mem_query", "profile_items",
    "remember_explicit",
]
