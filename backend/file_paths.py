"""Canonical local-path handling shared by file tools and verification."""

from __future__ import annotations

import os
import re
import stat


def is_reparse_or_link(path: str) -> bool:
    """True for symlinks and Windows directory junctions (reparse points)."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    if callable(isjunction):
        try:
            if isjunction(path):
                return True
        except OSError:
            pass
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse)


def remove_tree(path: str) -> None:
    """Delete a directory tree without following Windows junctions."""
    if not os.path.lexists(path):
        return
    if is_reparse_or_link(path):
        try:
            os.unlink(path)
        except OSError:
            os.rmdir(path)
        return
    if os.path.isdir(path):
        for name in os.listdir(path):
            remove_tree(os.path.join(path, name))
        os.rmdir(path)
        return
    os.unlink(path)


def normalize_path(path: str) -> str:
    value = os.path.expandvars(os.path.expanduser(str(path or "")))
    return os.path.realpath(os.path.abspath(value))


def effective_path(path: str, *, default_cwd: str = "") -> str:
    """Resolve model-facing relative paths consistently.

    The current project directory wins. Existing process-CWD paths and
    top-level folders under the user's home remain same-user fallbacks.
    """
    value = os.path.expandvars(os.path.expanduser(str(path or "")))
    if os.name == "nt":
        drive, _tail = os.path.splitdrive(value)
        is_unc = value.startswith(("\\\\", "//"))
        if drive or is_unc:
            return normalize_path(value)
        # Models commonly emit POSIX-looking project paths. On Windows a
        # drive-less slash makes ntpath.join discard the selected project.
        if value.startswith("/"):
            value = value[1:]
    elif os.path.isabs(value):
        return normalize_path(value)
    from project_context import current_project_context

    working_directory = current_project_context(default_cwd=default_cwd).cwd
    if working_directory and os.path.isdir(working_directory):
        return normalize_path(os.path.join(working_directory, value))
    cwd_candidate = normalize_path(value)
    if os.path.exists(cwd_candidate):
        return cwd_candidate
    home = os.path.expanduser("~")
    home_candidate = normalize_path(os.path.join(home, value))
    if os.path.exists(home_candidate):
        return home_candidate
    first = re.split(r"[/\\]+", value.lstrip("./\\"), maxsplit=1)[0]
    if first and os.path.isdir(os.path.join(home, first)):
        return home_candidate
    return cwd_candidate
