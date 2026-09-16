"""Generated/vendor exclusions and directory-aware gitignore grammar for file tools."""
from __future__ import annotations

import os
from pathlib import Path

from pathspec import GitIgnoreSpec

from project_context import current_project_context

DEFAULT_IGNORES = {
    ".git", ".hg", ".svn", ".idea", ".vscode", "node_modules", "vendor",
    "dist", "build", "target", ".next", ".nuxt", ".cache", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "__pycache__", ".venv", "venv", "env",
    "coverage", ".coverage",
}


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.normcase(os.path.commonpath((path, root))) == os.path.normcase(root)
    except (OSError, ValueError):
        return False


class IgnoreMatcher:
    """Load ancestor rules once and preserve excluded-parent semantics.

    ``*`` cannot cross directories, ``**`` can match zero directories, and
    backslash escapes belong to the pattern grammar. A child cannot re-include
    itself while its parent remains excluded, matching the desktop's npm ignore.
    """
    def __init__(self, root: str, *, project_root: str = ""):
        self.search_root = os.path.realpath(os.path.abspath(root))
        selected = str(project_root or "").strip()
        if not selected:
            selected = current_project_context(default_cwd=self.search_root).cwd
        candidate = os.path.realpath(os.path.abspath(selected or self.search_root))
        self.project_root = candidate if _inside(self.search_root, candidate) else self.search_root
        self._specs: dict[str, GitIgnoreSpec] = {}

    def _spec(self, directory: str) -> GitIgnoreSpec:
        key = os.path.normcase(directory)
        if key not in self._specs:
            patterns = []
            for name in (".gitignore", ".variant1ignore"):
                try:
                    # Preserve escaped spaces and literal !/# characters. npm ignore
                    # defaults to case-insensitive matching on the desktop.
                    patterns.extend(Path(directory, name).read_text(encoding="utf-8").casefold().splitlines())
                except (OSError, UnicodeError):
                    pass
            self._specs[key] = GitIgnoreSpec.from_lines(patterns, backend="simple")
        return self._specs[key]

    def ignored(self, relative_path: str, is_dir: bool = False) -> bool:
        relative = str(relative_path).replace("\\", "/").strip("/")
        candidate = os.path.realpath(os.path.join(self.search_root, relative))
        if not _inside(candidate, self.project_root):
            return False
        project_relative = os.path.relpath(candidate, self.project_root)
        if project_relative == ".":
            return False
        parts = Path(project_relative).parts
        if any(part.casefold() in DEFAULT_IGNORES for part in parts):
            return True
        ancestors = [self.project_root]
        current = self.project_root
        for index, part in enumerate(parts):
            current = os.path.join(current, part)
            directory = index < len(parts) - 1 or is_dir
            ignored = False
            for base in ancestors:
                local = os.path.relpath(current, base).replace("\\", "/").casefold()
                result = self._spec(base).check_file(local + ("/" if directory else ""))
                if result.include is not None:
                    ignored = result.include
            if ignored:
                return True
            if directory:
                ancestors.append(current)
        return False
