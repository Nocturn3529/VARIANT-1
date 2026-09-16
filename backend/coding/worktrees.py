"""Managed Git worktree lifecycle with non-destructive recovery semantics."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import threading
import time
import uuid
from typing import Any, Callable, Mapping

from core_invariants import request_fingerprint
from work_fabric.scope import WorkScope, coerce_work_scope, work_scope_visible

from .git_process import GitProcess, WorktreeObservation
from .models import (
    CodingConflict,
    CodingValidationError,
    ScopeMismatch,
    UnsafeWorktreeRemoval,
    WorktreeNotFound,
    WorktreeRecord,
)
from .repository import CodingRepository, default_managed_root, path_key


_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,80}$")
_LOCKS_GUARD = threading.Lock()
_REPOSITORY_LOCKS: dict[str, threading.RLock] = {}


def repository_mutation_lock(repository_id: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _REPOSITORY_LOCKS.setdefault(repository_id, threading.RLock())


def _owner(scope: WorkScope) -> tuple[str, str]:
    for kind, value in (
        ("step", scope.step_id),
        ("goal_run", scope.goal_run_id),
        ("goal", scope.goal_id),
        ("chat", scope.chat_id),
        ("workspace", scope.workspace_id),
    ):
        if value:
            return kind, value
    raise ScopeMismatch("managed worktree mutations require an owning WorkScope")


def _slug(value: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "-", str(value or "coding").lower()).strip("-")
    return (clean or "coding")[:40]


def _has_reparse_attribute(path: str) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse)


class WorktreeManager:
    """Own physical paths beneath one pinned managed-root authority."""

    def __init__(
        self,
        repository: CodingRepository,
        git: GitProcess,
        *,
        managed_root: str | None = None,
        data_dir: str | None = None,
        process_owned: Callable[[str], bool] | None = None,
    ) -> None:
        requested = os.path.abspath(managed_root or default_managed_root(data_dir=data_dir))
        os.makedirs(requested, exist_ok=True)
        self.managed_root = os.path.realpath(requested)
        self.repository = repository
        self.git = git
        self.process_owned = process_owned

    def _target(self, repository_id: str, worktree_id: str) -> str:
        if not _ID_RE.fullmatch(repository_id) or not _ID_RE.fullmatch(worktree_id):
            raise CodingValidationError("invalid repository or worktree identifier")
        target = os.path.abspath(os.path.join(self.managed_root, repository_id, worktree_id))
        try:
            common = os.path.commonpath([self.managed_root, target])
        except ValueError as exc:
            raise CodingValidationError("managed worktree path is on a different volume") from exc
        if path_key(common) != path_key(self.managed_root) or path_key(target) == path_key(self.managed_root):
            raise CodingValidationError("managed worktree path escapes the managed root")
        expected_parent = os.path.abspath(os.path.join(self.managed_root, repository_id))
        if path_key(os.path.dirname(target)) != path_key(expected_parent):
            raise CodingValidationError("managed worktree path has an unexpected shape")
        return target

    def _validate_path(self, record: WorktreeRecord, *, must_exist: bool) -> str:
        expected = self._target(record.repository_id, record.worktree_id)
        if path_key(record.root) != path_key(expected):
            raise CodingValidationError("persisted worktree root is outside its managed slot")
        relative = os.path.relpath(expected, self.managed_root)
        cursor = self.managed_root
        parts = relative.split(os.sep)
        for part in parts:
            cursor = os.path.join(cursor, part)
            if os.path.lexists(cursor) and _has_reparse_attribute(cursor):
                raise CodingValidationError("managed worktree path contains a reparse point")
        if must_exist and not os.path.isdir(expected):
            raise CodingValidationError("managed worktree root is missing")
        return expected

    @staticmethod
    def _assert_scope(record: WorktreeRecord, scope: WorkScope, *, mutation: bool) -> None:
        if scope.worktree_id and scope.worktree_id != record.worktree_id:
            raise ScopeMismatch("worktree is outside the current WorkScope")
        owner = record.scope.with_updates(worktree_id="")
        if not work_scope_visible(owner, scope):
            raise ScopeMismatch("worktree does not match the current WorkScope")
        if mutation:
            _owner(scope)

    def _verify_actual(
        self,
        record: WorktreeRecord,
        actual: WorktreeObservation | None = None,
    ) -> tuple[WorktreeObservation, str]:
        root = self._validate_path(record, must_exist=True)
        repo = self.repository.get_repository(record.repository_id)
        matching = actual
        if matching is None:
            observations = {path_key(item.root): item for item in self.git.worktrees(repo.root)}
            matching = observations.get(path_key(root))
        if matching is None:
            raise CodingValidationError("Git does not register the managed worktree")
        discovered = self.git.repository(root)
        if path_key(discovered.common_dir) != path_key(repo.common_dir):
            raise CodingValidationError("managed root resolves to a different Git repository")
        head_oid = self.git.resolve(root, "HEAD", allow_missing=True)
        if matching.head_oid and head_oid and matching.head_oid != head_oid:
            raise CodingConflict("Git worktree head observation is inconsistent")
        return matching, head_oid

    def _settle_reconciled_operations(self, record: WorktreeRecord) -> None:
        for operation in self.repository.list_operations(
            repository_id=record.repository_id,
            worktree_id=record.worktree_id,
            limit=50,
        ):
            if operation["state"] not in {"running", "unknown_effect"}:
                continue
            if operation["kind"] == "worktree.create" and record.state in {
                "active", "detached"
            }:
                self.repository.finish_operation(
                    operation["operation_id"],
                    state="succeeded",
                    after_oid=record.head_oid,
                    result={"worktree_id": record.worktree_id, "root": record.root},
                )
            elif operation["kind"] == "worktree.remove" and record.state == "retired":
                self.repository.finish_operation(
                    operation["operation_id"],
                    state="succeeded",
                    after_oid=record.head_oid,
                    result={
                        "worktree_id": record.worktree_id,
                        "branch_retained": record.branch,
                        "reconciled": True,
                    },
                )

    def create(
        self,
        repository_id: str,
        *,
        base_ref: str = "HEAD",
        branch: str = "",
        purpose: str = "coding",
        scope: WorkScope | Mapping[str, Any] | None,
        expected_repository_revision: int | None = None,
        expected_head_oid: str = "",
        idempotency_key: str = "",
        detach: bool = False,
        lease_owner: str = "",
        lease_seconds: float = 0.0,
    ) -> WorktreeRecord:
        resolved_scope = coerce_work_scope(scope)
        _owner(resolved_scope)
        if resolved_scope.worktree_id:
            raise ScopeMismatch("a new worktree cannot reuse a scoped worktree_id")
        repo = self.repository.get_repository(repository_id)
        if expected_repository_revision is not None and repo.revision != int(expected_repository_revision):
            raise CodingConflict("repository revision changed")
        if expected_head_oid and repo.head_oid != str(expected_head_oid):
            raise CodingConflict("repository head changed")
        selected_base = str(base_ref or "HEAD").strip()
        key = str(idempotency_key or "").strip()
        worktree_id = (
            "wt_" + hashlib.sha256(f"{repository_id}\0{key}".encode("utf-8")).hexdigest()[:24]
            if key else "wt_" + uuid.uuid4().hex
        )
        selected_branch = "" if detach else str(branch or "").strip()
        if not detach and not selected_branch:
            selected_branch = f"variant1/{_slug(purpose or resolved_scope.goal_id)}-{worktree_id[-8:]}"

        with repository_mutation_lock(repository_id):
            # Re-observe mutable refs only while holding the repository mutation lock.
            base_oid = self.git.resolve(repo.root, selected_base)
            live_head = self.git.resolve(repo.root, "HEAD", allow_missing=True)
            if expected_head_oid and live_head != str(expected_head_oid):
                raise CodingConflict("repository head changed before worktree creation")
            if selected_branch:
                if selected_branch.startswith("-"):
                    raise CodingValidationError("worktree branch cannot begin with a dash")
                self.git.run(repo.root, ["check-ref-format", "--branch", selected_branch])
            request = {
                "base_ref": selected_base,
                "base_oid": base_oid,
                "branch": selected_branch,
                "detach": bool(detach),
                "purpose": str(purpose or "coding"),
                "scope": resolved_scope.to_dict(),
            }
            target = self._target(repository_id, worktree_id)
            operation, replay = self.repository.reserve_operation(
                repository_id=repository_id,
                worktree_id=worktree_id,
                kind="worktree.create",
                idempotency_key=key,
                request_fingerprint=request_fingerprint(
                    "coding.worktree.create", request
                ),
                before_oid=live_head,
                scope=resolved_scope,
            )
            record: WorktreeRecord | None = None
            if replay:
                prior_id = str((operation.get("result") or {}).get("worktree_id") or worktree_id)
                try:
                    prior = self.repository.get_worktree(prior_id)
                except WorktreeNotFound:
                    prior = None
                if (
                    operation["state"] == "succeeded"
                    and prior is not None
                    and prior.state in {"active", "detached"}
                ):
                    return prior
                actual = {
                    path_key(item.root): item for item in self.git.worktrees(repo.root)
                }.get(path_key(target))
                if actual is not None or os.path.lexists(target):
                    reconciled = {
                        item.worktree_id: item for item in self.reconcile(repository_id)
                    }
                    prior = reconciled.get(prior_id, prior)
                    if prior is not None and prior.state in {"active", "detached"}:
                        self.repository.finish_operation(
                            operation["operation_id"], state="succeeded",
                            after_oid=prior.head_oid,
                            result={"worktree_id": prior.worktree_id, "root": prior.root},
                        )
                        return prior
                    raise CodingConflict("prior worktree creation has an ambiguous physical effect")
                if operation["state"] != "running":
                    raise CodingConflict("prior worktree creation requires reconciliation")
                if prior is not None:
                    if prior.state == "orphaned":
                        prior = self.repository.transition_worktree(
                            prior.worktree_id,
                            expected_revision=prior.revision,
                            allowed_states=("orphaned",),
                            state="creating",
                        )
                    if prior.state != "creating":
                        raise CodingConflict("prior worktree reservation is not resumable")
                    record = prior

            try:
                container = os.path.dirname(target)
                os.makedirs(container, exist_ok=True)
                if _has_reparse_attribute(container):
                    raise CodingValidationError(
                        "managed repository directory is a reparse point"
                    )
                if os.path.lexists(target):
                    raise CodingConflict("managed worktree target already exists")
                record_scope = resolved_scope.with_updates(worktree_id=worktree_id)
                if record is None:
                    record = self.repository.create_worktree_reservation(
                        worktree_id=worktree_id,
                        repository_id=repository_id,
                        root=target,
                        branch=selected_branch,
                        base_ref=selected_base,
                        base_oid=base_oid,
                        purpose=str(purpose or "coding")[:200],
                        scope=record_scope,
                        lease_owner=str(lease_owner or "")[:512],
                        lease_expires_at=(
                            time.time() + float(lease_seconds) if lease_seconds > 0 else 0.0
                        ),
                    )
            except BaseException as exc:
                self.repository.finish_operation(
                    operation["operation_id"], state="failed",
                    diagnostic=str(exc)[:4000],
                )
                raise
            assert record is not None
            try:
                arguments = ["worktree", "add"]
                if detach:
                    arguments.append("--detach")
                else:
                    arguments.extend(["-b", selected_branch])
                arguments.extend([target, base_oid])
                self.git.run(repo.root, arguments, mutating=True, timeout_s=120.0)
                observation, head_oid = self._verify_actual(record)
                if head_oid != base_oid:
                    raise CodingConflict("created worktree does not point at the resolved base OID")
                state = "detached" if observation.detached else "active"
                record = self.repository.activate_worktree(
                    worktree_id,
                    expected_revision=record.revision,
                    allowed_states=("creating",),
                    state=state,
                    head_oid=head_oid,
                    branch=("" if observation.detached else selected_branch),
                    dirty=False,
                    conflicted=False,
                    scope=record_scope,
                )
                self.repository.finish_operation(
                    operation["operation_id"], state="succeeded", after_oid=head_oid,
                    result={"worktree_id": worktree_id, "root": target},
                )
                return record
            except BaseException as exc:
                uncertain = os.path.lexists(target)
                try:
                    uncertain = uncertain or any(
                        path_key(item.root) == path_key(target)
                        for item in self.git.worktrees(repo.root)
                    )
                except Exception:
                    uncertain = True
                try:
                    current = self.repository.get_worktree(worktree_id)
                    if current.state == "creating":
                        self.repository.transition_worktree(
                            worktree_id,
                            expected_revision=current.revision,
                            allowed_states=("creating",),
                            state="orphaned",
                            last_error=str(exc)[:2000],
                        )
                finally:
                    self.repository.finish_operation(
                        operation["operation_id"],
                        state=("unknown_effect" if uncertain else "failed"),
                        diagnostic=str(exc)[:4000], result={"worktree_id": worktree_id},
                    )
                raise

    def get(self, worktree_id: str) -> WorktreeRecord:
        return self.repository.get_worktree(worktree_id)

    def list(
        self,
        repository_id: str = "",
        *,
        include_retired: bool = False,
        scope: WorkScope | Mapping[str, Any] | None = None,
    ) -> tuple[WorktreeRecord, ...]:
        return self.repository.list_worktrees(
            repository_id=repository_id,
            include_retired=include_retired,
            scope=scope,
        )

    def _unpushed(self, record: WorktreeRecord) -> bool:
        if not record.base_oid:
            return False
        current_head = self.git.resolve(record.root, "HEAD", allow_missing=True)
        if not current_head:
            return False
        if self.git.unique_commit_count(record.root, record.base_oid, current_head) <= 0:
            return False
        upstream = self.git.resolve(record.root, "@{upstream}", allow_missing=True)
        if not upstream:
            return True
        return not self.git.is_ancestor(record.root, current_head, upstream)

    def remove(
        self,
        worktree_id: str,
        *,
        scope: WorkScope | Mapping[str, Any] | None,
        expected_revision: int,
        expected_head_oid: str = "",
        allow_unpushed: bool = False,
        idempotency_key: str = "",
    ) -> WorktreeRecord:
        record = self.repository.get_worktree(worktree_id)
        resolved = coerce_work_scope(scope)
        self._assert_scope(record, resolved, mutation=True)
        request = {
            "worktree_id": worktree_id,
            "revision": int(expected_revision),
            "expected_head_oid": str(expected_head_oid or ""),
            "allow_unpushed": bool(allow_unpushed),
        }
        operation_fingerprint = request_fingerprint(
            "coding.worktree.remove", request
        )
        prior = self.repository.get_operation_by_key(
            record.repository_id, str(idempotency_key or "")
        )
        resumed_operation: dict[str, Any] | None = None
        if prior is not None:
            if prior["request_fingerprint"] != operation_fingerprint:
                raise CodingConflict("idempotency key was used for a different operation")
            if prior["state"] == "succeeded" and record.state == "retired":
                return record
            if (
                prior["state"] in {"running", "unknown_effect"}
                and record.state == "retiring"
            ):
                if not os.path.lexists(record.root):
                    self.reconcile(record.repository_id)
                    retired = self.repository.get_worktree(worktree_id)
                    if retired.state == "retired":
                        return retired
                resumed_operation = prior
            else:
                raise CodingConflict("prior worktree removal requires reconciliation")
        if resumed_operation is None and record.revision != int(expected_revision):
            raise CodingConflict("worktree revision changed")
        repo = self.repository.get_repository(record.repository_id)
        with repository_mutation_lock(record.repository_id):
            root = self._validate_path(record, must_exist=True)
            observation, head_oid = self._verify_actual(record)
            if expected_head_oid and head_oid != str(expected_head_oid):
                raise CodingConflict("worktree head changed")
            status = self.git.status(root, repository_id=record.repository_id)
            blockers: list[str] = []
            if status.dirty:
                blockers.append("dirty")
            if status.conflicted:
                blockers.append("conflicted")
            if observation.locked:
                blockers.append("git-locked")
            if self.process_owned is not None and self.process_owned(worktree_id):
                blockers.append("process-owned")
            if not allow_unpushed and self._unpushed(record):
                blockers.append("unpushed")
            if blockers:
                raise UnsafeWorktreeRemoval(
                    "worktree removal refused: " + ", ".join(sorted(set(blockers)))
                )
            if resumed_operation is None:
                operation, replay = self.repository.reserve_operation(
                    repository_id=record.repository_id,
                    worktree_id=worktree_id,
                    kind="worktree.remove",
                    idempotency_key=str(idempotency_key or ""),
                    request_fingerprint=operation_fingerprint,
                    before_oid=head_oid,
                    scope=resolved,
                )
                if replay:
                    raise CodingConflict("prior worktree removal requires reconciliation")
                retiring = self.repository.transition_worktree(
                    worktree_id,
                    expected_revision=record.revision,
                    allowed_states=("active", "detached", "orphaned"),
                    state="retiring",
                    head_oid=head_oid,
                    dirty=False,
                    conflicted=False,
                )
            else:
                operation = resumed_operation
                retiring = record
            try:
                # Git owns deletion.  There is deliberately no --force and no
                # recursive filesystem fallback.
                self.git.run(
                    repo.root,
                    ["worktree", "remove", "--", root],
                    mutating=True,
                    timeout_s=120.0,
                )
                if os.path.lexists(root):
                    raise CodingConflict("Git reported removal but the managed root still exists")
                retired = self.repository.retire_worktree(
                    worktree_id,
                    expected_revision=retiring.revision,
                    scope=resolved,
                )
                self.repository.finish_operation(
                    operation["operation_id"], state="succeeded", after_oid=head_oid,
                    result={"worktree_id": worktree_id, "branch_retained": record.branch},
                )
                return retired
            except BaseException as exc:
                uncertain = not os.path.lexists(root)
                if not uncertain:
                    try:
                        uncertain = not any(
                            path_key(item.root) == path_key(root)
                            for item in self.git.worktrees(repo.root)
                        )
                    except Exception:
                        uncertain = True
                self.repository.finish_operation(
                    operation["operation_id"],
                    state=("unknown_effect" if uncertain else "failed"),
                    after_oid=head_oid,
                    result={"worktree_id": worktree_id}, diagnostic=str(exc)[:4000],
                )
                # Keep `retiring` and the exact path so startup reconciliation
                # can distinguish applied from unapplied removal.
                raise

    def reconcile(self, repository_id: str) -> tuple[WorktreeRecord, ...]:
        repo = self.repository.get_repository(repository_id)
        with repository_mutation_lock(repository_id):
            actual = {path_key(item.root): item for item in self.git.worktrees(repo.root)}
            output: list[WorktreeRecord] = []
            for record in self.repository.list_worktrees(
                repository_id=repository_id, include_retired=True
            ):
                if record.state == "retired":
                    self._settle_reconciled_operations(record)
                    continue
                observed = actual.get(path_key(record.root))
                if observed is None:
                    if record.state == "retiring":
                        updated = self.repository.retire_worktree(
                            record.worktree_id,
                            expected_revision=record.revision,
                            scope=record.scope,
                        )
                    elif record.state != "orphaned":
                        updated = self.repository.transition_worktree(
                            record.worktree_id,
                            expected_revision=record.revision,
                            allowed_states=(record.state,),
                            state="orphaned",
                            last_error="Git worktree registry no longer contains the managed root",
                        )
                    else:
                        updated = record
                    self._settle_reconciled_operations(updated)
                    output.append(updated)
                    continue
                try:
                    observation, head_oid = self._verify_actual(record, observed)
                    status = self.git.status(record.root, repository_id=repository_id)
                    state = (
                        "retiring" if record.state == "retiring"
                        else "conflicted" if status.conflicted
                        else "detached" if observation.detached
                        else "active"
                    )
                    branch = (
                        "" if observation.detached
                        else observation.branch_ref.removeprefix("refs/heads/")
                    )
                    if record.state in {"creating", "orphaned"}:
                        updated = self.repository.activate_worktree(
                            record.worktree_id,
                            expected_revision=record.revision,
                            allowed_states=(record.state,),
                            state=state,
                            head_oid=head_oid,
                            branch=branch,
                            dirty=status.dirty,
                            conflicted=status.conflicted,
                            scope=record.scope,
                        )
                    elif (
                        record.state == state
                        and record.head_oid == head_oid
                        and record.branch == branch
                        and record.dirty == status.dirty
                        and record.conflicted == status.conflicted
                    ):
                        updated = record
                    else:
                        updated = self.repository.transition_worktree(
                            record.worktree_id,
                            expected_revision=record.revision,
                            allowed_states=(record.state,),
                            state=state,
                            head_oid=head_oid,
                            branch=branch,
                            dirty=status.dirty,
                            conflicted=status.conflicted,
                        )
                except Exception as exc:
                    updated = self.repository.transition_worktree(
                        record.worktree_id,
                        expected_revision=record.revision,
                        allowed_states=(record.state,),
                        state="orphaned",
                        last_error=str(exc)[:2000],
                    )
                self._settle_reconciled_operations(updated)
                output.append(updated)
            return tuple(output)


__all__ = ["WorktreeManager", "repository_mutation_lock"]
