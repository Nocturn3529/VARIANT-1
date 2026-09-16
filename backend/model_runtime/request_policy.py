"""Declarative cloud request projection shared by provider adapters.

Provider profiles describe accepted wire fields. Model-pattern exceptions live
in profile data, so transports do not accumulate provider/model if-chains.
"""

from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from fnmatch import fnmatchcase
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class CloudRequestPolicy:
    completion_token_field: str
    sampling: dict[str, float]
    structured_output_style: str = ""


def _matches(model: str, patterns: tuple[str, ...]) -> bool:
    value = str(model or "").strip().casefold()
    return any(fnmatchcase(value, str(pattern).casefold()) for pattern in patterns)


def resolve_cloud_request_policy(
    profile: Any,
    model: str,
    sampling: Mapping[str, Any],
) -> CloudRequestPolicy:
    fields = tuple(getattr(profile, "sampling_fields", ()) or ())
    forbidden = tuple(
        getattr(profile, "sampling_forbidden_model_patterns", ()) or ()
    )
    if _matches(model, forbidden):
        fields = ()
    if bool(getattr(profile, "omit_temperature", False)):
        fields = tuple(field for field in fields if field != "temperature")

    effective: dict[str, float] = {}
    defaults = {"temperature": 0.7, "top_p": 0.95}
    for field in fields:
        if field not in defaults:
            continue
        raw = sampling.get(field, defaults[field])
        try:
            effective[field] = float(raw)
        except (TypeError, ValueError, OverflowError):
            effective[field] = defaults[field]

    completion_field = str(
        getattr(profile, "completion_token_field", "max_tokens")
        or "max_tokens"
    )
    completion_patterns = tuple(
        getattr(profile, "max_completion_token_model_patterns", ()) or ()
    )
    if completion_field == "auto":
        completion_field = (
            "max_completion_tokens"
            if _matches(model, completion_patterns)
            else "max_tokens"
        )
    if completion_field not in {"max_tokens", "max_completion_tokens"}:
        completion_field = "max_tokens"

    return CloudRequestPolicy(
        completion_token_field=completion_field,
        sampling=effective,
        structured_output_style=str(
            getattr(profile, "structured_output_style", "") or ""
        ),
    )


def effective_reasoning_budget(
    router: Any,
    sampling: Mapping[str, Any],
    override: int | None,
) -> int:
    if override is not None:
        return max(0, int(override))
    if not bool(getattr(router, "reasoning", False)):
        return 0
    raw = sampling.get("reasoning_max_tokens", 1024)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError, OverflowError):
        return 1024


_EFFORT_ORDER = ("off", "none", "minimal", "low", "medium", "high", "xhigh", "max")


def _reasoning_rule(profile: Any, model: str) -> Mapping[str, Any]:
    for rule in getattr(profile, "reasoning_model_rules", ()) or ():
        if not isinstance(rule, Mapping):
            continue
        patterns = rule.get("patterns") or ()
        if isinstance(patterns, str):
            patterns = (patterns,)
        if _matches(model, patterns):
            return rule
    return {}


def _set_payload_field(payload: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    parent = payload
    for part in parts[:-1]:
        current = parent.get(part)
        if not isinstance(current, dict):
            if value is None:
                return
            current = {}
        else:
            current = dict(current)
        parent[part] = current
        parent = current
    if value is None:
        parent.pop(parts[-1], None)
    else:
        parent[parts[-1]] = deepcopy(value)


def project_reasoning_policy(
    router: Any,
    profile: Any,
    model: str,
    payload: dict,
    reasoning_budget: int | None,
    *,
    effort_field: str | None = None,
    default_effort: str = "",
) -> str:
    """Honor a per-call minimum without changing the durable chat setting.

    Zero means off where declared, otherwise the weakest supported reasoning.
    Providers with budget/toggle APIs declare native minimum fields in their
    existing profile. Unknown compatible protocols receive no invented knobs.
    A missing/nonzero override preserves the established chat effort policy.
    """
    minimum = reasoning_budget == 0
    rule = _reasoning_rule(profile, model) if minimum else {}
    field = (effort_field if effort_field is not None else
             str(getattr(profile, "reasoning_effort_field", "") or ""))
    if minimum:
        efforts = tuple(rule.get("efforts", getattr(profile, "reasoning_efforts", ())) or ())
        effort = next((level for level in _EFFORT_ORDER if level in efforts), "")
        # A plugin may declare an ordered provider vocabulary of its own.
        if not effort and efforts:
            effort = str(efforts[0])
    else:
        try:
            effort = router.get_reasoning_effort(getattr(profile, "name", ""), model)
        except Exception:
            effort = default_effort
    if field and effort:
        _set_payload_field(payload, field, effort)
    if minimum:
        for path, value in (rule.get("minimum_fields") or {}).items():
            _set_payload_field(payload, str(path), value)
    return effort


__all__ = [
    "CloudRequestPolicy",
    "effective_reasoning_budget",
    "project_reasoning_policy",
    "resolve_cloud_request_policy",
]
