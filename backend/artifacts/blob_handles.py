"""Bound CAS references using the existing artifact service and handle broker."""

from __future__ import annotations

from typing import Any

from work_fabric.handles import remote_handle_envelope
from work_fabric.scope import effective_work_scope


BLOB_HANDLE_METHODS = [
    {
        "name": "save",
        "description": "Save original bytes; return verified size/SHA-256 and a machine-generated JSON receipt handle. result.receipt.save(path) delivers exact export facts as a file. Existing paths require overwrite=True or a different destination.",
        "params": [
            {"name": "path", "type": "str", "required": True},
            {"name": "overwrite", "type": "bool", "required": False, "default": False},
        ],
        "returns": "object",
    },
    {
        "name": "read_bytes",
        "description": "Read complete Python bytes. For large files use save(path). size is the byte count.",
        "params": [
            {"name": "max_bytes", "type": "int", "required": False, "default": 4_194_304},
        ],
        "returns": "bytes",
    },
]


def blob_handle_envelope(host: Any, context: Any, ref: str, *, metadata=None) -> dict[str, Any]:
    runtime = host.require_runtime()
    stat = runtime.artifacts.blobs.stat(ref, scope=effective_work_scope(context))
    return remote_handle_envelope(
        service="artifacts", kind="blob", handle_id=ref, generation=1, revision=1,
        metadata={
            **dict(metadata or {}),
            **{key: value for key, value in stat.items() if key != "kind"},
            "blob_kind": stat.get("kind", ""),
            "ref": ref, "size": stat["bytes"],
        },
        methods=BLOB_HANDLE_METHODS, broker=runtime.broker, context=context,
    )
