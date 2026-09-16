"""Goal-owned descendant resource cleanup.

This module is deliberately lifecycle-neutral: it stops resources and returns
an exact result, while :class:`GoalService` remains the authority that persists
goal/step cleanup state.  Ownership begins with durable goal spawn-effect
records supplied by the caller; a parent chat alone is never cleanup authority.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar


_TERMINAL_CHILD_STATES = frozenset({"completed", "failed", "cancelled"})


def _text(value: Any, *, limit: int = 2_000) -> str:
    return str(value or "").strip()[:limit]


@dataclass(frozen=True, slots=True)
class GoalChildRoot:
    """One exact child identity taken from a goal-owned spawn effect."""

    child_id: str
    parent_chat_id: str
    child_chat_id: str
    effect_id: str = ""

    def __post_init__(self) -> None:
        for field_name in ("child_id", "parent_chat_id", "child_chat_id"):
            value = _text(getattr(self, field_name), limit=512)
            if not value:
                raise ValueError(f"goal child root requires {field_name}")
            object.__setattr__(self, field_name, value)
        object.__setattr__(self, "effect_id", _text(self.effect_id, limit=512))

    @classmethod
    def from_value(cls, value: "GoalChildRoot | Mapping[str, Any]") -> "GoalChildRoot":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("goal child root must be a mapping")
        return cls(
            child_id=str(value.get("child_id") or ""),
            parent_chat_id=str(value.get("parent_chat_id") or ""),
            child_chat_id=str(value.get("child_chat_id") or ""),
            effect_id=str(value.get("effect_id") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "child_id": self.child_id,
            "parent_chat_id": self.parent_chat_id,
            "child_chat_id": self.child_chat_id,
            "effect_id": self.effect_id or None,
        }


@dataclass(frozen=True, slots=True)
class CleanupIssue:
    """One retryable cleanup uncertainty, without suppressing other cleanup."""

    phase: str
    resource_kind: str
    resource_id: str
    owner_chat_id: str
    error: str
    retryable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "resource_kind": self.resource_kind,
            "resource_id": self.resource_id,
            "owner_chat_id": self.owner_chat_id,
            "error": self.error,
            "retryable": bool(self.retryable),
        }


@dataclass(frozen=True, slots=True)
class GoalResourceCleanupResult:
    """Complete evidence for one idempotent descendant cleanup pass."""

    schema: ClassVar[str] = "variant1.goal-resource-cleanup.v1"

    goal_id: str
    roots: tuple[GoalChildRoot, ...]
    descendant_chat_ids: tuple[str, ...]
    cancelled_child_ids: tuple[str, ...]
    stopped_process_ids: tuple[str, ...]
    closed_terminal_ids: tuple[str, ...]
    closed_kernel_chat_ids: tuple[str, ...]
    remaining: tuple[dict[str, Any], ...]
    issues: tuple[CleanupIssue, ...]
    discovery_complete: bool

    @property
    def complete(self) -> bool:
        return bool(
            self.discovery_complete
            and not self.remaining
            and not self.issues
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "goal_id": self.goal_id,
            "complete": self.complete,
            "discovery_complete": bool(self.discovery_complete),
            "roots": [root.to_dict() for root in self.roots],
            "descendant_chat_ids": list(self.descendant_chat_ids),
            "cancelled_child_ids": list(self.cancelled_child_ids),
            "stopped_process_ids": list(self.stopped_process_ids),
            "closed_terminal_ids": list(self.closed_terminal_ids),
            "closed_kernel_chat_ids": list(self.closed_kernel_chat_ids),
            "remaining": [dict(item) for item in self.remaining],
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _issue(
    issues: list[CleanupIssue],
    *,
    phase: str,
    resource_kind: str,
    resource_id: str,
    owner_chat_id: str,
    error: BaseException | str,
) -> None:
    issues.append(CleanupIssue(
        phase=_text(phase, limit=80),
        resource_kind=_text(resource_kind, limit=80),
        resource_id=_text(resource_id, limit=512),
        owner_chat_id=_text(owner_chat_id, limit=512),
        error=_text(
            f"{type(error).__name__}: {error}"
            if isinstance(error, BaseException)
            else error,
        ),
    ))


def _child_identity(value: Mapping[str, Any], *, fallback_depth: int = 0) -> dict[str, Any]:
    return {
        "child_id": _text(value.get("child_id"), limit=512),
        "parent_chat_id": _text(value.get("parent_chat_id"), limit=512),
        "child_chat_id": _text(value.get("child_chat_id"), limit=512),
        "status": _text(value.get("status"), limit=80),
        "depth": max(0, int(value.get("depth") or fallback_depth)),
    }


async def cleanup_goal_descendant_resources(
    *,
    goal_id: str,
    roots: Iterable[GoalChildRoot | Mapping[str, Any]],
    child_manager: Any,
    execution: Any,
    kernel: Any,
) -> GoalResourceCleanupResult:
    """Stop and settle resources reachable from goal-owned child roots.

    The operation is idempotent.  Failures are accumulated so one broken owner
    cannot prevent teardown of another, then returned as explicit retryable
    evidence.  ``asyncio.CancelledError`` is not caught and therefore preserves
    outer cancellation semantics.
    """

    clean_goal_id = _text(goal_id, limit=512)
    if not clean_goal_id:
        raise ValueError("goal_id is required for descendant cleanup")
    if child_manager is None or execution is None or kernel is None:
        raise ValueError("child_manager, execution, and kernel are required")

    clean_roots = tuple(
        dict.fromkeys(GoalChildRoot.from_value(root) for root in roots)
    )
    issues: list[CleanupIssue] = []
    discovered: dict[str, dict[str, Any]] = {}
    discovery_complete = True

    for root in clean_roots:
        try:
            observed = _child_identity(
                child_manager.inspect(root.parent_chat_id, root.child_id),
                fallback_depth=0,
            )
            if (
                observed["child_id"] != root.child_id
                or observed["parent_chat_id"] != root.parent_chat_id
                or observed["child_chat_id"] != root.child_chat_id
            ):
                raise RuntimeError("spawn-effect child identity does not match child catalog")
        except Exception as exc:
            discovery_complete = False
            _issue(
                issues,
                phase="discover",
                resource_kind="child",
                resource_id=root.child_id,
                owner_chat_id=root.child_chat_id,
                error=exc,
            )
            # A mismatched catalog identity cannot authorize cleanup of a chat.
            continue

        discovered[root.child_id] = observed
        try:
            enumerate_for_cleanup = getattr(
                child_manager, "descendants_for_cleanup", None,
            )
            if callable(enumerate_for_cleanup):
                tree = {
                    "items": list(enumerate_for_cleanup(root.child_chat_id) or ()),
                    "truncated": False,
                }
            else:
                # Compatibility for isolated/fake managers. Production exposes
                # an internally unbounded ownership traversal; the public tree
                # view is conservative and can never authorize full success
                # when it reports truncation.
                tree = child_manager.tree(root.child_chat_id, limit=100)
            if bool(tree.get("truncated")):
                discovery_complete = False
                _issue(
                    issues,
                    phase="discover",
                    resource_kind="descendant_tree",
                    resource_id=root.child_id,
                    owner_chat_id=root.child_chat_id,
                    error="owned descendant tree exceeds the canonical 100-item view",
                )
            for item in tree.get("items") or ():
                if not isinstance(item, Mapping):
                    discovery_complete = False
                    _issue(
                        issues,
                        phase="discover",
                        resource_kind="child",
                        resource_id=root.child_id,
                        owner_chat_id=root.child_chat_id,
                        error="descendant tree returned a malformed child record",
                    )
                    continue
                child = _child_identity(item, fallback_depth=1)
                if not all(child[key] for key in (
                    "child_id", "parent_chat_id", "child_chat_id",
                )):
                    discovery_complete = False
                    _issue(
                        issues,
                        phase="discover",
                        resource_kind="child",
                        resource_id=child["child_id"] or root.child_id,
                        owner_chat_id=child["child_chat_id"] or root.child_chat_id,
                        error="descendant tree returned an incomplete child identity",
                    )
                    continue
                discovered.setdefault(child["child_id"], child)
        except Exception as exc:
            discovery_complete = False
            _issue(
                issues,
                phase="discover",
                resource_kind="descendant_tree",
                resource_id=root.child_id,
                owner_chat_id=root.child_chat_id,
                error=exc,
            )

    ordered = sorted(
        discovered.values(),
        key=lambda item: (int(item["depth"]), item["child_id"]),
        reverse=True,
    )
    cancelled_children: list[str] = []
    stopped_processes: list[str] = []
    closed_terminals: list[str] = []
    closed_kernels: list[str] = []
    remaining: list[dict[str, Any]] = []

    for child in ordered:
        child_id = str(child["child_id"])
        parent_chat_id = str(child["parent_chat_id"])
        child_chat_id = str(child["child_chat_id"])

        try:
            latest = _child_identity(
                child_manager.inspect(parent_chat_id, child_id),
                fallback_depth=int(child["depth"]),
            )
            if latest["child_chat_id"] != child_chat_id:
                raise RuntimeError("child chat identity changed during cleanup")
            child = latest
        except Exception as exc:
            _issue(
                issues,
                phase="verify",
                resource_kind="child",
                resource_id=child_id,
                owner_chat_id=child_chat_id,
                error=exc,
            )
            remaining.append({
                "kind": "child",
                "id": child_id,
                "owner_chat_id": child_chat_id,
                "state": "unknown",
            })
        else:
            if str(child.get("status") or "") not in _TERMINAL_CHILD_STATES:
                try:
                    settled = _child_identity(
                        await child_manager.cancel(parent_chat_id, child_id),
                        fallback_depth=int(child["depth"]),
                    )
                    if settled["status"] not in _TERMINAL_CHILD_STATES:
                        raise RuntimeError(
                            f"child remained {settled['status'] or 'nonterminal'} after cancellation"
                        )
                    cancelled_children.append(child_id)
                except Exception as exc:
                    _issue(
                        issues,
                        phase="cancel_child",
                        resource_kind="child",
                        resource_id=child_id,
                        owner_chat_id=child_chat_id,
                        error=exc,
                    )
                    remaining.append({
                        "kind": "child",
                        "id": child_id,
                        "owner_chat_id": child_chat_id,
                        "state": "cleanup_pending",
                    })

        before_terminals: tuple[str, ...] = ()
        before_processes: tuple[str, ...] = ()
        try:
            before_terminals, before_processes = (
                execution.repository.live_ids_for_chat(child_chat_id)
            )
        except Exception as exc:
            _issue(
                issues,
                phase="verify",
                resource_kind="execution",
                resource_id=child_chat_id,
                owner_chat_id=child_chat_id,
                error=exc,
            )

        try:
            await execution.delete_chat(child_chat_id)
        except Exception as exc:
            _issue(
                issues,
                phase="execution",
                resource_kind="execution",
                resource_id=child_chat_id,
                owner_chat_id=child_chat_id,
                error=exc,
            )

        try:
            after_terminals, after_processes = (
                execution.repository.live_ids_for_chat(child_chat_id)
            )
            live_terminals = set(after_terminals)
            live_processes = set(after_processes)
            closed_terminals.extend(
                identity for identity in before_terminals
                if identity not in live_terminals
            )
            stopped_processes.extend(
                identity for identity in before_processes
                if identity not in live_processes
            )
        except Exception as exc:
            _issue(
                issues,
                phase="verify_before_kernel",
                resource_kind="execution",
                resource_id=child_chat_id,
                owner_chat_id=child_chat_id,
                error=exc,
            )

        try:
            if await kernel.close_chat(child_chat_id, reason="goal_cancelled"):
                closed_kernels.append(child_chat_id)
        except Exception as exc:
            _issue(
                issues,
                phase="kernel",
                resource_kind="kernel",
                resource_id=child_chat_id,
                owner_chat_id=child_chat_id,
                error=exc,
            )
            remaining.append({
                "kind": "kernel",
                "id": child_chat_id,
                "owner_chat_id": child_chat_id,
                "state": "unknown",
            })

        # Closing the producer is the fence.  A capability already in flight
        # can publish a managed process after the first execution snapshot, so
        # perform one bounded post-kernel sweep and verify the durable owner
        # index again.  This is not a polling loop; any remaining uncertainty
        # is returned for the lifecycle owner's normal cleanup retry.
        late_terminals: tuple[str, ...] = ()
        late_processes: tuple[str, ...] = ()
        try:
            late_terminals, late_processes = (
                execution.repository.live_ids_for_chat(child_chat_id)
            )
        except Exception as exc:
            _issue(
                issues,
                phase="verify_after_kernel",
                resource_kind="execution",
                resource_id=child_chat_id,
                owner_chat_id=child_chat_id,
                error=exc,
            )
        try:
            await execution.delete_chat(child_chat_id)
        except Exception as exc:
            _issue(
                issues,
                phase="execution_after_kernel",
                resource_kind="execution",
                resource_id=child_chat_id,
                owner_chat_id=child_chat_id,
                error=exc,
            )
        try:
            final_terminals, final_processes = (
                execution.repository.live_ids_for_chat(child_chat_id)
            )
            final_terminal_set = set(final_terminals)
            final_process_set = set(final_processes)
            closed_terminals.extend(
                identity for identity in late_terminals
                if identity not in final_terminal_set
            )
            stopped_processes.extend(
                identity for identity in late_processes
                if identity not in final_process_set
            )
            remaining.extend({
                "kind": "terminal",
                "id": identity,
                "owner_chat_id": child_chat_id,
                "state": "live",
            } for identity in sorted(final_terminal_set))
            remaining.extend({
                "kind": "process",
                "id": identity,
                "owner_chat_id": child_chat_id,
                "state": "live",
            } for identity in sorted(final_process_set))
            if final_terminal_set or final_process_set:
                _issue(
                    issues,
                    phase="verify_after_kernel",
                    resource_kind="execution",
                    resource_id=child_chat_id,
                    owner_chat_id=child_chat_id,
                    error=(
                        f"{len(final_terminal_set)} terminal(s) and "
                        f"{len(final_process_set)} process(es) remain live"
                    ),
                )
        except Exception as exc:
            _issue(
                issues,
                phase="verify_after_kernel",
                resource_kind="execution",
                resource_id=child_chat_id,
                owner_chat_id=child_chat_id,
                error=exc,
            )
            remaining.append({
                "kind": "execution",
                "id": child_chat_id,
                "owner_chat_id": child_chat_id,
                "state": "unknown",
            })

    # A child could finish a spawn dispatch while its parent was being closed.
    # Rescan once after every known producer is closed.  Newly discovered owned
    # children are surfaced for the serialized retry rather than cleaned from
    # an unbounded busy-loop or omitted from a false-success receipt.
    known_children = set(discovered)
    for root in clean_roots:
        try:
            enumerate_for_cleanup = getattr(
                child_manager, "descendants_for_cleanup", None,
            )
            if callable(enumerate_for_cleanup):
                latest_descendants = list(
                    enumerate_for_cleanup(root.child_chat_id) or ()
                )
                truncated = False
            else:
                latest_tree = child_manager.tree(root.child_chat_id, limit=100)
                latest_descendants = list(latest_tree.get("items") or ())
                truncated = bool(latest_tree.get("truncated"))
            if truncated:
                raise RuntimeError(
                    "owned descendant tree exceeds the canonical 100-item view"
                )
            for item in latest_descendants:
                if not isinstance(item, Mapping):
                    raise RuntimeError(
                        "descendant rescan returned a malformed child record"
                    )
                child = _child_identity(item, fallback_depth=1)
                if child["child_id"] in known_children:
                    continue
                discovery_complete = False
                remaining.append({
                    "kind": "child",
                    "id": child["child_id"],
                    "owner_chat_id": child["child_chat_id"],
                    "state": "discovered_after_producer_close",
                })
                _issue(
                    issues,
                    phase="rescan",
                    resource_kind="child",
                    resource_id=child["child_id"],
                    owner_chat_id=child["child_chat_id"],
                    error="owned child appeared during cleanup; retry cleanup",
                )
        except Exception as exc:
            discovery_complete = False
            _issue(
                issues,
                phase="rescan",
                resource_kind="descendant_tree",
                resource_id=root.child_id,
                owner_chat_id=root.child_chat_id,
                error=exc,
            )

    def unique(values: Iterable[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(str(value) for value in values if str(value)))

    return GoalResourceCleanupResult(
        goal_id=clean_goal_id,
        roots=clean_roots,
        descendant_chat_ids=unique(
            child["child_chat_id"] for child in ordered
        ),
        cancelled_child_ids=unique(cancelled_children),
        stopped_process_ids=unique(stopped_processes),
        closed_terminal_ids=unique(closed_terminals),
        closed_kernel_chat_ids=unique(closed_kernels),
        remaining=tuple(dict(item) for item in remaining),
        issues=tuple(issues),
        discovery_complete=discovery_complete,
    )


__all__ = [
    "CleanupIssue",
    "GoalChildRoot",
    "GoalResourceCleanupResult",
    "cleanup_goal_descendant_resources",
]
