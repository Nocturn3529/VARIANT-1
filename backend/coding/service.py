"""Composition surface for VARIANT-1's repository, worktree, and Review core.

Ordinary model-driven Git remains Python/shell composition through
``run_command``. This runtime retains only the typed internals that provide
durable repository discovery, managed worktrees, review, and check semantics.
"""

from __future__ import annotations

import os
from typing import Any, Mapping, Sequence

from work_fabric.scope import WorkScope, coerce_work_scope

from .checks import CheckRecipe, CheckService
from .git_process import GitProcess
from .models import GitCommandError
from .repository import CodingRepository
from .repository_registry import RepositoryRegistry
from .review import ReviewService
from .worktrees import WorktreeManager


class CodingRuntime:
    """Concrete internal service graph installed by host composition."""

    def __init__(
        self,
        *,
        repository: CodingRepository,
        process: GitProcess,
        repositories: RepositoryRegistry,
        worktrees: WorktreeManager,
        review: ReviewService,
        checks: CheckService,
    ) -> None:
        self.repository = repository
        self.process = process
        self.repositories = repositories
        self.worktrees = worktrees
        self.review = review
        self.checks = checks

    def begin_review_observation(
        self,
        path: str,
        *,
        scope: WorkScope | Mapping[str, Any] | None,
        admitted_root: str = "",
        timeout_s: float | None = None,
    ) -> dict[str, Any] | None:
        """Capture one exact Git fingerprint before an original seed runs."""

        resolved = coerce_work_scope(scope)
        if resolved.empty:
            return None
        try:
            worktree_id = str(resolved.worktree_id or "")
            if worktree_id:
                worktree = self.repository.get_worktree(worktree_id)
                self.worktrees._assert_scope(worktree, resolved, mutation=False)
                self.worktrees._verify_actual(worktree)
                repository_id = worktree.repository_id
                root = worktree.root
            else:
                repository = self.repositories.discover(
                    path or ".",
                    scope=resolved,
                    timeout_s=timeout_s,
                )
                repository_id = repository.repository_id
                root = repository.root
            allowed_root = os.path.realpath(os.path.abspath(
                str(admitted_root or "")
            )) if admitted_root else ""
            if (
                allowed_root
                and os.path.normcase(os.path.realpath(root))
                != os.path.normcase(allowed_root)
            ):
                return None
            status = self.process.status(
                root,
                repository_id=repository_id,
                timeout_s=timeout_s,
            )
            return self._review_observation_token(
                repository_id=repository_id,
                worktree_id=worktree_id,
                root=root,
                status=status,
                scope=resolved,
            )
        except GitCommandError:
            return None

    @staticmethod
    def _review_observation_token(
        *,
        repository_id: str,
        worktree_id: str,
        root: str,
        status: Any,
        scope: WorkScope,
    ) -> dict[str, Any]:
        return {
            "repository_id": str(repository_id),
            "worktree_id": str(worktree_id),
            "root": str(root),
            "head_oid": str(status.head_oid or ""),
            "status_fingerprint": str(status.fingerprint or ""),
            "scope": scope.to_dict(),
        }

    def finish_review_observation_detailed(
        self,
        token: Mapping[str, Any] | None,
        *,
        tool: str,
        arguments: Mapping[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Return the optional Review plus the next reusable observation token."""

        if not isinstance(token, Mapping):
            return {"review": None, "token": None}
        repository_id = str(token.get("repository_id") or "")
        root = str(token.get("root") or "")
        worktree_id = str(token.get("worktree_id") or "")
        scope = WorkScope.from_mapping(token.get("scope") or {})
        current = self.process.status(
            root,
            repository_id=repository_id,
            timeout_s=timeout_s,
        )
        next_token = self._review_observation_token(
            repository_id=repository_id,
            worktree_id=worktree_id,
            root=root,
            status=current,
            scope=scope,
        )
        if (
            current.head_oid == str(token.get("head_oid") or "")
            and current.fingerprint == str(token.get("status_fingerprint") or "")
        ):
            return {"review": None, "token": next_token}
        paths: list[str] = []
        if str(tool or "") == "apply_patch":
            for change in list((arguments or {}).get("changes") or ()):
                if not isinstance(change, Mapping):
                    continue
                candidate = os.path.realpath(os.path.abspath(
                    os.path.expandvars(str(change.get("path") or ""))
                ))
                try:
                    relative = os.path.relpath(candidate, root).replace("\\", "/")
                except ValueError:
                    continue
                if relative and relative != ".." and not relative.startswith("../"):
                    paths.append(relative)
        before_head = str(token.get("head_oid") or "")
        target = (
            "working_since"
            if before_head and current.head_oid != before_head
            else "working"
        )
        review = self.review.start(
            repository_id,
            target=target,
            base=(before_head if target == "working_since" else ""),
            paths=tuple(dict.fromkeys(paths)),
            worktree_id=worktree_id,
            scope=scope,
            origin=str(tool or "seed"),
        )
        return {"review": review, "token": next_token}

    def finish_review_observation(
        self,
        token: Mapping[str, Any] | None,
        *,
        tool: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> Any | None:
        """Create one frozen Review only when an original seed changed Git."""
        return self.finish_review_observation_detailed(
            token,
            tool=tool,
            arguments=arguments,
        )["review"]

    def startup(self) -> dict[str, Any]:
        reconciled = 0
        checks_reconciled = 0
        errors: list[dict[str, str]] = []
        for check in self.repository.list_running_checks():
            try:
                self.repository.finish_check(
                    check.check_run_id,
                    state="unknown_effect",
                    exit_code=None,
                    log_ref=check.log_ref,
                    log_sha256=check.log_sha256,
                    duration_ms=check.duration_ms,
                    diagnostic="check interrupted before a terminal receipt",
                )
                checks_reconciled += 1
            except Exception as exc:
                errors.append({
                    "check_run_id": check.check_run_id,
                    "error": str(exc),
                })
        for repository in self.repositories.list():
            try:
                reconciled += len(self.worktrees.reconcile(repository.repository_id))
            except Exception as exc:
                errors.append({
                    "repository_id": repository.repository_id,
                    "error": str(exc),
                })
        return {
            "repositories": len(self.repositories.list()),
            "worktrees_reconciled": reconciled,
            "checks_reconciled": checks_reconciled,
            "errors": errors,
        }

    def shutdown(self) -> None:
        return None

    def delete_chat(self, chat_id: str) -> dict[str, int]:
        """Retire safe managed coding state owned by one hard-deleted chat."""

        scope = WorkScope(chat_id=str(chat_id or ""))
        checks = 0
        worktrees = 0
        for check in self.repository.list_running_checks():
            if check.scope.chat_id != scope.chat_id:
                continue
            self.repository.finish_check(
                check.check_run_id,
                state="cancelled",
                exit_code=None,
                log_ref=check.log_ref,
                log_sha256=check.log_sha256,
                duration_ms=check.duration_ms,
                diagnostic="chat deleted",
            )
            checks += 1
        for worktree in self.worktrees.list(scope=scope):
            if worktree.state in {"retired", "retiring"}:
                continue
            try:
                self.worktrees.remove(
                    worktree.worktree_id,
                    scope=scope,
                    expected_revision=worktree.revision,
                    expected_head_oid=worktree.head_oid,
                    allow_unpushed=True,
                    idempotency_key=f"chat-delete:{scope.chat_id}:{worktree.worktree_id}",
                )
                worktrees += 1
            except Exception:
                # Dirty/conflicted worktrees are user evidence. Hard chat
                # deletion cancels their workers but never destroys that data.
                continue
        return {"checks_cancelled": checks, "worktrees_retired": worktrees}

    def stop_chat(self, chat_id: str) -> dict[str, int]:
        """Terminalize live coding work while preserving recoverable worktrees."""

        scope = WorkScope(chat_id=str(chat_id or ""))
        checks = 0
        for check in self.repository.list_running_checks():
            if check.scope.chat_id != scope.chat_id:
                continue
            self.repository.finish_check(
                check.check_run_id,
                state="cancelled",
                exit_code=None,
                log_ref=check.log_ref,
                log_sha256=check.log_sha256,
                duration_ms=check.duration_ms,
                diagnostic="chat owner tombstoned",
            )
            checks += 1
        return {"checks_cancelled": checks, "worktrees_preserved": len(
            self.worktrees.list(scope=scope)
        )}

    def health(self) -> dict[str, Any]:
        probe = self.process.run(".", ["--version"], check=False)
        return {
            "status": "ok" if probe.returncode == 0 else "degraded",
            "git": probe.text,
            "database": self.repository.path,
            "managed_root": self.worktrees.managed_root,
            "repositories": len(self.repositories.list()),
            "worktrees": len(self.worktrees.list(include_retired=False)),
        }


def create_coding_runtime(
    *,
    path: str | None = None,
    data_dir: str | None = None,
    managed_root: str | None = None,
    artifact_store: Any | None = None,
    git_executable: str | None = None,
    git_timeout_s: float = 30.0,
    recipes: Mapping[str, CheckRecipe | Sequence[str]] | None = None,
    process_owned: Any | None = None,
    process_service: Any | None = None,
) -> CodingRuntime:
    repository = CodingRepository(path=path, data_dir=data_dir)
    process = GitProcess(git_executable, timeout_s=git_timeout_s)
    repositories = RepositoryRegistry(repository, process)
    worktrees = WorktreeManager(
        repository,
        process,
        managed_root=managed_root,
        data_dir=data_dir,
        process_owned=process_owned,
    )
    review = ReviewService(
        repository, process, worktrees, artifact_store=artifact_store
    )
    checks = CheckService(
        repository,
        review,
        artifact_store=artifact_store,
        recipes=recipes,
        process_service=process_service,
    )
    return CodingRuntime(
        repository=repository,
        process=process,
        repositories=repositories,
        worktrees=worktrees,
        review=review,
        checks=checks,
    )


__all__ = ["CodingRuntime", "create_coding_runtime"]
