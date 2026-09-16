from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import host_memory_ops
from memory_schema import MemoryRecord
from memory_store import MemoryStore


@pytest.mark.asyncio
async def test_post_turn_candidates_remain_pending_for_memory_page(tmp_path):
    lifecycle = MemoryStore(str(tmp_path / "memory.sqlite3"))
    broadcasts = []
    host = SimpleNamespace(
        memory_ports=lambda: object(),
        hub=SimpleNamespace(broadcast=AsyncMock(
            side_effect=lambda message: broadcasts.append(message)
        )),
        require_runtime=lambda: SimpleNamespace(
            memory=SimpleNamespace(store=lifecycle),
        ),
    )
    record = MemoryRecord(
        content="The user prefers concise replies",
        type="preference",
        context="The user stated it",
        confidence=0.9,
        source_ids=["chat:chat-a"],
    ).normalize()

    with patch.object(
        host_memory_ops.memory_tools,
        "extract_and_store",
        new=AsyncMock(return_value=[record]),
    ):
        proposal_ids = await host_memory_ops.extract_and_store(
            host,
            "I prefer concise replies",
            "Understood",
            evidence={"session_id": "chat-a"},
        )

    assert len(proposal_ids) == 1
    assert lifecycle.retrieve("chat-a", "concise") == []
    proposals = lifecycle.list_proposals(state="pending")
    assert proposals[0]["proposal_id"] == proposal_ids[0]
    assert proposals[0]["content"] == record.content
    assert broadcasts[-1]["type"] == "memory:proposals"
    assert broadcasts[-1]["count"] == 1


@pytest.mark.asyncio
async def test_repeated_post_turn_memory_does_not_duplicate_pending_add(tmp_path):
    lifecycle = MemoryStore(str(tmp_path / "memory.sqlite3"))
    host = SimpleNamespace(
        memory_ports=lambda: object(),
        hub=SimpleNamespace(broadcast=AsyncMock()),
        require_runtime=lambda: SimpleNamespace(
            memory=SimpleNamespace(store=lifecycle),
        ),
    )
    record = MemoryRecord(
        content="The user prefers concise replies",
        type="preference", source_ids=["chat:chat-a"],
    ).normalize()

    with patch.object(
        host_memory_ops.memory_tools, "extract_and_store",
        new=AsyncMock(return_value=[record]),
    ):
        first = await host_memory_ops.extract_and_store(
            host, "I prefer concise replies", "ok",
            evidence={"session_id": "chat-a"},
        )
        second = await host_memory_ops.extract_and_store(
            host, "I prefer concise replies", "ok",
            evidence={"session_id": "chat-a"},
        )

    assert len(first) == 1
    assert second == []
    assert len(lifecycle.list_proposals(state="pending")) == 1


def test_memory_page_approval_accepts_corrected_content(tmp_path):
    lifecycle = MemoryStore(str(tmp_path / "memory.sqlite3"))
    proposal = lifecycle.propose(
        "chat-a", kind="add", content="The deployment lane is blue"
    )

    lifecycle.approve(
        proposal["proposal_id"],
        actor="user",
        content_override="The deployment lane is green",
    )

    assert lifecycle.retrieve("chat-a", "blue") == []
    assert lifecycle.retrieve("chat-a", "green")[0]["content"].endswith("green")
    assert lifecycle.list_proposals(state="pending") == []


def test_memory_page_rejection_keeps_proposal_out_of_recall(tmp_path):
    lifecycle = MemoryStore(str(tmp_path / "memory.sqlite3"))
    proposal = lifecycle.propose("chat-a", kind="add", content="Do not keep this")

    lifecycle.reject(proposal["proposal_id"], actor="user", note="not useful")

    assert lifecycle.retrieve("chat-a", "keep") == []
    rejected = lifecycle.list_proposals(state="rejected")
    assert rejected[0]["proposal_id"] == proposal["proposal_id"]
    assert rejected[0]["review_note"] == "not useful"
