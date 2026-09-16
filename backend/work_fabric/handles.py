"""Host-side envelopes for reconstructable IPython remote handles."""

from __future__ import annotations

from typing import Any

from capability_broker import InvocationContext

from .models import JobRecord


REMOTE_HANDLE_SCHEMA = "variant1.remote-handle-dispatch.v1"
REMOTE_HANDLE_METHODS_SCHEMA = "variant1.remote-handle-methods.v1"
REMOTE_HANDLE_DISPATCH_CAPABILITY = "remote_handle_dispatch"


def _dispatch_descriptor(broker: Any, context: InvocationContext) -> dict[str, Any]:
    ref = broker.ref_for_name(
        REMOTE_HANDLE_DISPATCH_CAPABILITY,
        catalog_release_id=context.catalog_release_id,
    )
    return {
        "schema": REMOTE_HANDLE_SCHEMA,
        "ref_id": ref.opaque_id,
        "handler_revision": ref.handler_revision,
        "catalog_release_id": ref.catalog_release_id,
        "category_id": "",
        "slot_id": ref.slot_id,
        "slot_version": int(ref.slot_version),
        "mount_revision": int(context.mount_revision),
    }


def job_handle_envelope(
    job: JobRecord,
    *,
    broker: Any,
    context: InvocationContext,
) -> dict[str, Any]:
    """Return a bounded envelope; the live JobRecord remains host-owned."""
    progress: dict[str, Any] = {}
    for key in ("phase", "current", "total", "percent", "message"):
        value = job.progress.get(key)
        if isinstance(value, str):
            progress[key] = value[:500]
        elif value is None or isinstance(value, (bool, int, float)):
            if value is not None:
                progress[key] = value
    return remote_handle_envelope(
        service="work",
        kind="job",
        handle_id=job.job_id,
        generation=1,
        revision=int(job.revision),
        metadata={
            "kind": job.kind,
            "status": job.status,
            "owner_kind": job.owner_kind,
            "owner_id": job.owner_id,
            "progress": progress,
            "updated_at": float(job.updated_at),
            "terminal": bool(job.terminal),
        },
        methods=[
            {
                "name": "refresh",
                "description": "Return the latest durable job state.",
                "params": [],
                "returns": "job",
            },
            {
                "name": "cancel",
                "control": True,
                "description": "Request cancellation of this job.",
                "params": [{
                    "name": "reason", "type": "str", "required": False,
                    "default": "cancelled_from_kernel",
                }],
                "returns": "job",
            },
            {
                "name": "wait",
                "description": "Wait up to timeout_s for a terminal job state.",
                "params": [{
                    "name": "timeout_s", "type": "float", "required": False,
                    "default": 30.0,
                }],
                "returns": "job",
            },
            {
                "name": "events",
                "description": "Read a bounded page of durable job events.",
                "params": [
                    {
                        "name": "after_sequence", "type": "int",
                        "required": False, "default": 0,
                    },
                    {
                        "name": "limit", "type": "int",
                        "required": False, "default": 100,
                    },
                ],
                "returns": "list",
            },
        ],
        broker=broker,
        context=context,
    )


def remote_handle_envelope(
    *,
    service: str,
    kind: str,
    handle_id: str,
    generation: int,
    revision: int,
    metadata: dict[str, Any],
    methods: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    broker: Any,
    context: InvocationContext,
) -> dict[str, Any]:
    """Build one descriptor-routed, reconnectable kernel handle envelope."""

    handle_metadata = dict(metadata)
    handle_metadata["cell_origin"] = context.cell_origin.to_dict()
    payload = {
            "service": str(service),
            "kind": str(kind),
            "id": str(handle_id),
            "generation": int(generation),
            "revision": int(revision),
            "metadata": handle_metadata,
            "_dispatch": _dispatch_descriptor(broker, context),
    }
    if methods:
        payload["methods"] = {
            "schema": REMOTE_HANDLE_METHODS_SCHEMA,
            "items": [dict(item) for item in list(methods)[:64]],
        }
    return {"$variant1_handle": payload}


__all__ = [
    "REMOTE_HANDLE_DISPATCH_CAPABILITY",
    "REMOTE_HANDLE_SCHEMA",
    "REMOTE_HANDLE_METHODS_SCHEMA",
    "job_handle_envelope",
    "remote_handle_envelope",
]
