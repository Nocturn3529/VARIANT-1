"""Internal Work job handlers for Git-backed review checks.

Review state remains a host/UI concern. It is not projected into the ASTB
model toolbelt.
"""

from __future__ import annotations

from typing import Any

from work_fabric.jobs import JobExecutionContext, JobResult


CODING_CHECK_JOB = "coding.check.v1"


def _coding(host: Any) -> Any:
    runtime = getattr(host.require_runtime(), "coding", None)
    if runtime is None:
        raise RuntimeError("Coding service is unavailable")
    return runtime


def _work(host: Any) -> Any:
    runtime = getattr(host.require_runtime(), "work", None)
    if runtime is None:
        raise RuntimeError("Work Fabric is unavailable")
    return runtime


def register_coding_job_handlers(host: Any) -> None:
    """Install the host-only durable check runner used by the Review UI."""

    def run_checks(execution: JobExecutionContext) -> JobResult:
        manifest = dict(execution.job.input_manifest or {})
        review_id = str(manifest.get("review_id") or "")
        recipes = tuple(str(item) for item in manifest.get("recipes") or ())
        runs = _coding(host).checks.run_many(
            review_id,
            recipes,
            scope=execution.job.scope,
            cancellation_requested=execution.cancellation_requested,
        )
        result = {
            "schema": "variant1.coding-check-result.v1",
            "review_id": review_id,
            "checks": [item.to_dict() for item in runs],
            "passed": all(item.state == "passed" for item in runs),
        }
        artifact = host.require_runtime().session_artifacts.put_json(
            result,
            kind="coding_check_result",
            scope=str(execution.job.scope.chat_id or f"review:{review_id}"),
        )
        return JobResult(
            result_ref=str(artifact.ref),
            progress={
                "phase": "complete",
                "current": len(runs),
                "total": len(runs),
                "message": "Coding checks completed",
                "passed": result["passed"],
            },
        )

    _work(host).register_job_handler(CODING_CHECK_JOB, run_checks)


__all__ = ["CODING_CHECK_JOB", "register_coding_job_handlers"]
