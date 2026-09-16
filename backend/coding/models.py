"""Typed, dependency-light contracts for Git, worktrees, and code review.

The coding domain stores only durable identifiers and observations.  Live
``subprocess`` objects, repository locks, and terminal/process handles never
cross this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core_invariants import StrictJSONError, strict_json_value
from work_fabric.scope import WorkScope


WORKTREE_STATES = frozenset({
    "creating",
    "active",
    "conflicted",
    "detached",
    "retiring",
    "orphaned",
    "retired",
})
REVIEW_STATES = frozenset({"open", "approved", "stale", "integrated", "closed"})
CHECK_STATES = frozenset({"queued", "running", "passed", "failed", "cancelled", "unknown_effect"})


class CodingError(RuntimeError):
    """Base class for coding-domain failures."""


class GitUnavailable(CodingError):
    """The configured Git executable is unavailable."""


class GitCommandError(CodingError):
    """A Git command failed with bounded diagnostic output."""

    def __init__(
        self,
        message: str,
        *,
        arguments: tuple[str, ...] = (),
        returncode: int = 1,
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.arguments = tuple(arguments)
        self.returncode = int(returncode)
        self.stderr = str(stderr or "")


class RepositoryNotFound(CodingError):
    """A requested registered repository does not exist."""


class WorktreeNotFound(CodingError):
    """A requested managed worktree does not exist."""


class ReviewNotFound(CodingError):
    """A requested review does not exist."""


class CodingConflict(CodingError):
    """A compare-and-set revision, head, or index observation changed."""


class ReviewStale(CodingConflict):
    """The repository no longer matches the exact reviewed snapshot."""


class UnsafeWorktreeRemoval(CodingError):
    """A worktree failed one or more non-destructive removal gates."""


class ScopeMismatch(CodingError):
    """A requested coding object is outside the supplied WorkScope."""


class CodingValidationError(CodingError, ValueError):
    """A command or persisted coding value is structurally invalid."""


def json_value(value: Any, *, path: str = "$") -> Any:
    try:
        return strict_json_value(value, path=path)
    except StrictJSONError as exc:
        raise CodingValidationError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class RepositoryRecord:
    repository_id: str
    root: str
    git_dir: str
    common_dir: str
    object_format: str
    default_branch: str
    head_oid: str
    branch: str
    remotes: tuple[dict[str, Any], ...] = ()
    scope: WorkScope = field(default_factory=WorkScope)
    revision: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.coding-repository.v1",
            "repository_id": self.repository_id,
            "root": self.root,
            "git_dir": self.git_dir,
            "common_dir": self.common_dir,
            "object_format": self.object_format,
            "default_branch": self.default_branch or None,
            "head_oid": self.head_oid or None,
            "branch": self.branch or None,
            "remotes": [json_value(item) for item in self.remotes],
            "scope": self.scope.to_dict(include_empty=False),
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_error": self.last_error or None,
        }


@dataclass(frozen=True, slots=True)
class StatusEntry:
    path: str
    record_type: str
    index_status: str = "."
    worktree_status: str = "."
    original_path: str = ""
    submodule: str = ""
    head_mode: str = ""
    index_mode: str = ""
    worktree_mode: str = ""
    head_oid: str = ""
    index_oid: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "record_type": self.record_type,
            "index_status": self.index_status,
            "worktree_status": self.worktree_status,
            "original_path": self.original_path or None,
            "submodule": self.submodule or None,
            "head_mode": self.head_mode or None,
            "index_mode": self.index_mode or None,
            "worktree_mode": self.worktree_mode or None,
            "head_oid": self.head_oid or None,
            "index_oid": self.index_oid or None,
        }


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    repository_id: str
    root: str
    head_oid: str
    branch: str
    upstream: str
    ahead: int
    behind: int
    entries: tuple[StatusEntry, ...]
    fingerprint: str
    observed_at: float

    @property
    def dirty(self) -> bool:
        return bool(self.entries)

    @property
    def conflicted(self) -> bool:
        return any(item.record_type == "unmerged" for item in self.entries)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.git-status.v1",
            "repository_id": self.repository_id,
            "root": self.root,
            "head_oid": self.head_oid or None,
            "branch": self.branch or None,
            "upstream": self.upstream or None,
            "ahead": self.ahead,
            "behind": self.behind,
            "dirty": self.dirty,
            "conflicted": self.conflicted,
            "fingerprint": self.fingerprint,
            "observed_at": self.observed_at,
            "entries": [item.to_dict() for item in self.entries],
        }


@dataclass(frozen=True, slots=True)
class BranchRecord:
    name: str
    full_name: str
    oid: str
    current: bool = False
    remote: bool = False
    upstream: str = ""
    upstream_track: str = ""
    subject: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "full_name": self.full_name,
            "oid": self.oid,
            "current": self.current,
            "remote": self.remote,
            "upstream": self.upstream or None,
            "upstream_track": self.upstream_track or None,
            "subject": self.subject,
        }


@dataclass(frozen=True, slots=True)
class CommitRecord:
    oid: str
    parents: tuple[str, ...]
    author_name: str
    author_email: str
    authored_at: int
    committed_at: int
    subject: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "oid": self.oid,
            "parents": list(self.parents),
            "author": {"name": self.author_name, "email": self.author_email},
            "authored_at": self.authored_at,
            "committed_at": self.committed_at,
            "subject": self.subject,
        }


@dataclass(frozen=True, slots=True)
class DiffFile:
    path: str
    original_path: str
    status: str
    score: int
    old_mode: str
    new_mode: str
    old_oid: str
    new_oid: str
    additions: int | None
    deletions: int | None
    binary: bool
    content_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "original_path": self.original_path or None,
            "status": self.status,
            "score": self.score or None,
            "old_mode": self.old_mode or None,
            "new_mode": self.new_mode or None,
            "old_oid": self.old_oid or None,
            "new_oid": self.new_oid or None,
            "additions": self.additions,
            "deletions": self.deletions,
            "binary": self.binary,
            "content_ref": self.content_ref or None,
        }


@dataclass(frozen=True, slots=True)
class DiffSnapshot:
    repository_id: str
    root: str
    target: str
    base_ref: str
    base_oid: str
    head_ref: str
    head_oid: str
    status_fingerprint: str
    patch_ref: str
    patch_sha256: str
    patch_bytes: int
    files: tuple[DiffFile, ...]
    observed_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.git-diff.v1",
            "repository_id": self.repository_id,
            "root": self.root,
            "target": self.target,
            "base_ref": self.base_ref or None,
            "base_oid": self.base_oid or None,
            "head_ref": self.head_ref or None,
            "head_oid": self.head_oid or None,
            "status_fingerprint": self.status_fingerprint,
            "patch_ref": self.patch_ref or None,
            "patch_sha256": self.patch_sha256,
            "patch_bytes": self.patch_bytes,
            "observed_at": self.observed_at,
            "files": [item.to_dict() for item in self.files],
        }


@dataclass(frozen=True, slots=True)
class WorktreeRecord:
    worktree_id: str
    repository_id: str
    root: str
    branch: str
    base_ref: str
    base_oid: str
    head_oid: str
    purpose: str
    state: str
    dirty: bool
    conflicted: bool
    scope: WorkScope
    lease_owner: str
    lease_expires_at: float
    revision: int
    created_at: float
    updated_at: float
    retired_at: float = 0.0
    last_error: str = ""

    def __post_init__(self) -> None:
        if self.state not in WORKTREE_STATES:
            raise CodingValidationError(f"invalid worktree state: {self.state}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.coding-worktree.v1",
            "worktree_id": self.worktree_id,
            "repository_id": self.repository_id,
            "root": self.root,
            "branch": self.branch or None,
            "base_ref": self.base_ref,
            "base_oid": self.base_oid,
            "head_oid": self.head_oid,
            "purpose": self.purpose,
            "state": self.state,
            "dirty": self.dirty,
            "conflicted": self.conflicted,
            "scope": self.scope.to_dict(include_empty=False),
            "lease_owner": self.lease_owner or None,
            "lease_expires_at": self.lease_expires_at or None,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "retired_at": self.retired_at or None,
            "last_error": self.last_error or None,
        }


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    review_id: str
    repository_id: str
    worktree_id: str
    root: str
    target: str
    base_ref: str
    base_oid: str
    head_ref: str
    head_oid: str
    status_fingerprint: str
    patch_ref: str
    patch_sha256: str
    summary: dict[str, Any]
    state: str
    approved_head_oid: str
    scope: WorkScope
    revision: int
    created_at: float
    updated_at: float

    def __post_init__(self) -> None:
        if self.state not in REVIEW_STATES:
            raise CodingValidationError(f"invalid review state: {self.state}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.coding-review.v1",
            "review_id": self.review_id,
            "repository_id": self.repository_id,
            "worktree_id": self.worktree_id or None,
            "root": self.root,
            "target": self.target,
            "base_ref": self.base_ref or None,
            "base_oid": self.base_oid or None,
            "head_ref": self.head_ref or None,
            "head_oid": self.head_oid or None,
            "status_fingerprint": self.status_fingerprint,
            "patch_ref": self.patch_ref or None,
            "patch_sha256": self.patch_sha256,
            "summary": json_value(self.summary),
            "state": self.state,
            "approved_head_oid": self.approved_head_oid or None,
            "scope": self.scope.to_dict(include_empty=False),
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class ReviewFile:
    review_id: str
    path: str
    original_path: str
    status: str
    score: int
    old_mode: str
    new_mode: str
    old_oid: str
    new_oid: str
    additions: int | None
    deletions: int | None
    binary: bool
    patch_ref: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "path": self.path,
            "original_path": self.original_path or None,
            "status": self.status,
            "score": self.score or None,
            "old_mode": self.old_mode or None,
            "new_mode": self.new_mode or None,
            "old_oid": self.old_oid or None,
            "new_oid": self.new_oid or None,
            "additions": self.additions,
            "deletions": self.deletions,
            "binary": self.binary,
            "patch_ref": self.patch_ref or None,
        }


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    finding_id: str
    review_id: str
    path: str
    line: int
    side: str
    severity: str
    title: str
    body: str
    status: str
    scope: WorkScope
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "review_id": self.review_id,
            "path": self.path,
            "line": self.line or None,
            "side": self.side,
            "severity": self.severity,
            "title": self.title,
            "body": self.body,
            "status": self.status,
            "scope": self.scope.to_dict(include_empty=False),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class CheckRun:
    check_run_id: str
    review_id: str
    recipe: str
    argv: tuple[str, ...]
    state: str
    exit_code: int | None
    head_oid: str
    status_fingerprint: str
    log_ref: str
    log_sha256: str
    duration_ms: float
    scope: WorkScope
    revision: int
    created_at: float
    started_at: float
    completed_at: float
    diagnostic: str = ""

    def __post_init__(self) -> None:
        if self.state not in CHECK_STATES:
            raise CodingValidationError(f"invalid check state: {self.state}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.coding-check-run.v1",
            "check_run_id": self.check_run_id,
            "review_id": self.review_id,
            "recipe": self.recipe,
            "argv": list(self.argv),
            "state": self.state,
            "exit_code": self.exit_code,
            "head_oid": self.head_oid,
            "status_fingerprint": self.status_fingerprint,
            "log_ref": self.log_ref or None,
            "log_sha256": self.log_sha256,
            "duration_ms": self.duration_ms,
            "scope": self.scope.to_dict(include_empty=False),
            "revision": self.revision,
            "created_at": self.created_at,
            "started_at": self.started_at or None,
            "completed_at": self.completed_at or None,
            "diagnostic": self.diagnostic or None,
        }


__all__ = [
    "BranchRecord",
    "CHECK_STATES",
    "CheckRun",
    "CodingConflict",
    "CodingError",
    "CodingValidationError",
    "CommitRecord",
    "DiffFile",
    "DiffSnapshot",
    "GitCommandError",
    "GitUnavailable",
    "REVIEW_STATES",
    "RepositoryNotFound",
    "RepositoryRecord",
    "ReviewFinding",
    "ReviewNotFound",
    "ReviewRecord",
    "ReviewStale",
    "ReviewFile",
    "ScopeMismatch",
    "StatusEntry",
    "StatusSnapshot",
    "UnsafeWorktreeRemoval",
    "WORKTREE_STATES",
    "WorktreeNotFound",
    "WorktreeRecord",
    "json_value",
]
