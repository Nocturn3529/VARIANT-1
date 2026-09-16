"""Dependency-light value contracts for immutable conversation history."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


CONVERSATION_SCHEMA = "variant1.conversation.v1"

EDGE_KINDS = frozenset({"continuation", "fork", "merge", "edit", "replay"})
RUNTIME_FORK_MODES = frozenset({"pending", "snapshot", "fresh"})


class ConversationError(RuntimeError):
    pass


class ConversationNotFound(ConversationError):
    pass


class ConversationConflict(ConversationError):
    pass


class ConversationTombstoned(ConversationError):
    pass


class InvalidConversationGraph(ConversationError):
    pass


@dataclass(frozen=True)
class ConversationRecord:
    conversation_id: str
    title: str
    default_branch_id: str = ""
    pinned: bool = False
    archived: bool = False
    version: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0
    tombstoned_at: float = 0.0

    @property
    def deleted(self) -> bool:
        return self.tombstoned_at > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CONVERSATION_SCHEMA,
            "conversation_id": self.conversation_id,
            "title": self.title,
            "default_branch_id": self.default_branch_id,
            "pinned": bool(self.pinned),
            "archived": bool(self.archived),
            "version": int(self.version),
            "created_at": float(self.created_at),
            "updated_at": float(self.updated_at),
            "tombstoned_at": float(self.tombstoned_at),
        }


@dataclass(frozen=True)
class BranchRecord:
    branch_id: str
    conversation_id: str
    name: str
    base_node_id: str = ""
    head_node_id: str = ""
    parent_branch_id: str = ""
    runtime_chat_id: str = ""
    runtime_thread_id: str = ""
    runtime_run_id: str = ""
    runtime_fork_mode: str = "fresh"
    runtime_fork_reason: str = ""
    version: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0
    tombstoned_at: float = 0.0

    @property
    def deleted(self) -> bool:
        return self.tombstoned_at > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch_id": self.branch_id,
            "conversation_id": self.conversation_id,
            "name": self.name,
            "base_node_id": self.base_node_id or None,
            "head_node_id": self.head_node_id or None,
            "parent_branch_id": self.parent_branch_id or None,
            "runtime_chat_id": self.runtime_chat_id or None,
            "runtime_thread_id": self.runtime_thread_id or None,
            "runtime_run_id": self.runtime_run_id or None,
            "runtime_fork_mode": self.runtime_fork_mode,
            "runtime_fork_reason": self.runtime_fork_reason or None,
            "version": int(self.version),
            "created_at": float(self.created_at),
            "updated_at": float(self.updated_at),
            "tombstoned_at": float(self.tombstoned_at),
        }


@dataclass(frozen=True)
class ConversationNode:
    node_id: str
    conversation_id: str
    role: str
    content: Any
    content_ref: str = ""
    preview: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    turn_id: str = ""
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "conversation_id": self.conversation_id,
            "role": self.role,
            "content": self.content,
            "content_ref": self.content_ref or None,
            "preview": self.preview,
            "metadata": dict(self.metadata),
            "turn_id": self.turn_id or None,
            "created_at": float(self.created_at),
        }


@dataclass(frozen=True)
class SearchHit:
    conversation_id: str
    node_id: str = ""
    branch_id: str = ""
    title: str = ""
    role: str = ""
    preview: str = ""
    score: float = 0.0
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "node_id": self.node_id or None,
            "branch_id": self.branch_id or None,
            "title": self.title,
            "role": self.role or None,
            "preview": self.preview,
            "score": float(self.score),
            "created_at": float(self.created_at),
        }


__all__ = [
    "BranchRecord",
    "CONVERSATION_SCHEMA",
    "ConversationConflict",
    "ConversationError",
    "ConversationNode",
    "ConversationNotFound",
    "ConversationRecord",
    "ConversationTombstoned",
    "EDGE_KINDS",
    "InvalidConversationGraph",
    "RUNTIME_FORK_MODES",
    "SearchHit",
]
