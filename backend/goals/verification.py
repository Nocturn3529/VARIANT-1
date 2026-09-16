"""Deterministic goal success-criteria verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Mapping, Sequence
from typing import Any

from .models import GoalArtifactRecord, GoalRecord, StepRecord


SUPPORTED_CRITERIA = frozenset({
    "process_exit", "file_content", "git_state", "reviewed_commit",
    "artifact_exists", "artifact_validation", "child_result",
    "review_no_blockers", "explicit_user_confirmation",
})


@dataclass(frozen=True)
class CriterionResult:
    criterion_id: str
    kind: str
    status: str
    reason: str
    required: bool = True
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id, "kind": self.kind,
            "status": self.status, "reason": self.reason,
            "required": self.required, "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class VerificationReport:
    goal_id: str
    passed: bool
    checks: tuple[CriterionResult, ...]
    step_failures: tuple[str, ...] = ()
    missing_required_steps: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.goal-verification.v1", "goal_id": self.goal_id,
            "passed": self.passed, "checks": [item.to_dict() for item in self.checks],
            "step_failures": list(self.step_failures),
            "missing_required_steps": list(self.missing_required_steps),
        }


def _criterion_id(raw: Mapping[str, Any], index: int) -> str:
    return str(raw.get("id") or raw.get("criterion_id") or f"criterion-{index + 1}")


def _evidence_for(
    raw: Mapping[str, Any], index: int, evidence: Mapping[str, Any]
) -> Mapping[str, Any]:
    cid = _criterion_id(raw, index)
    value = evidence.get(cid)
    if isinstance(value, Mapping):
        return value
    kind_value = evidence.get(str(raw.get("kind") or ""))
    return kind_value if isinstance(kind_value, Mapping) else {}


def _check(
    raw: Mapping[str, Any], index: int, evidence: Mapping[str, Any],
    artifacts: Sequence[GoalArtifactRecord],
) -> CriterionResult:
    cid = _criterion_id(raw, index)
    kind = str(raw.get("kind") or "")
    required = bool(raw.get("required", True))
    proof = dict(_evidence_for(raw, index, evidence))
    if kind not in SUPPORTED_CRITERIA:
        return CriterionResult(cid, kind or "unknown", "unsupported",
                               "criterion has no deterministic verifier", required, proof)
    passed = False; reason = "required evidence is missing or does not match"
    if kind == "process_exit":
        expected = int(raw.get("expected", raw.get("expected_exit_code", 0)))
        passed = proof.get("exit_code") == expected
        reason = f"process exit is {proof.get('exit_code')!r}; expected {expected}"
    elif kind == "file_content":
        passed = bool(proof.get("matched")) and bool(proof.get("content_sha256") or proof.get("evidence_ref"))
        reason = "file-content matcher requires matched=true and content hash/evidence"
    elif kind == "git_state":
        expected = str(raw.get("expected") or raw.get("state") or "clean")
        passed = str(proof.get("state") or "") == expected and bool(proof.get("head_oid"))
        reason = f"Git state/head must prove {expected!r}"
    elif kind == "reviewed_commit":
        passed = bool(proof.get("commit_oid")) and bool(proof.get("review_fingerprint")) \
            and proof.get("approved") is True
        reason = "reviewed commit requires OID, review fingerprint, and approved=true"
    elif kind == "artifact_exists":
        wanted_ref = str(raw.get("artifact_ref") or "")
        wanted_format = str(raw.get("format") or "").casefold()
        matches = [item for item in artifacts if (not wanted_ref or item.artifact_ref == wanted_ref)]
        if wanted_format:
            matches = [item for item in matches if wanted_format in str(
                item.metadata.get("format") or item.metadata.get("media_type") or item.role
            ).casefold()]
        passed = bool(matches)
        reason = "matching attached artifact exists" if passed else "matching attached artifact is absent"
        proof = {"artifact_refs": [item.artifact_ref for item in matches]}
    elif kind == "artifact_validation":
        passed = proof.get("valid") is True and bool(proof.get("artifact_ref"))
        reason = "artifact validation requires valid=true and artifact_ref"
    elif kind == "child_result":
        passed = proof.get("status") == "succeeded" and bool(proof.get("child_id"))
        reason = "child result requires a succeeded child identity"
    elif kind == "review_no_blockers":
        passed = proof.get("blockers") == 0 and bool(proof.get("review_ref") or proof.get("review_fingerprint"))
        reason = "review must prove zero blockers"
    elif kind == "explicit_user_confirmation":
        passed = proof.get("confirmed") is True and bool(proof.get("attention_id") or proof.get("actor"))
        reason = "explicit user confirmation is absent"
    return CriterionResult(cid, kind, "passed" if passed else "failed", reason, required, proof)


def verify_goal(
    goal: GoalRecord,
    steps: Sequence[StepRecord],
    *,
    evidence: Mapping[str, Any] | None = None,
    artifacts: Sequence[GoalArtifactRecord] = (),
) -> VerificationReport:
    checks = tuple(
        _check(dict(raw), index, dict(evidence or {}), artifacts)
        for index, raw in enumerate(goal.success_criteria)
    )
    step_failures = tuple(
        step.step_id for step in steps if step.required and step.status in {"failed", "blocked", "cancelled"}
    )
    missing = tuple(
        step.step_id for step in steps if step.required and step.status not in {"succeeded", "skipped"}
        and step.step_id not in step_failures
    )
    required_checks = [item for item in checks if item.required]
    passed = not step_failures and not missing and all(item.passed for item in required_checks)
    return VerificationReport(goal.goal_id, passed, checks, step_failures, missing)


__all__ = [
    "CriterionResult", "SUPPORTED_CRITERIA", "VerificationReport", "verify_goal",
]
