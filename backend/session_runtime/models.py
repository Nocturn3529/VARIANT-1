"""Value contracts for session runtime ownership."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from session_catalog.profiles import (
    ACTION_SURFACE,
    CHAT_GRAPH_REVISION,
    DISCLOSURE_PROFILE,
    DISCLOSURE_REVISION,
    IPYTHON_SCHEMA_REVISION,
)


RUNTIME_SCHEMA_VERSION = 3
RUNTIME_STATES = frozenset({"creating", "active", "deleting", "deleted"})
TICKET_STATES = frozenset({
    "parked",
    "queued",
    "resume_queued",
    "selected",
    "preparing",
    "transcript_committing",
    "transcript_failed",
    "running",
    "completed",
    "cancelled",
    "rejected",
})
TICKET_TERMINAL_STATES = frozenset({
    "completed", "cancelled", "rejected", "transcript_failed"
})


class RuntimeDeleted(RuntimeError):
    """The durable chat is tombstoned and rejects new admissions."""


class BudgetExhausted(RuntimeError):
    """The host-owned continuation budget does not permit another run."""


@dataclass(frozen=True)
class RuntimeIdentity:
    """Pinned identity assigned before a chat's first model request."""

    action_surface: str = ACTION_SURFACE
    provider_tool_schema_revision: str = IPYTHON_SCHEMA_REVISION
    graph_revision: str = CHAT_GRAPH_REVISION
    catalog_release_id: str = ""
    environment_digest: str = ""
    trust_profile: str = "trusted-local.v1"
    disclosure_profile_id: str = DISCLOSURE_PROFILE
    disclosure_profile_revision: str = DISCLOSURE_REVISION
    discovery_state_ref: str = ""
    mount_revision: int | None = None
    selected_category_id: str = ""
    overlay_revision: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_surface": self.action_surface,
            "provider_tool_schema_revision": self.provider_tool_schema_revision,
            "graph_revision": self.graph_revision,
            "catalog_release_id": self.catalog_release_id,
            "environment_digest": self.environment_digest,
            "trust_profile": self.trust_profile,
            "disclosure_profile_id": self.disclosure_profile_id,
            "disclosure_profile_revision": self.disclosure_profile_revision,
            "discovery_state_ref": self.discovery_state_ref,
            "mount_revision": self.mount_revision,
            "selected_category_id": self.selected_category_id,
            "overlay_revision": int(self.overlay_revision),
        }


@dataclass(frozen=True)
class ChatRuntimeRecord:
    chat_id: str
    lifecycle_state: str
    identity: RuntimeIdentity
    kernel_generation: int = 0
    continuation_state: str = "ready"
    mutation_write_enabled: bool = False
    mutation_authority_revision: int = 0
    mutation_authority_updated_at: float = 0.0
    mutation_authority_actor: str = ""
    budget_limits: dict[str, float] = field(default_factory=dict)
    budget_used: dict[str, float] = field(default_factory=dict)
    creation_saga_state: str = "complete"
    deletion_saga_state: str = ""
    version: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0
    tombstoned_at: float = 0.0

    @property
    def deleted(self) -> bool:
        return self.lifecycle_state in {"deleting", "deleted"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "chat_id": self.chat_id,
            "lifecycle_state": self.lifecycle_state,
            **self.identity.to_dict(),
            "kernel_generation": int(self.kernel_generation),
            "continuation_state": self.continuation_state,
            "mutation_write_enabled": bool(self.mutation_write_enabled),
            "mutation_authority_revision": int(self.mutation_authority_revision),
            "mutation_authority_updated_at": float(
                self.mutation_authority_updated_at
            ),
            "mutation_authority_actor": self.mutation_authority_actor,
            "budget_limits": dict(self.budget_limits),
            "budget_used": dict(self.budget_used),
            "creation_saga_state": self.creation_saga_state,
            "deletion_saga_state": self.deletion_saga_state,
            "version": int(self.version),
            "created_at": float(self.created_at),
            "updated_at": float(self.updated_at),
            "tombstoned_at": float(self.tombstoned_at),
        }


@dataclass(frozen=True)
class InputTicket:
    ticket_id: str
    chat_id: str
    delivery: str
    text: str
    state: str
    client_id: str = ""
    source: str = ""
    attachment_id: str = ""
    run_id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    selected_at: float = 0.0
    transcript_committed_at: float = 0.0
    completed_at: float = 0.0
    proof: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def terminal(self) -> bool:
        return self.state in TICKET_TERMINAL_STATES

    def as_state(self) -> dict[str, Any]:
        return {
            "id": self.ticket_id,
            "text": self.text,
            "delivery": self.delivery,
            "client_id": self.client_id,
            "source": self.source,
            "session_id": self.chat_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "chat_id": self.chat_id,
            "delivery": self.delivery,
            "text": self.text,
            "state": self.state,
            "client_id": self.client_id,
            "source": self.source,
            "attachment_id": self.attachment_id,
            "run_id": self.run_id,
            "created_at": float(self.created_at),
            "updated_at": float(self.updated_at),
            "selected_at": float(self.selected_at),
            "transcript_committed_at": float(self.transcript_committed_at),
            "completed_at": float(self.completed_at),
            "proof": dict(self.proof),
            "error": self.error,
        }
