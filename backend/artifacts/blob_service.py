"""Internal scoped-blob projection behind the mounted ``artifacts`` object."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tool_core import ToolError
from work_fabric.scope import WorkScope

from .scopes import cas_scope_id


class ArtifactBlobService:
    """Bounded access to CAS grants without exposing the CAS driver itself."""

    def __init__(self, store: Any) -> None:
        if store is None:
            raise ValueError("artifact blob storage is required")
        self._store = store

    def list(
        self,
        *,
        scope: str | WorkScope | Mapping[str, Any] | None,
        limit: int = 20,
        kind: str = "",
        artifact_id: str = "",
    ) -> list[dict[str, Any]]:
        return self._store.list_scope(
            cas_scope_id(scope, artifact_id),
            limit=max(1, min(int(limit or 20), 200)),
            kind=str(kind or ""),
        )

    def read_text(
        self,
        ref: str,
        *,
        scope: str | WorkScope | Mapping[str, Any] | None,
        max_chars: int = 20_000,
        artifact_id: str = "",
    ) -> dict[str, Any]:
        cap = max(1, min(int(max_chars or 20_000), 100_000))
        raw = self._store.read_bytes_scoped(
            str(ref or ""), cas_scope_id(scope, artifact_id)
        )
        value = raw.decode("utf-8", errors="replace")
        return {
            "ref": str(ref or ""),
            "text": value[:cap],
            "bytes": len(raw),
            "truncated": len(value) > cap,
        }

    def read_bytes(self, ref: str, *, scope, max_bytes: int = 4_194_304) -> bytes:
        """Return complete bytes, never an indistinguishable truncated file."""
        bound = max(1, min(int(max_bytes), 16_777_216))
        scope_id = cas_scope_id(scope)
        metadata = self._store.stat(ref, scope=scope_id)
        if metadata.bytes > bound:
            raise ValueError(f"Artifact has {metadata.bytes} bytes; use artifacts.save(ref, path) or increase max_bytes.")
        with self._store.open_reader(ref, scope=scope_id) as reader:
            raw = reader.read(bound + 1)
        if len(raw) != metadata.bytes or len(raw) > bound:
            raise ValueError("Artifact size changed during read")
        return raw

    def stat(self, ref: str, *, scope) -> dict[str, Any]:
        return self._store.stat(ref, scope=cas_scope_id(scope)).to_dict()

    def save(self, ref: str, path: str, *, scope, overwrite: bool = False, cancellation=None) -> dict[str, Any]:
        try:
            exported = self._store.export_to(ref, path, scope=cas_scope_id(scope),
                                             overwrite=overwrite, cancellation=cancellation).to_dict()
        except FileExistsError as exc:
            raise ToolError(f"FileExistsError: {exc}",
                            code="artifact_destination_exists", cause_class="model") from exc
        # Exact report values come from the verified export, not model prose.
        # Retain this receipt in CAS; writing a sidecar remains an explicit
        # caller action through the same ArtifactBlob.save capability.
        result = {"schema": "variant1.artifact-save-result.v1", **exported}
        try:
            receipt = self._store.put_json(
                {"schema": "variant1.artifact-export-receipt.v1", **exported},
                kind="artifact_export_receipt", scope=cas_scope_id(scope),
            )
            result["receipt_ref"] = receipt.ref
        except Exception as exc:
            # The primary export already completed. Never misreport it as a
            # failed write or encourage replay because receipt storage failed.
            result["receipt_error"] = f"{type(exc).__name__}: {exc}"[:500]
        return result


__all__ = ["ArtifactBlobService"]
