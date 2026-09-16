"""Deterministic post-dispatch verification for desktop transactions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .models import DesktopElement, DesktopObservation


@dataclass(frozen=True, slots=True)
class VerificationResult:
    state: str
    evidence: dict[str, Any]


def _find(after: DesktopObservation, reference: str) -> DesktopElement | None:
    for element in after.elements:
        if element.element_ref == reference:
            return element
    return None


def _same(actual: Any, expected: Any) -> bool:
    if actual is None:
        return expected is None
    if isinstance(expected, str):
        return str(actual) == expected
    return actual == expected


def verify_operation(
    *, action: str, before: DesktopObservation, after: DesktopObservation,
    target: DesktopElement | None, expectation: Mapping[str, Any],
    readback: Any = None,
) -> VerificationResult:
    changed = after.fingerprint != before.fingerprint
    evidence: dict[str, Any] = {
        "before_fingerprint": before.fingerprint,
        "after_fingerprint": after.fingerprint,
        "observation_changed": changed,
        "readback": readback,
        "checks": [],
    }
    checks: list[dict[str, Any]] = evidence["checks"]
    explicit = bool(expectation)
    # Compact IPython contract: {"property": "value", "equals": "..."}.
    if "property" in expectation and "equals" in expectation:
        property_name = str(expectation.get("property") or "")
        current = _find(after, target.element_ref) if target else None
        if current is None:
            actual = None
        elif property_name in {"name", "text", "value", "role"}:
            actual = getattr(current, property_name)
        else:
            actual = current.states.get(property_name)
        expected = expectation.get("equals")
        checks.append({
            "kind": f"target.{property_name}", "expected": expected,
            "actual": actual, "passed": _same(actual, expected),
        })
    expected_change = expectation.get("observation_changed")
    if expected_change is not None:
        checks.append({
            "kind": "observation_changed", "expected": bool(expected_change),
            "actual": changed, "passed": changed is bool(expected_change),
        })
    if "readback_equals" in expectation:
        expected = expectation["readback_equals"]
        checks.append({
            "kind": "readback_equals", "expected": expected, "actual": readback,
            "passed": _same(readback, expected),
        })

    element_expectation = expectation.get("element")
    if isinstance(element_expectation, Mapping):
        reference = str(element_expectation.get("ref") or (
            target.element_ref if target else ""))
        current = _find(after, reference) if reference else None
        expected_exists = bool(element_expectation.get("exists", True))
        checks.append({
            "kind": "element_exists", "ref": reference,
            "expected": expected_exists, "actual": current is not None,
            "passed": (current is not None) is expected_exists,
        })
        if current is not None:
            for field in ("name", "text", "value", "role"):
                if field in element_expectation:
                    expected = element_expectation[field]
                    actual = getattr(current, field)
                    checks.append({
                        "kind": f"element.{field}", "expected": expected,
                        "actual": actual, "passed": _same(actual, expected),
                    })
            if "state" in element_expectation:
                expected = element_expectation["state"]
                actual = current.states.get("state")
                checks.append({
                    "kind": "element.state", "expected": expected,
                    "actual": actual, "passed": _same(actual, expected),
                })
            if isinstance(element_expectation.get("states"), Mapping):
                for key, expected in element_expectation["states"].items():
                    actual = current.states.get(key)
                    checks.append({
                        "kind": f"element.states.{key}", "expected": expected,
                        "actual": actual, "passed": _same(actual, expected),
                    })

    if checks:
        passed = all(bool(check["passed"]) for check in checks)
        if passed:
            evidence["verification"] = "explicit_expectation_satisfied"
            return VerificationResult("verified", evidence)
        evidence["verification"] = "explicit_expectation_not_satisfied"
        # If nothing changed, no effect is stronger and more useful than a
        # generic failed verification. A changed but wrong state is failed.
        return VerificationResult("failed" if changed else "no_effect", evidence)

    normalized = action.casefold()
    if readback is not None and normalized in {
        "set_value", "type", "select", "toggle", "set_range_value",
    }:
        evidence["verification"] = "semantic_pattern_readback"
        return VerificationResult("verified", evidence)
    deterministic = any("uia" in item.provenance for item in after.elements)
    evidence["deterministic_uia_evidence"] = deterministic
    if changed and deterministic:
        evidence["verification"] = (
            "fresh_observation_changed_without_explicit_postcondition")
        return VerificationResult("verified", evidence)
    if changed:
        evidence["verification"] = "visual_only_change_is_not_deterministic"
        return VerificationResult("failed", evidence)
    evidence["verification"] = "fresh_observation_showed_no_effect"
    evidence["explicit_expectation_supplied"] = explicit
    return VerificationResult("no_effect", evidence)


__all__ = ["VerificationResult", "verify_operation"]
