"""Collect installed dependency license texts for the release package.

Resolves the direct inventory plus the frozen Requires-Dist closure. Missing
required packages or license materials fail the build hard.
"""
from __future__ import annotations

from importlib import metadata
from pathlib import Path
import sys

from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

root = Path(__file__).resolve().parents[1]


class NoticeCollectionError(RuntimeError):
    """Release packaging cannot continue without complete notices."""


def _dependency_inventory(project_root: Path | None = None) -> Path:
    """Prefer the win32 lock on Windows; otherwise the portable requirements.txt."""
    base = project_root or root
    lock = base / "backend" / "requirements.lock"
    req = base / "backend" / "requirements.txt"
    if sys.platform.startswith("win") and lock.is_file():
        return lock
    if req.is_file():
        return req
    if lock.is_file():
        return lock
    raise NoticeCollectionError(
        "missing backend/requirements.txt and backend/requirements.lock"
    )


def _strip_requirement_comment(line: str) -> str:
    """Drop PEP 508 / requirements.txt comments not inside quotes."""
    in_single = False
    in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return line[:i].rstrip()
    return line.rstrip()


def _requirement_names(
    inventory: Path,
    *,
    environ: dict[str, str] | None = None,
) -> list[str]:
    """Parse direct requirement names with markers/comments handled correctly."""
    names: list[str] = []
    for lineno, line in enumerate(inventory.read_text(encoding="utf-8").splitlines(), 1):
        raw = _strip_requirement_comment(line).strip()
        if not raw or raw.startswith("#"):
            continue
        try:
            req = Requirement(raw)
        except Exception as exc:
            raise NoticeCollectionError(
                f"{inventory}: line {lineno}: invalid requirement {raw!r}: {exc}"
            ) from exc
        if req.marker is not None:
            try:
                if not req.marker.evaluate(environ):
                    continue
            except Exception as exc:
                raise NoticeCollectionError(
                    f"{inventory}: line {lineno}: marker evaluation failed for "
                    f"{raw!r}: {exc}"
                ) from exc
        names.append(req.name)
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        key = canonicalize_name(name)
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _iter_requires(dist: metadata.Distribution, environ: dict[str, str] | None = None):
    for raw in dist.requires or []:
        try:
            req = Requirement(raw)
        except Exception as exc:
            raise NoticeCollectionError(
                f"{dist.metadata['Name']}: invalid Requires-Dist {raw!r}: {exc}"
            ) from exc
        if req.marker is not None:
            try:
                if not req.marker.evaluate(environ):
                    continue
            except Exception as exc:
                raise NoticeCollectionError(
                    f"{dist.metadata['Name']}: marker evaluation failed for "
                    f"{raw!r}: {exc}"
                ) from exc
        # Skip extras-only edges unless the extra is empty/default.
        if req.extras:
            continue
        yield req.name


def resolve_dependency_closure(
    roots: list[str],
    *,
    environ: dict[str, str] | None = None,
    distribution_loader=metadata.distribution,
) -> list[str]:
    """BFS over Requires-Dist; missing packages raise NoticeCollectionError."""
    seen: set[str] = set()
    ordered: list[str] = []
    stack = list(roots)
    while stack:
        name = stack.pop()
        key = canonicalize_name(name)
        if key in seen:
            continue
        try:
            dist = distribution_loader(name)
        except metadata.PackageNotFoundError as exc:
            raise NoticeCollectionError(
                f"required package not installed for notice collection: {name}"
            ) from exc
        seen.add(key)
        ordered.append(dist.metadata["Name"] or name)
        for child in _iter_requires(dist, environ):
            child_key = canonicalize_name(child)
            if child_key not in seen:
                stack.append(child)
    return ordered


def _license_blobs(dist: metadata.Distribution) -> list[str]:
    paths = [
        p
        for p in dist.files or []
        if any(part.lower().startswith(("license", "copying", "notice")) for part in p.parts)
    ]
    blobs: list[str] = []
    for item in paths:
        path = Path(dist.locate_file(item))
        if path.is_file():
            blobs.append(str(item) + "\n" + path.read_text(encoding="utf-8", errors="replace"))
    if blobs:
        return blobs
    meta = (
        dist.metadata.get("License-Expression")
        or dist.metadata.get("License")
        or ""
    ).strip()
    urls = dist.metadata.get_all("Project-URL") or []
    if meta or urls:
        parts = [meta or "See upstream project license."]
        parts.extend(urls)
        return ["\n".join(parts)]
    raise NoticeCollectionError(
        f"no license text or metadata for required package: {dist.metadata['Name']}"
    )


def _cpython_license_path(base_prefix: Path | None = None) -> Path:
    base = Path(base_prefix or sys.base_prefix)
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    candidates = [
        base / "LICENSE.txt",
        base / "LICENSE",
        base / "LICENSE.md",
        base / "lib" / f"python{version}" / "LICENSE.txt",
        base / "lib" / f"python{version}" / "LICENSE",
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise NoticeCollectionError(
        "CPython license text is required for release packaging "
        f"(looked under {base}; expected LICENSE or LICENSE.txt)"
    )


def collect_notices(
    *,
    project_root: Path | None = None,
    inventory: Path | None = None,
    environ: dict[str, str] | None = None,
    distribution_loader=metadata.distribution,
    cpython_license: Path | None = None,
) -> str:
    base = project_root or root
    inv = inventory or _dependency_inventory(base)
    direct = _requirement_names(inv, environ=environ)
    names = resolve_dependency_closure(
        direct, environ=environ, distribution_loader=distribution_loader
    )
    notices = [
        "Dependency notices for the Python build environment "
        f"({inv.relative_to(base).as_posix() if inv.is_relative_to(base) else inv.as_posix()}). "
        "Components retain their own licenses."
    ]
    for name in sorted(names, key=lambda n: canonicalize_name(n)):
        dist = distribution_loader(name)
        notices.append(f"\n{dist.metadata['Name']} {dist.version}\n" + "-" * 60)
        notices.extend(_license_blobs(dist))

    python_license = cpython_license or _cpython_license_path()
    if not python_license.is_file():
        raise NoticeCollectionError(f"CPython license missing: {python_license}")
    notices.extend(
        ["\nCPython\n" + "-" * 60, python_license.read_text(encoding="utf-8")]
    )
    return "\n".join(notices)


def main() -> None:
    text = collect_notices()
    target = root / "backend/dist/Variant1Backend/_internal/THIRD_PARTY_LICENSES.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    inv = _dependency_inventory()
    print(
        "Collected Python dependency and CPython notices "
        f"from {inv.relative_to(root).as_posix()} "
        f"({_cpython_license_path().name})."
    )


if __name__ == "__main__":
    try:
        main()
    except NoticeCollectionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
