"""Thin same-user project context derived from the active run.

Project context chooses convenient defaults for relative paths and subprocesses.
It is not an authority boundary, revision graph, snapshot store, or worktree owner.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Mapping

from run_context import current_run_context


def _path(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return os.path.realpath(os.path.abspath(os.path.expandvars(os.path.expanduser(text))))


@dataclass(frozen=True, slots=True)
class ProjectContext:
    cwd: str
    roots: tuple[str, ...]
    environment: Mapping[str, Any]


class ProjectBindingError(ValueError):
    """A requested durable chat project root is not usable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)


def canonical_project_binding(value: Any) -> dict[str, str] | None:
    """Validate and normalize the user-selected root stored on one chat."""

    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ProjectBindingError(
            "invalid_project_root", "project root must be a string or null",
        )
    supplied = os.path.expandvars(os.path.expanduser(value.strip()))
    if not supplied:
        return None
    if not os.path.isabs(supplied):
        raise ProjectBindingError(
            "project_root_not_absolute", "project root must be an absolute path",
        )
    root = os.path.realpath(os.path.abspath(supplied))
    if not os.path.isdir(root):
        raise ProjectBindingError(
            "project_root_not_directory",
            f"project root is not an existing directory: {root}",
        )
    name = os.path.basename(os.path.normpath(root))
    if not name:
        drive, _tail = os.path.splitdrive(root)
        name = drive.rstrip(":\\/") or root
    return {"root": root, "name": name}


def stored_project_binding(value: Any) -> dict[str, str] | None:
    """Project the persisted record without pretending it still exists."""

    if not isinstance(value, Mapping):
        return None
    root = str(value.get("root") or "").strip()
    if not root or not os.path.isabs(root):
        return None
    canonical = os.path.realpath(os.path.abspath(root))
    name = str(value.get("name") or "").strip()
    if not name:
        name = os.path.basename(os.path.normpath(canonical)) or canonical
    return {"root": canonical, "name": name}


def _explicit_host_root() -> str:
    """Return the optional deployment-level working-directory override."""
    explicit = _path(os.environ.get("VARIANT1_PROJECT_ROOT"))
    if explicit and os.path.isdir(explicit):
        return explicit
    return ""


def host_project_context(host: Any) -> ProjectContext:
    """Return the host's same-user working directory without a Workspace layer.

    Development keeps the source checkout as its useful default. In a packaged
    build app_root is the immutable resources tree, so the writable user data
    directory is the automatic fallback.
    """

    selected = _explicit_host_root()
    app_root = _path(getattr(host, "app_root", ""))
    data_dir = _path(getattr(host, "data_dir", ""))
    packaged_layout = bool(
        app_root and data_dir
        and os.path.normcase(app_root) != os.path.normcase(data_dir)
    )
    fallback = data_dir if packaged_layout else (app_root or data_dir or os.getcwd())
    cwd = selected or fallback
    return ProjectContext(cwd=cwd, roots=(cwd,) if cwd else (), environment={})


def chat_project_context(host: Any, chat_id: str) -> ProjectContext:
    """Resolve one chat's durable root, falling back to the host default."""

    fallback = host_project_context(host)
    clean = str(chat_id or "").strip()
    if not clean:
        return fallback
    try:
        sessions = host.require_runtime().sessions
    except (AttributeError, RuntimeError):
        return fallback
    get_project = getattr(sessions, "get_project", None)
    if not callable(get_project):
        return fallback
    try:
        project = get_project(clean)
    except ProjectBindingError:
        raise
    except Exception as exc:
        raise ProjectBindingError(
            "project_binding_unavailable",
            f"could not resolve the chat project: {exc}",
        ) from exc
    root = str((project or {}).get("root") or "").strip()
    if project is None:
        return fallback
    if root and os.path.isdir(root):
        canonical = _path(root)
        return ProjectContext(
            cwd=canonical, roots=(canonical,), environment={},
        )
    raise ProjectBindingError(
        "project_root_unavailable",
        f"the project bound to chat {clean} is unavailable: {root or '(missing root)'}",
    )


def current_project_context(default_cwd: str = "") -> ProjectContext:
    context = current_run_context()
    metadata = dict(getattr(context, "metadata", {}) or {}) if context else {}
    raw_roots = metadata.get("project_roots") or ()
    roots = []
    for value in raw_roots if isinstance(raw_roots, (list, tuple)) else ():
        resolved = _path(value)
        if resolved and resolved not in roots:
            roots.append(resolved)
    cwd = _path(
        metadata.get("working_directory")
        or metadata.get("project_root")
        or (roots[0] if roots else "")
        or default_cwd
        or os.getcwd()
    )
    if cwd and cwd not in roots:
        roots.insert(0, cwd)
    raw_environment = metadata.get("project_environment") or {}
    environment = dict(raw_environment) if isinstance(raw_environment, Mapping) else {}
    return ProjectContext(cwd=cwd, roots=tuple(roots), environment=environment)


__all__ = [
    "ProjectBindingError",
    "ProjectContext",
    "canonical_project_binding",
    "chat_project_context",
    "current_project_context",
    "host_project_context",
    "stored_project_binding",
]
