"""Stable repository discovery and refresh over Git common-directory identity."""

from __future__ import annotations

import hashlib
import os
from typing import Any, Mapping

from work_fabric.scope import WorkScope, coerce_work_scope

from .git_process import GitProcess
from .models import CodingConflict, RepositoryRecord
from .repository import CodingRepository, path_key


def repository_id_for(common_dir: str, object_format: str) -> str:
    material = f"{path_key(common_dir)}\0{str(object_format or 'sha1').lower()}"
    return "repo_" + hashlib.sha256(material.encode("utf-8", errors="surrogatepass")).hexdigest()[:24]


class RepositoryRegistry:
    def __init__(self, repository: CodingRepository, git: GitProcess) -> None:
        self.repository = repository
        self.git = git

    def discover(
        self,
        path: str = ".",
        *,
        scope: WorkScope | Mapping[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> RepositoryRecord:
        observation = self.git.repository(path, timeout_s=timeout_s)
        identity = repository_id_for(observation.common_dir, observation.object_format)
        existing = self.repository.get_repository_by_common(
            observation.common_dir, observation.object_format
        )
        if existing is not None and existing.repository_id != identity:
            raise CodingConflict("repository common-directory identity changed")
        if (
            existing is not None
            and os.path.normcase(os.path.realpath(existing.root))
            != os.path.normcase(os.path.realpath(observation.root))
            and os.path.isdir(existing.root)
        ):
            # Linked worktrees share repository identity but must not silently
            # replace the registry's stable display/primary root or project its
            # branch head as the repository head.
            primary = self.git.repository(existing.root, timeout_s=timeout_s)
            if (
                os.path.normcase(os.path.realpath(primary.common_dir))
                == os.path.normcase(os.path.realpath(observation.common_dir))
                and primary.object_format == observation.object_format
            ):
                observation = primary
        resolved_scope = coerce_work_scope(scope)
        if resolved_scope.empty and existing is not None:
            resolved_scope = existing.scope
        return self.repository.upsert_repository(
            repository_id=identity,
            root=observation.root,
            git_dir=observation.git_dir,
            common_dir=observation.common_dir,
            object_format=observation.object_format,
            default_branch=observation.default_branch,
            head_oid=observation.head_oid,
            branch=observation.branch,
            remotes=observation.remotes,
            scope=resolved_scope,
        )

    def refresh(
        self,
        repository_id: str,
        *,
        expected_revision: int | None = None,
    ) -> RepositoryRecord:
        current = self.repository.get_repository(repository_id)
        if expected_revision is not None and current.revision != int(expected_revision):
            raise CodingConflict("repository revision changed")
        result = self.discover(current.root, scope=current.scope)
        if result.repository_id != repository_id:
            raise CodingConflict("repository root now resolves to a different repository")
        return result

    def get(self, repository_id: str) -> RepositoryRecord:
        return self.repository.get_repository(repository_id)

    def list(self) -> tuple[RepositoryRecord, ...]:
        return self.repository.list_repositories()


__all__ = ["RepositoryRegistry", "repository_id_for"]
