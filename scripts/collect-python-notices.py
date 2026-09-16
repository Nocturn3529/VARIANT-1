"""Collect installed dependency license texts for the release package."""
from __future__ import annotations

from importlib import metadata
from pathlib import Path
import re
import sys

root = Path(__file__).resolve().parents[1]


def _dependency_inventory() -> Path:
    """Prefer the win32 lock on Windows; otherwise the portable requirements.txt."""
    lock = root / "backend" / "requirements.lock"
    req = root / "backend" / "requirements.txt"
    if sys.platform.startswith("win") and lock.is_file():
        return lock
    if req.is_file():
        return req
    if lock.is_file():
        return lock
    raise RuntimeError("missing backend/requirements.txt and backend/requirements.lock")


def _requirement_names(inventory: Path) -> list[str]:
    names: list[str] = []
    for line in inventory.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        req_part = raw.split(";", 1)[0].strip()
        if not req_part:
            continue
        marker = raw.split(";", 1)[1].strip() if ";" in raw else ""
        if marker:
            try:
                from packaging.markers import Marker
                if not Marker(marker).evaluate():
                    continue
            except Exception:
                pass
        names.append(re.split(r"[=<>!~\[]", req_part)[0].strip())
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        key = name.lower()
        if key in seen or not name:
            continue
        seen.add(key)
        out.append(name)
    return out


def _cpython_license_path() -> Path:
    base = Path(sys.base_prefix)
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
    raise RuntimeError(
        "CPython license text is required for release packaging "
        f"(looked under {base}; expected LICENSE or LICENSE.txt)"
    )


inventory = _dependency_inventory()
names = _requirement_names(inventory)
notices = [
    "Dependency notices for the Python build environment "
    f"({inventory.relative_to(root).as_posix()}). Components retain their own licenses."
]
for name in sorted(names, key=str.lower):
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        continue
    paths = [
        p
        for p in dist.files or []
        if any(part.lower().startswith(("license", "copying", "notice")) for part in p.parts)
    ]
    notices.append(f"\n{name} {dist.version}\n" + "-" * 60)
    if paths:
        for item in paths:
            path = Path(dist.locate_file(item))
            if path.is_file():
                notices.append(
                    str(item) + "\n" + path.read_text(encoding="utf-8", errors="replace")
                )
    else:
        notices.append(
            dist.metadata.get("License-Expression")
            or dist.metadata.get("License")
            or "See upstream project license."
        )
        notices.extend(dist.metadata.get_all("Project-URL") or [])

python_license = _cpython_license_path()
notices.extend(["\nCPython\n" + "-" * 60, python_license.read_text(encoding="utf-8")])
target = root / "backend/dist/Variant1Backend/_internal/THIRD_PARTY_LICENSES.txt"
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text("\n".join(notices), encoding="utf-8")
print(
    "Collected Python dependency and CPython notices "
    f"from {inventory.relative_to(root).as_posix()} ({python_license.name})."
)
