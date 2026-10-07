"""Validate an installed backend environment against one pinned dependency lock.

Run it with the environment's own interpreter after a fresh install of the
lock (``pip install --no-deps -r <lock>`` then ``pip check``):

1. every pinned distribution is installed at exactly its pinned version, and
   nothing beyond the lock is installed except packaging tools;
2. every dependency that ``requirements.txt`` declares for this platform
   imports.

Prints a short report and exits non-zero on any mismatch.
"""
from __future__ import annotations

import argparse
from importlib import import_module, metadata
import platform
import sys
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

PACKAGING_TOOLS = frozenset({"pip", "setuptools", "wheel"})


def read_lock(path: Path) -> dict[str, str]:
    """``name -> version`` for every ``name[extras]==version`` pin."""

    pins: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        requirement = Requirement(line)
        exact = [spec for spec in requirement.specifier if spec.operator == "=="]
        if len(exact) != 1:
            raise ValueError(f"lock line is not one exact pin: {raw!r}")
        pins[canonicalize_name(requirement.name)] = exact[0].version
    return pins


def installed_versions(distributions=metadata.distributions) -> dict[str, str]:
    return {
        canonicalize_name(dist.metadata["Name"]): str(dist.version)
        for dist in distributions()
        if dist.metadata["Name"]
    }


def compare(lock: dict[str, str], installed: dict[str, str]) -> dict[str, list]:
    return {
        "missing": sorted(name for name in lock if name not in installed),
        "version_mismatch": sorted(
            (name, lock[name], installed[name])
            for name in lock
            if name in installed and installed[name] != lock[name]
        ),
        "unexpected": sorted(
            name for name in installed
            if name not in lock and name not in PACKAGING_TOOLS
        ),
    }


def declared_for_this_platform(requirements: Path) -> list[str]:
    names = []
    for raw in requirements.read_text(encoding="utf-8-sig").splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        names.append(canonicalize_name(requirement.name))
    return names


def import_failures(names: list[str]) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Import each declared distribution's primary top-level module.

    The primary module is the one named like the distribution; when none is,
    any of its top-level modules importing counts. Other modules can be
    optional add-ons (duckdb ships an ADBC driver needing an extra package),
    so their failures are notes rather than failures.
    """

    modules_by_dist: dict[str, set[str]] = {}
    for module, dists in metadata.packages_distributions().items():
        for dist in dists:
            modules_by_dist.setdefault(canonicalize_name(dist), set()).add(module)
    failures: list[tuple[str, str, str]] = []
    notes: list[str] = []
    for name in names:
        modules = sorted(
            module for module in modules_by_dist.get(name, set())
            if not module.startswith("_")
        )
        if not modules:
            failures.append((name, "", "no importable top-level module found"))
            continue
        primary = [module for module in modules if canonicalize_name(module) == name]
        errors: dict[str, str] = {}
        for module in primary or modules:
            try:
                import_module(module)
            except Exception as exc:  # report every failure, keep checking
                errors[module] = f"{type(exc).__name__}: {exc}"
        imported = [module for module in (primary or modules) if module not in errors]
        if primary and errors:
            failures.extend((name, module, error) for module, error in errors.items())
        elif not imported:
            failures.extend((name, module, error) for module, error in errors.items())
        else:
            notes.extend(f"{name} {module}: {error}" for module, error in errors.items())
    return failures, notes


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--requirements", type=Path, default=root / "backend/requirements.txt")
    args = parser.parse_args()

    lock = read_lock(args.lock)
    result = compare(lock, installed_versions())
    declared = declared_for_this_platform(args.requirements)
    failures, notes = import_failures(declared)

    print(f"python {sys.version.split()[0]} on {sys.platform} {platform.machine()}")
    print(f"lock {args.lock.name}: {len(lock)} pins")
    print(f"missing: {result['missing'] or 'none'}")
    print(f"version mismatches: {result['version_mismatch'] or 'none'}")
    print(f"unexpected installed: {result['unexpected'] or 'none'}")
    print(f"declared dependencies imported: {len(declared) - len({f[0] for f in failures})}/{len(declared)}")
    for dist, module, error in failures:
        print(f"IMPORT FAILURE {dist} {module}: {error}")
    for note in notes:
        print(f"note (optional module): {note}")
    ok = not (result["missing"] or result["version_mismatch"] or result["unexpected"] or failures)
    print("RESULT: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
