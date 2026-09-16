"""Repository-grounded immutable review snapshots and exact-head approval."""

from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from typing import Any, Mapping, Sequence

from core_invariants import request_fingerprint
from work_fabric.scope import WorkScope, coerce_work_scope, work_scope_visible

from .git_process import GitProcess
from .models import (
    CodingConflict,
    CodingValidationError,
    DiffFile,
    DiffSnapshot,
    ReviewFile,
    ReviewRecord,
    ReviewStale,
    ScopeMismatch,
)
from .repository import CodingRepository
from .worktrees import WorktreeManager, repository_mutation_lock


_TARGETS = frozenset({
    "working", "staged", "uncommitted", "base_branch",
    "branch", "commit", "range", "custom", "working_since",
})
_HUNK = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)
_MAX_PROJECTED_HUNK_LINES = 500


def _patch_hunks(payload: bytes) -> dict[str, list[dict[str, Any]]]:
    """Project immutable Git patch bytes into the Review panel line contract."""

    result: dict[str, list[dict[str, Any]]] = {}
    path = ""
    old_path = ""
    current: dict[str, Any] | None = None
    old_no = new_no = 0
    rendered = 0
    for raw in payload.decode("utf-8", errors="replace").splitlines():
        if raw.startswith("diff --git "):
            path = ""
            old_path = ""
            current = None
            rendered = 0
            continue
        if raw.startswith("--- "):
            value = raw[4:].strip()
            old_path = value[2:] if value.startswith("a/") else value
            if old_path == "/dev/null":
                old_path = ""
            continue
        if raw.startswith("+++ "):
            value = raw[4:].strip()
            path = value[2:] if value.startswith("b/") else value
            if path == "/dev/null":
                path = old_path
            continue
        match = _HUNK.match(raw)
        if match and path:
            old_no = int(match.group(1))
            new_no = int(match.group(3))
            current = {
                "oldStart": old_no,
                "oldLines": int(match.group(2) or 1),
                "newStart": new_no,
                "newLines": int(match.group(4) or 1),
                "lines": [],
            }
            result.setdefault(path, []).append(current)
            continue
        if current is None or rendered >= _MAX_PROJECTED_HUNK_LINES:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            current["lines"].append({
                "kind": "add", "text": raw[1:], "newNo": new_no,
            })
            new_no += 1
            rendered += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            current["lines"].append({
                "kind": "del", "text": raw[1:], "oldNo": old_no,
            })
            old_no += 1
            rendered += 1
        elif raw.startswith(" "):
            current["lines"].append({
                "kind": "ctx", "text": raw[1:],
                "oldNo": old_no, "newNo": new_no,
            })
            old_no += 1
            new_no += 1
            rendered += 1
    return result


