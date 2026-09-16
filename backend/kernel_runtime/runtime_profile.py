"""Pinned dependency identities for separately packaged kernel runtimes."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.metadata
import sys
from typing import Any, Mapping

from core_invariants import canonical_digest
from .repl_protocol import REPL_PROTOCOL_SCHEMA


KERNEL_RUNTIME_PROFILE_SCHEMA = "variant1.kernel-runtime-profile.v1"
CORE_RUNTIME_PROFILE = "core.v1"
DATA_RUNTIME_PROFILE = "data.v1"

_CORE_PACKAGES = {
    "psutil": "6.1.1",
}
_DATA_PACKAGES = {
    **_CORE_PACKAGES,
    "duckdb": "1.5.5",
    "matplotlib": "3.11.1",
    "numpy": "2.5.1",
    "pandas": "3.0.5",
    "plotly": "6.9.0",
    "pyarrow": "25.0.0",
    "safetensors": "0.8.0",
}


def _digest(value: Mapping[str, Any]) -> str:
    return canonical_digest(value)


@dataclass(frozen=True, slots=True)
class KernelRuntimeProfile:
    profile_id: str
    revision: int
    python_major_minor: str
    packages: Mapping[str, str]

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": KERNEL_RUNTIME_PROFILE_SCHEMA,
            "profile_id": self.profile_id,
            "revision": int(self.revision),
            "repl_protocol": REPL_PROTOCOL_SCHEMA,
            "python_major_minor": self.python_major_minor,
            "packages": dict(sorted(self.packages.items())),
        }

    @property
    def digest(self) -> str:
        return _digest(self.to_payload())

    def to_dict(self) -> dict[str, Any]:
        return {**self.to_payload(), "digest": self.digest}


_PROFILES = {
    CORE_RUNTIME_PROFILE: KernelRuntimeProfile(
        profile_id=CORE_RUNTIME_PROFILE,
        revision=2,
        python_major_minor="3.13",
        packages=_CORE_PACKAGES,
    ),
    DATA_RUNTIME_PROFILE: KernelRuntimeProfile(
        profile_id=DATA_RUNTIME_PROFILE,
        revision=2,
        python_major_minor="3.13",
        packages=_DATA_PACKAGES,
    ),
}


def runtime_profile(profile_id: str = CORE_RUNTIME_PROFILE) -> KernelRuntimeProfile:
    selected = str(profile_id or CORE_RUNTIME_PROFILE).strip().casefold()
    try:
        return _PROFILES[selected]
    except KeyError as exc:
        raise ValueError(f"unknown kernel runtime profile {profile_id!r}") from exc


def installed_profile_state(profile: KernelRuntimeProfile) -> dict[str, Any]:
    installed: dict[str, str] = {}
    mismatches: list[dict[str, str]] = []
    actual_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    if actual_python != profile.python_major_minor:
        mismatches.append({
            "package": "python",
            "expected": profile.python_major_minor,
            "actual": actual_python,
        })
    for package, expected in sorted(profile.packages.items()):
        try:
            actual = str(importlib.metadata.version(package))
        except Exception:
            actual = ""
        installed[package] = actual
        if actual != expected:
            mismatches.append({
                "package": package,
                "expected": expected,
                "actual": actual or "missing",
            })
    return {
        "schema": "variant1.kernel-runtime-profile-state.v1",
        "profile": profile.to_dict(),
        "installed": installed,
        "compatible": not mismatches,
        "mismatches": mismatches,
    }


def validate_profile_document(value: Any) -> KernelRuntimeProfile:
    if not isinstance(value, dict):
        raise RuntimeError("kernel runtime profile document is absent")
    profile = runtime_profile(str(value.get("profile_id") or ""))
    if value != profile.to_dict():
        raise RuntimeError("kernel runtime profile document does not match its pin")
    state = installed_profile_state(profile)
    if not state["compatible"]:
        mismatch = state["mismatches"][0]
        raise RuntimeError(
            "kernel runtime profile dependency mismatch: "
            f"{mismatch['package']} expected {mismatch['expected']}, "
            f"found {mismatch['actual']}"
        )
    return profile


__all__ = [
    "CORE_RUNTIME_PROFILE",
    "DATA_RUNTIME_PROFILE",
    "KERNEL_RUNTIME_PROFILE_SCHEMA",
    "KernelRuntimeProfile",
    "installed_profile_state",
    "runtime_profile",
    "validate_profile_document",
]
