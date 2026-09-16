"""Immutable provider-schema snapshot and hash for one VARIANT-1 run.

The provider surface is exactly one ``ipython`` schema. This module freezes
that snapshot for checkpoints, resume, and refusal of unknown outer tools.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Iterable

from core_invariants import canonical_digest, canonical_json


def initial_tools(specs: Iterable[dict]) -> list[dict]:
    """Return the complete provider surface for this run."""
    return [deepcopy(spec) for spec in (specs or ()) if isinstance(spec, dict)]


def schema_hash(specs: Iterable[dict]) -> str:
    return canonical_digest(list(specs or ()))[:16]


@dataclass(frozen=True)
class ToolCatalogSnapshot:
    """Immutable complete provider surface bound to one model run."""

    _specs_json: str
    schema_hash: str

    @classmethod
    def from_specs(cls, specs: Iterable[dict]) -> "ToolCatalogSnapshot":
        rows = list(specs or ())
        canonical = canonical_json(rows)
        return cls(
            _specs_json=canonical,
            schema_hash=canonical_digest(rows)[:16],
        )

    @property
    def specs(self) -> tuple[dict, ...]:
        return tuple(json.loads(self._specs_json))

    @property
    def names(self) -> frozenset[str]:
        return frozenset(
            str(spec.get("name") or "")
            for spec in self.specs
            if spec.get("name")
        )

    def public_dict(self) -> dict:
        return {
            "schema_hash": self.schema_hash,
            "tool_count": len(self.specs),
            "tools": sorted(self.names),
        }


def provider_tool_specs(disclosed_specs: Iterable[dict] | None) -> list[dict]:
    """Copy the complete schemas admitted for the next provider call."""
    return [
        deepcopy(spec)
        for spec in (disclosed_specs or ())
        if isinstance(spec, dict)
    ]
