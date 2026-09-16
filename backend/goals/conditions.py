"""Pure deterministic dependency, budget, and wait decisions."""

from __future__ import annotations

from dataclasses import dataclass
import math
import time
from collections.abc import Mapping, Sequence
from typing import Any

from .models import GoalRecord, StepRecord, WaitRecord


@dataclass(frozen=True)
class ReadinessDecision:
    state: str
    reason: str = ""
    dependencies: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.state == "ready"


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    reason: str = ""
    exhausted_key: str = ""


def dependency_readiness(
    step: StepRecord,
    dependencies: Sequence[str],
    steps: Mapping[str, StepRecord],
    *,
    now: float | None = None,
) -> ReadinessDecision:
    """Return the only state derivable from persisted dependencies.

    Failed, blocked, or cancelled prerequisites block dependants.  A skipped
    prerequisite is accepted because an operator explicitly removed it from
    the required path.  Missing dependency rows are corruption and therefore
    block rather than silently running work.
    """

    current = float(time.time() if now is None else now)
    deps = tuple(str(item) for item in dependencies)
    if step.status == "retry_scheduled" and step.available_at > current:
        return ReadinessDecision("pending", "retry delay has not elapsed", deps)
    if step.status not in {"pending", "retry_scheduled", "ready"}:
        return ReadinessDecision(step.status, "step is not eligible for readiness", deps)
    for dep_id in deps:
        dep = steps.get(dep_id)
        if dep is None:
            return ReadinessDecision("blocked", f"dependency {dep_id} is missing", deps)
        if dep.status in {"failed", "blocked", "cancelled"}:
            return ReadinessDecision(
                "blocked", f"dependency {dep_id} ended as {dep.status}", deps
            )
        if dep.status not in {"succeeded", "skipped"}:
            return ReadinessDecision("pending", f"dependency {dep_id} is {dep.status}", deps)
    return ReadinessDecision("ready", "all dependencies satisfied", deps)


def budget_allows(goal: GoalRecord, *, now: float | None = None) -> BudgetDecision:
    current = float(time.time() if now is None else now)
    if goal.deadline and current >= goal.deadline:
        return BudgetDecision(False, "goal deadline reached", "deadline")
    for key in sorted(goal.budget_limits):
        limit = goal.budget_limits.get(key)
        usage = goal.budget_usage.get(key, 0)
        if isinstance(limit, bool) or not isinstance(limit, (int, float)):
            return BudgetDecision(False, f"budget limit {key} is not numeric", str(key))
        if not math.isfinite(float(limit)) or float(limit) < 0:
            return BudgetDecision(False, f"budget limit {key} is invalid", str(key))
        if isinstance(usage, bool) or not isinstance(usage, (int, float)):
            return BudgetDecision(False, f"budget usage {key} is not numeric", str(key))
        effective_usage = float(usage)
        if str(key) == "wall_time_s" and goal.created_at:
            effective_usage = max(effective_usage, current - goal.created_at)
        if effective_usage >= float(limit):
            return BudgetDecision(False, f"budget {key} exhausted", str(key))
    return BudgetDecision(True)


def event_matches(matcher: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    """Bounded recursive-subset match used for explicit durable wake events."""

    def matches(expected: Any, actual: Any, depth: int) -> bool:
        if depth > 8:
            return False
        if isinstance(expected, Mapping):
            if not isinstance(actual, Mapping):
                return False
            return all(key in actual and matches(value, actual[key], depth + 1)
                       for key, value in expected.items())
        if isinstance(expected, list):
            return isinstance(actual, list) and expected == actual
        return expected == actual

    return matches(dict(matcher), dict(event), 0)


def wait_is_ready(
    wait: WaitRecord,
    *,
    now: float | None = None,
    event: Mapping[str, Any] | None = None,
) -> bool:
    if wait.status != "pending":
        return False
    current = float(time.time() if now is None else now)
    if wait.wake_at and current >= wait.wake_at:
        return True
    return event is not None and event_matches(wait.matcher, event)


__all__ = [
    "BudgetDecision", "ReadinessDecision", "budget_allows",
    "dependency_readiness", "event_matches", "wait_is_ready",
]