class ReviewService:
    def __init__(
        self,
        repository: CodingRepository,
        git: GitProcess,
        worktrees: WorktreeManager,
        *,
        artifact_store: Any | None = None,
    ) -> None:
        self.repository = repository
        self.git = git
        self.worktrees = worktrees
        self.artifact_store = artifact_store

    @staticmethod
    def _scope(value: WorkScope | Mapping[str, Any] | None) -> WorkScope:
        scope = coerce_work_scope(value)
        if scope.empty:
            raise ScopeMismatch("review mutations require a WorkScope")
        return scope

    def _root(
        self,
        repository_id: str,
        worktree_id: str,
        scope: WorkScope,
    ) -> str:
        if not worktree_id:
            if scope.worktree_id:
                raise ScopeMismatch("review omitted the worktree pinned by WorkScope")
            return self.repository.get_repository(repository_id).root
        worktree = self.repository.get_worktree(worktree_id)
        if worktree.repository_id != repository_id:
            raise ScopeMismatch("worktree belongs to another repository")
        if scope.worktree_id and scope.worktree_id != worktree_id:
            raise ScopeMismatch("worktree is outside the current WorkScope")
        # Read-only reviewers may have a different chat/step identity.  The
        # exact worktree ID and Git OIDs remain the authority.
        self.worktrees._verify_actual(worktree)
        return worktree.root

    def _selector(
        self,
        root: str,
        *,
        target: str,
        base: str,
        head: str,
        default_branch: str,
    ) -> tuple[list[str], str, str, str, str]:
        kind = str(target or "working").strip().lower()
        if kind not in _TARGETS:
            raise CodingValidationError(f"unsupported review target: {kind}")
        current_oid = self.git.resolve(root, "HEAD", allow_missing=True)
        current_ref = self.git.current_branch(root) or "HEAD"
        if kind == "working":
            return [], "", "", current_ref, current_oid
        if kind == "staged":
            return ["--cached"], "", "", current_ref, current_oid
        if kind == "uncommitted":
            if current_oid:
                return [current_oid], "HEAD", current_oid, current_ref, current_oid
            empty = self.git.run(root, ["hash-object", "-t", "tree", "--stdin"], input_bytes=b"").text
            return [empty], "empty-tree", empty, current_ref, ""
        if kind == "working_since":
            if not base:
                raise CodingValidationError("working_since review requires a base ref")
            base_oid = self.git.resolve(root, base)
            # Compare the pre-effect commit with the current working tree so
            # committed HEAD movement and remaining edits share one Review.
            return [base_oid], str(base), base_oid, current_ref, current_oid
        if kind == "base_branch":
            base_ref = str(base or default_branch or "main")
            base_tip = self.git.resolve(root, base_ref)
            base_oid = self.git.merge_base(root, base_tip, current_oid) if current_oid else base_tip
            return [base_oid], base_ref, base_oid, current_ref, current_oid
        if kind == "branch":
            head_ref = str(head or current_ref or "HEAD")
            head_oid = self.git.resolve(root, head_ref)
            base_ref = str(base or default_branch or "main")
            base_tip = self.git.resolve(root, base_ref)
            base_oid = self.git.merge_base(root, base_tip, head_oid)
            return [base_oid, head_oid], base_ref, base_oid, head_ref, head_oid
        if kind == "commit":
            head_ref = str(head or "HEAD")
            head_oid = self.git.resolve(root, head_ref)
            parent = self.git.resolve(root, f"{head_oid}^", allow_missing=True)
            if not parent:
                parent = self.git.run(
                    root, ["hash-object", "-t", "tree", "--stdin"], input_bytes=b""
                ).text
            return [parent, head_oid], parent, parent, head_ref, head_oid
        # range/custom both pin two immutable commit OIDs.
        if not base or not head:
            raise CodingValidationError(f"{kind} review requires base and head refs")
        base_oid = self.git.resolve(root, base)
        head_oid = self.git.resolve(root, head)
        return [base_oid, head_oid], str(base), base_oid, str(head), head_oid

    def observe_diff(
        self,
        repository_id: str,
        *,
        target: str = "working",
        base: str = "",
        head: str = "",
        paths: Sequence[str] | None = None,
        context: int = 3,
        worktree_id: str = "",
        scope: WorkScope | Mapping[str, Any] | None,
    ) -> DiffSnapshot:
        resolved = self._scope(scope)
        repo = self.repository.get_repository(repository_id)
        root = self._root(repository_id, worktree_id, resolved)
        selector, base_ref, base_oid, head_ref, head_oid = self._selector(
            root,
            target=target,
            base=base,
            head=head,
            default_branch=repo.default_branch,
        )
        status = self.git.status(root, repository_id=repository_id)
        observation = self.git.diff(root, selector, paths=paths, context=context)
        patch_sha = hashlib.sha256(observation.patch).hexdigest()
        patch_ref = ""
        if observation.patch and self.artifact_store is not None:
            artifact = self.artifact_store.put_bytes(
                observation.patch,
                media_type="application/vnd.git.patch",
                kind="coding_review_patch",
                scope=(resolved.chat_id or f"coding:{repository_id}"),
            )
            patch_ref = str(artifact.ref)
        files = list(observation.files)
        present = {item.path for item in files}
        if str(target or "").lower() in {
            "uncommitted", "base_branch", "working", "working_since",
        }:
            for entry in status.entries:
                if entry.record_type == "untracked" and entry.path not in present:
                    content_ref = ""
                    if self.artifact_store is not None:
                        candidate = os.path.abspath(
                            os.path.join(root, entry.path.replace("/", os.sep))
                        )
                        try:
                            contained = os.path.normcase(os.path.commonpath([root, candidate])) == os.path.normcase(root)
                        except ValueError:
                            contained = False
                        if not contained:
                            raise CodingValidationError(
                                "untracked review path escapes the repository"
                            )
                        if os.path.islink(candidate):
                            payload = os.readlink(candidate).encode(
                                "utf-8", errors="surrogateescape"
                            )
                            media_type = "application/vnd.git.symlink"
                        elif os.path.isfile(candidate):
                            if os.path.getsize(candidate) > self.git.max_output_bytes:
                                raise CodingValidationError(
                                    "untracked review file exceeds the evidence bound"
                                )
                            with open(candidate, "rb") as handle:
                                payload = handle.read(self.git.max_output_bytes + 1)
                            if len(payload) > self.git.max_output_bytes:
                                raise CodingValidationError(
                                    "untracked review file changed beyond the evidence bound"
                                )
                            media_type = "application/octet-stream"
                        else:
                            raise CodingValidationError(
                                "untracked review path is not a file or symlink"
                            )
                        content = self.artifact_store.put_bytes(
                            payload,
                            media_type=media_type,
                            kind="coding_review_untracked_content",
                            scope=(resolved.chat_id or f"coding:{repository_id}"),
                        )
                        content_ref = str(content.ref)
                    files.append(DiffFile(
                        path=entry.path,
                        original_path="",
                        status="?",
                        score=0,
                        old_mode="",
                        new_mode="",
                        old_oid="",
                        new_oid="",
                        additions=None,
                        deletions=None,
                        binary=False,
                        content_ref=content_ref,
                    ))
        return DiffSnapshot(
            repository_id=repository_id,
            root=root,
            target=str(target or "working").lower(),
            base_ref=base_ref,
            base_oid=base_oid,
            head_ref=head_ref,
            head_oid=head_oid,
            status_fingerprint=status.fingerprint,
            patch_ref=patch_ref,
            patch_sha256=patch_sha,
            patch_bytes=len(observation.patch),
            files=tuple(files),
            observed_at=time.time(),
        )

    def start(
        self,
        repository_id: str,
        *,
        target: str = "working",
        base: str = "",
        head: str = "",
        paths: Sequence[str] | None = None,
        context: int = 3,
        worktree_id: str = "",
        scope: WorkScope | Mapping[str, Any] | None,
        origin: str = "",
    ) -> ReviewRecord:
        resolved = self._scope(scope)
        snapshot = self.observe_diff(
            repository_id,
            target=target,
            base=base,
            head=head,
            paths=paths,
            context=context,
            worktree_id=worktree_id,
            scope=resolved,
        )
        review_id = "review_" + uuid.uuid4().hex
        additions = sum(item.additions or 0 for item in snapshot.files)
        deletions = sum(item.deletions or 0 for item in snapshot.files)
        summary = {
            "files": len(snapshot.files),
            "additions": additions,
            "deletions": deletions,
            "binary_files": sum(1 for item in snapshot.files if item.binary),
            "untracked_files": sum(1 for item in snapshot.files if item.status == "?"),
            "patch_bytes": snapshot.patch_bytes,
            "patch_available": bool(snapshot.patch_ref or not snapshot.patch_bytes),
            "origin": str(origin or "manual")[:100],
        }
        now = time.time()
        review = ReviewRecord(
            review_id=review_id,
            repository_id=repository_id,
            worktree_id=worktree_id,
            root=snapshot.root,
            target=snapshot.target,
            base_ref=snapshot.base_ref,
            base_oid=snapshot.base_oid,
            head_ref=snapshot.head_ref,
            head_oid=snapshot.head_oid,
            status_fingerprint=snapshot.status_fingerprint,
            patch_ref=snapshot.patch_ref,
            patch_sha256=snapshot.patch_sha256,
            summary=summary,
            state="open",
            approved_head_oid="",
            scope=resolved.with_updates(worktree_id=(worktree_id or resolved.worktree_id)),
            revision=1,
            created_at=now,
            updated_at=now,
        )
        files = tuple(ReviewFile(
            review_id=review_id,
            path=item.path,
            original_path=item.original_path,
            status=item.status,
            score=item.score,
            old_mode=item.old_mode,
            new_mode=item.new_mode,
            old_oid=item.old_oid,
            new_oid=item.new_oid,
            additions=item.additions,
            deletions=item.deletions,
            binary=item.binary,
            patch_ref=(item.content_ref or snapshot.patch_ref),
        ) for item in snapshot.files)
        return self.repository.create_review(review, files)

    def get(self, review_id: str) -> ReviewRecord:
        return self.repository.get_review(review_id)

    def require_visible(
        self,
        review_id: str,
        *,
        scope: WorkScope | Mapping[str, Any] | None,
    ) -> ReviewRecord:
        record = self.get(review_id)
        if not work_scope_visible(record.scope, scope):
            raise ScopeMismatch("review is outside the current WorkScope")
        return record

    def list(
        self, *, repository_id: str = "",
        scope: WorkScope | Mapping[str, Any] | None = None,
        limit: int = 100,
    ) -> tuple[ReviewRecord, ...]:
        return self.repository.list_reviews(
            repository_id=repository_id, scope=scope, limit=limit,
        )

    def files(self, review_id: str) -> tuple[ReviewFile, ...]:
        return self.repository.list_review_files(review_id)

    def project_files(self, review_id: str) -> list[dict[str, Any]]:
        """Return immutable Review files with UI-ready hunks from CAS evidence."""

        review = self.get(review_id)
        files = self.files(review_id)
        patch_hunks: dict[str, list[dict[str, Any]]] = {}
        if review.patch_ref and self.artifact_store is not None:
            patch_hunks = _patch_hunks(
                self.artifact_store.read_bytes(review.patch_ref)
            )
        rows: list[dict[str, Any]] = []
        for item in files:
            status = str(item.status or "M").upper()
            action = (
                "create" if status.startswith(("A", "?"))
                else "delete" if status.startswith("D") else "update"
            )
            hunks = list(patch_hunks.get(item.path) or ())
            truncated = bool(item.binary)
            if (
                status == "?" and item.patch_ref
                and item.patch_ref != review.patch_ref
                and self.artifact_store is not None
            ):
                payload = self.artifact_store.read_bytes(item.patch_ref)
                text = payload.decode("utf-8", errors="replace")
                lines = text.splitlines()
                projected = [
                    {"kind": "add", "text": line, "newNo": index}
                    for index, line in enumerate(
                        lines[:_MAX_PROJECTED_HUNK_LINES], 1
                    )
                ]
                if projected:
                    hunks = [{
                        "oldStart": 1, "oldLines": 0,
                        "newStart": 1, "newLines": len(lines),
                        "lines": projected,
                    }]
                truncated = truncated or len(lines) > len(projected)
            rows.append({
                **item.to_dict(),
                "absPath": os.path.join(
                    review.root, item.path.replace("/", os.sep)
                ),
                "label": os.path.basename(item.path) or item.path,
                "action": action,
                "added": int(item.additions or 0),
                "removed": int(item.deletions or 0),
                "truncated": truncated,
                "hunks": hunks,
            })
        return rows

    def _assert_scope(self, review: ReviewRecord, scope: WorkScope) -> None:
        if scope.worktree_id and scope.worktree_id != review.worktree_id:
            raise ScopeMismatch("review is outside the current WorkScope")
        owner = review.scope.with_updates(worktree_id="")
        if not work_scope_visible(owner, scope):
            raise ScopeMismatch("review does not match WorkScope")

    def assert_current(
        self,
        review_id: str,
        *,
        scope: WorkScope | Mapping[str, Any] | None,
        mark_stale: bool = True,
    ) -> ReviewRecord:
        resolved = self._scope(scope)
        review = self.repository.get_review(review_id)
        self._assert_scope(review, resolved)
        changed = False
        if review.target in {
            "working", "working_since", "staged", "uncommitted", "base_branch",
        }:
            current = self.git.status(review.root, repository_id=review.repository_id)
            changed = (
                current.head_oid != review.head_oid
                or current.fingerprint != review.status_fingerprint
            )
        elif review.target == "branch":
            changed = self.git.resolve(
                review.root, review.head_ref, allow_missing=True
            ) != review.head_oid
        elif review.target in {"range", "custom"}:
            changed = (
                self.git.resolve(review.root, review.head_ref, allow_missing=True)
                != review.head_oid
                or self.git.resolve(review.root, review.base_ref, allow_missing=True)
                != review.base_oid
            )
        # A commit target is already an immutable object snapshot.
        if changed:
            if mark_stale and review.state in {"open", "approved"}:
                review = self.repository.transition_review(
                    review_id,
                    expected_revision=review.revision,
                    allowed_states=(review.state,),
                    state="stale",
                )
            raise ReviewStale(
                "reviewed head/index/worktree no longer match the immutable review snapshot"
            )
        return review

    def approve(
        self,
        review_id: str,
        *,
        expected_revision: int,
        scope: WorkScope | Mapping[str, Any] | None,
        required_checks: Sequence[str] = (),
    ) -> ReviewRecord:
        identity = self.repository.get_review(review_id)
        with repository_mutation_lock(identity.repository_id):
            review = self.assert_current(review_id, scope=scope)
            if review.revision != int(expected_revision):
                raise CodingConflict("review revision changed")
            if required_checks:
                checks = self.repository.list_checks(review_id)
                latest = {item.recipe: item for item in checks}
                missing = [
                    name for name in required_checks
                    if name not in latest or latest[name].state != "passed"
                    or latest[name].head_oid != review.head_oid
                    or latest[name].status_fingerprint != review.status_fingerprint
                ]
                if missing:
                    raise CodingConflict("required checks are not passing: " + ", ".join(missing))
            # Host-managed mutations share this repository lock. External
            # same-user Git/FS writes do not, so check immediately around the
            # approval commit as well and stale a raced approval afterward.
            review = self.assert_current(review_id, scope=scope)
            approved = self.repository.transition_review(
                review_id,
                expected_revision=review.revision,
                allowed_states=("open",),
                state="approved",
                approved_head_oid=review.head_oid,
            )
            return self.assert_current(approved.review_id, scope=scope)

    def add_finding(
        self,
        review_id: str,
        *,
        path: str,
        line: int = 0,
        side: str = "new",
        severity: str = "note",
        title: str = "",
        body: str,
        scope: WorkScope | Mapping[str, Any] | None,
    ):
        resolved = self._scope(scope)
        review = self.repository.get_review(review_id)
        self._assert_scope(review, resolved)
        selected_path = str(path or "").replace("\\", "/")
        known = {item.path for item in self.repository.list_review_files(review_id)}
        if selected_path not in known:
            raise CodingValidationError("finding path is not part of the review snapshot")
        selected_side = str(side or "new").lower()
        if selected_side not in {"old", "new"}:
            raise CodingValidationError("finding side must be old or new")
        selected_severity = str(severity or "note").lower()
        if selected_severity not in {"note", "warning", "error", "blocker"}:
            raise CodingValidationError("unsupported finding severity")
        text = str(body or "").strip()
        if not text:
            raise CodingValidationError("finding body is required")
        return self.repository.add_finding(
            review_id=review_id,
            path=selected_path,
            line=max(0, int(line)),
            side=selected_side,
            severity=selected_severity,
            title=str(title or "")[:500],
            body=text[:20_000],
            scope=resolved,
        )

    def findings(self, review_id: str):
        return self.repository.list_findings(review_id)

    def integrate_fast_forward(
        self,
        review_id: str,
        *,
        integration_root: str,
        target_branch: str,
        expected_target_oid: str,
        expected_revision: int,
        scope: WorkScope | Mapping[str, Any] | None,
        idempotency_key: str = "",
    ) -> ReviewRecord:
        resolved = self._scope(scope)
        review = self.assert_current(review_id, scope=resolved)
        if review.state != "approved" or review.revision != int(expected_revision):
            raise CodingConflict("review is not the expected approved revision")
        if not review.approved_head_oid or review.approved_head_oid != review.head_oid:
            raise CodingConflict("review approval is not pinned to its exact head")
        if self.git.status(review.root, repository_id=review.repository_id).dirty:
            raise CodingConflict("review worktree is dirty; reviewed changes are not an integrable commit")
        repo = self.repository.get_repository(review.repository_id)
        target_observation = self.git.repository(integration_root)
        if os.path.normcase(os.path.realpath(target_observation.common_dir)) != os.path.normcase(
            os.path.realpath(repo.common_dir)
        ):
            raise ScopeMismatch("integration root belongs to another repository")
        if target_observation.branch != str(target_branch):
            raise CodingConflict("integration root is not on the requested target branch")
        with repository_mutation_lock(review.repository_id):
            target_status = self.git.status(integration_root, repository_id=review.repository_id)
            if target_status.dirty:
                raise CodingConflict("integration worktree is dirty")
            if target_status.head_oid != str(expected_target_oid):
                raise CodingConflict("integration target head changed")
            if review.base_oid and target_status.head_oid != review.base_oid:
                raise CodingConflict("integration target is no longer the reviewed base")
            if not self.git.is_ancestor(integration_root, target_status.head_oid, review.head_oid):
                raise CodingConflict("reviewed head cannot fast-forward the target")
            request_hash = request_fingerprint("coding.review.integrate.ff", {
                "review_id": review_id,
                "target_head_oid": target_status.head_oid,
                "review_head_oid": review.head_oid,
            })
            operation, replay = self.repository.reserve_operation(
                repository_id=review.repository_id,
                worktree_id=review.worktree_id,
                kind="review.integrate.ff",
                idempotency_key=idempotency_key,
                request_fingerprint=request_hash,
                before_oid=target_status.head_oid,
                scope=resolved,
            )
            if replay:
                if operation["state"] == "succeeded":
                    current_target = self.git.resolve(integration_root, "HEAD")
                    if current_target != review.head_oid:
                        raise CodingConflict(
                            "succeeded integration receipt does not match target HEAD"
                        )
                    return self.repository.complete_review_integration(
                        operation_id=operation["operation_id"],
                        review_id=review_id,
                        expected_review_revision=review.revision,
                        after_oid=current_target,
                        result={
                            "review_id": review_id,
                            "target_branch": target_branch,
                            "reconciled": True,
                        },
                    )
                if operation["state"] != "running":
                    raise CodingConflict("prior integration requires reconciliation")
                current_target = self.git.resolve(integration_root, "HEAD")
                if current_target == review.head_oid:
                    return self.repository.complete_review_integration(
                        operation_id=operation["operation_id"],
                        review_id=review_id,
                        expected_review_revision=review.revision,
                        after_oid=current_target,
                        result={
                            "review_id": review_id,
                            "target_branch": target_branch,
                            "reconciled": True,
                        },
                    )
                if current_target != target_status.head_oid:
                    raise CodingConflict(
                        "prior integration changed target HEAD to an unknown value"
                    )
            try:
                self.git.run(
                    integration_root,
                    ["merge", "--ff-only", review.head_oid],
                    mutating=True,
                    timeout_s=120.0,
                )
                after = self.git.resolve(integration_root, "HEAD")
                if after != review.head_oid:
                    raise CodingConflict("integration did not produce the exact reviewed head")
                return self.repository.complete_review_integration(
                    operation_id=operation["operation_id"],
                    review_id=review_id,
                    expected_review_revision=review.revision,
                    after_oid=after,
                    result={"review_id": review_id, "target_branch": target_branch},
                )
            except BaseException as exc:
                current_target = ""
                try:
                    current_target = self.git.resolve(integration_root, "HEAD")
                except Exception:
                    current_target = ""
                if current_target == review.head_oid:
                    # Git has already applied. Never rewrite that truth as a
                    # failed operation; reconciliation can complete both rows.
                    try:
                        return self.repository.complete_review_integration(
                            operation_id=operation["operation_id"],
                            review_id=review_id,
                            expected_review_revision=review.revision,
                            after_oid=current_target,
                            result={
                                "review_id": review_id,
                                "target_branch": target_branch,
                                "reconciled_after_error": True,
                            },
                        )
                    except Exception:
                        raise exc
                self.repository.finish_operation(
                    operation["operation_id"],
                    state=(
                        "failed"
                        if current_target == target_status.head_oid
                        else "unknown_effect"
                    ),
                    after_oid=current_target,
                    diagnostic=str(exc)[:4000],
                )
                raise


__all__ = ["ReviewService"]
