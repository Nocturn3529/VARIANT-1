"""Read-only skill projection over the canonical extension package catalog.

Skills are resources contributed by enabled immutable plugins. This module has
no independent install, permission, proposal, pin, archive, or usage state; the
package activation table is the sole authority for whether a skill is live.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from .packages_v2 import ExtensionPackageService
from .skill_format import parse_skill


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    package_id: str
    package_digest: str
    contribution_id: str


class SkillCatalogService:
    """Expose enabled plugin skill resources through progressive disclosure."""

    def __init__(self, packages: ExtensionPackageService) -> None:
        self.packages = packages

    def _rows(self, *, chat_id: str = "") -> list[dict[str, Any]]:
        return list(self.packages.resolved_contributions(
            kind="skills", chat_id=str(chat_id or ""),
        ))

    @staticmethod
    def _metadata(row: dict[str, Any]) -> dict[str, Any]:
        descriptor = dict(row.get("descriptor") or {})
        return {
            "name": str(descriptor.get("name") or row["id"]),
            "description": str(descriptor.get("description") or "(no description)"),
            "body_sha256": str(descriptor.get("body_sha256") or ""),
            "body_chars": int(descriptor.get("body_chars") or 0),
            "package_id": str(row["package_id"]),
            "package_digest": str(row["package_digest"]),
            "contribution_id": str(row["id"]),
            "descriptor_digest": str(row["descriptor_digest"]),
            "immutable": True,
        }

    def list(self, *, chat_id: str = "") -> list[dict[str, Any]]:
        rows = [self._metadata(row) for row in self._rows(chat_id=chat_id)]
        return sorted(rows, key=lambda row: str(row["name"]).casefold())

    def count(self) -> int:
        return len(self.list())

    def search(
        self, query: str, limit: int = 8, *, chat_id: str = "",
    ) -> list[dict[str, Any]]:
        terms = set(re.findall(r"[a-z0-9_]+", str(query or "").casefold()))
        ranked: list[tuple[int, str, dict[str, Any]]] = []
        for row in self.list(chat_id=chat_id):
            name = str(row["name"]).casefold()
            description = str(row["description"]).casefold()
            score = sum(8 for term in terms if term in name)
            score += sum(2 for term in terms if term in description)
            ranked.append((-score, name, row))
        ranked.sort(key=lambda item: item[:2])
        return [row for _score, _name, row in ranked[:max(1, min(int(limit), 50))]]

    def _find_row(self, name: str, *, chat_id: str = "") -> dict[str, Any] | None:
        key = str(name or "").strip().casefold()
        for row in self._rows(chat_id=chat_id):
            descriptor = dict(row.get("descriptor") or {})
            if key in {
                str(descriptor.get("name") or "").casefold(),
                str(row.get("id") or "").casefold(),
            }:
                return row
        return None

    def inspect(self, name: str, *, chat_id: str = "") -> dict[str, Any] | None:
        row = self._find_row(name, chat_id=chat_id)
        if row is None:
            return None
        resource = self.packages.read_resource(
            str(row["package_id"]), str(row["id"]), "SKILL.md", chat_id=chat_id,
        )
        _metadata, body = parse_skill(str(resource.get("text") or ""))
        return {**self._metadata(row), "instructions": body}

    def get(self, name: str, *, chat_id: str = "") -> Skill | None:
        inspected = self.inspect(name, chat_id=chat_id)
        if inspected is None:
            return None
        return Skill(
            name=str(inspected["name"]),
            description=str(inspected["description"]),
            body=str(inspected["instructions"]),
            package_id=str(inspected["package_id"]),
            package_digest=str(inspected["package_digest"]),
            contribution_id=str(inspected["contribution_id"]),
        )

__all__ = ["Skill", "SkillCatalogService"]
