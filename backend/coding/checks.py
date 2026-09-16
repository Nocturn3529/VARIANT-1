"""Registered argv-only check recipes tied to immutable review fingerprints."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import time
from typing import Any, Callable, Mapping, Sequence

from core_invariants import cancellation_is_requested
from execution_hosts import ExecutionOwner
from work_fabric.scope import WorkScope, coerce_work_scope

from .models import CheckRun, CodingValidationError, ScopeMismatch
from .repository import CodingRepository
from .review import ReviewService


@dataclass(frozen=True, slots=True)
class CheckRecipe:
    name: str
    argv: tuple[str, ...]
    timeout_s: float = 300.0
    environment: dict[str, str] = field(default_factory=dict)
    max_log_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        if not self.name or not self.argv:
            raise CodingValidationError("check recipe name and argv are required")
        if any(not isinstance(item, str) or "\x00" in item for item in self.argv):
            raise CodingValidationError("check recipe argv must contain valid strings")
        if not 0.1 <= float(self.timeout_s) <= 24 * 60 * 60:
            raise CodingValidationError("check timeout is outside the supported range")
        if not 1024 <= int(self.max_log_bytes) <= 512 * 1024 * 1024:
            raise CodingValidationError("check log bound is outside the supported range")


class CheckService:
    def __init__(
        self,
        repository: CodingRepository,
        reviews: ReviewService,
        *,
        artifact_store: Any | None = None,
        recipes: Mapping[str, CheckRecipe | Sequence[str]] | None = None,
        process_service: Any | None = None,
    ) -> None:
        self.repository = repository
        self.reviews = reviews
        self.artifact_store = artifact_store
        self.process_service = process_service
        self._recipes: dict[str, CheckRecipe] = {}
        for name, recipe in dict(recipes or {}).items():
            if isinstance(recipe, CheckRecipe):
                self.register(recipe)
            else:
                self.register(CheckRecipe(str(name), tuple(str(item) for item in recipe)))

    def register(self, recipe: CheckRecipe) -> None:
        if recipe.name in self._recipes:
            raise CodingValidationError(f"check recipe already exists: {recipe.name}")
        self._recipes[recipe.name] = recipe

    def recipes(self) -> tuple[dict[str, Any], ...]:
        return tuple({
            "name": item.name,
            "argv": list(item.argv),
            "timeout_s": item.timeout_s,
            "max_log_bytes": item.max_log_bytes,
        } for item in sorted(self._recipes.values(), key=lambda value: value.name))

    def run(
        self,
        review_id: str,
        recipe_name: str,
        *,
        scope: WorkScope | Mapping[str, Any] | None,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> CheckRun:
        resolved = coerce_work_scope(scope)
        if resolved.empty:
            raise ScopeMismatch("check execution requires a WorkScope")
        recipe = self._recipes.get(str(recipe_name or ""))
        if recipe is None:
            raise CodingValidationError(f"unknown check recipe: {recipe_name}")
        if self.process_service is None:
            raise CodingValidationError(
                "review checks require VARIANT-1's shared execution Process service"
            )
        review = self.reviews.assert_current(review_id, scope=resolved)
        check = self.repository.create_check(
            review_id=review_id,
            recipe=recipe.name,
            argv=recipe.argv,
            head_oid=review.head_oid,
            status_fingerprint=review.status_fingerprint,
            scope=resolved,
        )
        started = time.perf_counter()
        exit_code: int | None = None
        diagnostic = ""
        timed_out = False
        output = b""
        truncated = False
        cancelled = False
        stdout_data = b""
        stderr_data = b""
        execution_failed = False
        process_id = "proc_" + hashlib.sha256(
            f"coding-check\0{check.check_run_id}".encode("utf-8")
        ).hexdigest()

        def cancellation() -> bool:
            return cancellation_is_requested(cancellation_requested)

        environment = {
            str(key): str(value) for key, value in recipe.environment.items()
        }
        environment.setdefault("GIT_TERMINAL_PROMPT", "0")
        try:
            completed = self.process_service.run_bounded(
                recipe.argv,
                owner=ExecutionOwner("review_check", check.check_run_id, resolved),
                cwd=review.root,
                environment=environment,
                timeout=recipe.timeout_s,
                max_output_bytes=recipe.max_log_bytes,
                cancellation_requested=cancellation,
                process_id=process_id,
            )
            cancelled = completed.cancelled
            timed_out = completed.timed_out
            exit_code = (
                None if cancelled or timed_out else completed.process.exit_code
            )
            if cancelled:
                diagnostic = "check cancelled"
            elif timed_out:
                diagnostic = f"check timed out after {recipe.timeout_s:g} seconds"
            stdout_data = completed.stdout[:recipe.max_log_bytes]
            remaining = max(0, recipe.max_log_bytes - len(stdout_data))
            stderr_data = completed.stderr[:remaining]
            truncated = (
                completed.truncated
                or bool(completed.artifact_refs)
                or len(completed.stdout) > len(stdout_data)
                or len(completed.stderr) > len(stderr_data)
            )
            if completed.artifact_refs:
                refs = "\n".join(completed.artifact_refs).encode("utf-8")
                diagnostic = (
                    diagnostic + "; " if diagnostic else ""
                ) + "full output retained by the execution Process record"
                remaining = max(0, recipe.max_log_bytes - len(stdout_data) - len(stderr_data))
                if remaining:
                    stderr_data += b"\n[execution artifact segments]\n" + refs[:remaining]
        except Exception as exc:
            execution_failed = True
            diagnostic = f"check execution failed: {type(exc).__name__}: {exc}"
        output = (
            b"[stdout]\n" + stdout_data + b"\n[stderr]\n" + stderr_data
            + (b"\n[log truncated]\n" if truncated else b"")
        )
        duration_ms = (
            completed.duration_ms
            if "completed" in locals()
            else (time.perf_counter() - started) * 1000.0
        )
        log_sha = hashlib.sha256(output).hexdigest()
        log_ref = ""
        if self.artifact_store is not None:
            artifact = self.artifact_store.put_bytes(
                output,
                media_type="text/plain; charset=utf-8",
                kind="coding_check_log",
                scope=(resolved.chat_id or f"review:{review_id}"),
            )
            log_ref = str(artifact.ref)
        if execution_failed:
            state = "unknown_effect"
        elif cancelled:
            state = "cancelled"
        elif timed_out or exit_code is None:
            state = "failed"
        else:
            state = "passed" if exit_code == 0 else "failed"
            if exit_code and not diagnostic:
                diagnostic = f"check exited with status {exit_code}"
        if truncated:
            diagnostic = (diagnostic + "; " if diagnostic else "") + "log exceeded its bound"

        # A nominally passing command is evidence only for the exact snapshot
        # it began on.  Any mutation during the command stales the review.
        after = self.reviews.git.status(review.root, repository_id=review.repository_id)
        changed = (
            after.head_oid != review.head_oid
            or after.fingerprint != review.status_fingerprint
        )
        if changed:
            if state != "cancelled":
                state = "failed"
            diagnostic = (
                diagnostic + "; " if diagnostic else ""
            ) + "repository changed while the check was running"
            current_review = self.repository.get_review(review_id)
            if current_review.state in {"open", "approved"}:
                self.repository.transition_review(
                    review_id,
                    expected_revision=current_review.revision,
                    allowed_states=(current_review.state,),
                    state="stale",
                )
        return self.repository.finish_check(
            check.check_run_id,
            state=state,
            exit_code=exit_code,
            log_ref=log_ref,
            log_sha256=log_sha,
            duration_ms=duration_ms,
            diagnostic=diagnostic,
        )

    def run_many(
        self,
        review_id: str,
        recipe_names: Sequence[str],
        *,
        scope: WorkScope | Mapping[str, Any] | None,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> tuple[CheckRun, ...]:
        return tuple(
            self.run(
                review_id, name, scope=scope,
                cancellation_requested=cancellation_requested,
            )
            for name in recipe_names
        )

    def list(self, review_id: str) -> tuple[CheckRun, ...]:
        return self.repository.list_checks(review_id)


__all__ = ["CheckRecipe", "CheckService"]
