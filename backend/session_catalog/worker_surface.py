"""Assignment for durable headless run sources."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any

from session_catalog.profiles import (
    ACTION_SURFACE,
    WORKER_GRAPH_REVISION,
    canonical_action_surface,
    canonical_graph_revision,
    is_action_surface,
)
from runtime_profiles import identity_for_profile


_WORKER_SOURCES = frozenset({"automation"})
_SAFE_KEY = re.compile(r"[^A-Za-z0-9._-]+")


def worker_runtime_id(source: str, key: str) -> str:
    worker_source = str(source or "").strip().lower()
    if worker_source not in _WORKER_SOURCES:
        raise ValueError(f"unsupported durable worker source: {worker_source!r}")
    raw_key = str(key or "default").strip() or "default"
    clean_key = _SAFE_KEY.sub("-", raw_key).strip("-.") or "default"
    prefix = f"worker:{worker_source}:"
    if len(prefix) + len(clean_key) > 240:
        digest = hashlib.sha256(raw_key.encode("utf-8", errors="replace")).hexdigest()[:20]
        clean_key = clean_key[: 239 - len(prefix) - len(digest)] + "-" + digest
    return prefix + clean_key


def _resume_contract(resume_state: dict[str, Any] | None) -> tuple[str, str, str]:
    state = dict(resume_state or {})
    return (
        canonical_action_surface(state.get("action_surface")),
        str(state.get("provider_tool_schema_revision") or "").strip(),
        str(state.get("graph_revision") or "").strip(),
    )


def prepare_worker_surface(
    host: Any,
    *,
    source: str,
    key: str,
    query: str = "",
    resume_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve and persist one worker on VARIANT-1's only action surface."""
    installed = host.require_runtime()
    runtime_id = worker_runtime_id(source, key)
    registry = installed.session_runtimes
    record = registry.runtime(runtime_id)
    saved_surface, saved_schema, saved_graph = _resume_contract(resume_state)
    if record is None:
        requested = canonical_action_surface(saved_surface or ACTION_SURFACE)
        saved_graph = canonical_graph_revision(saved_graph) if saved_graph else saved_graph
        if not is_action_surface(requested):
            raise RuntimeError(
                f"worker checkpoint uses unsupported action surface {requested!r}"
            )
        if saved_graph and saved_graph != WORKER_GRAPH_REVISION:
            raise RuntimeError(
                "worker checkpoint graph/profile contract is inconsistent "
                f"({saved_graph!r} != {WORKER_GRAPH_REVISION!r})"
            )
        identity = identity_for_profile(
            host,
            installed.catalog,
            requested,
            graph_revision=WORKER_GRAPH_REVISION,
            provider_tool_schema_revision=saved_schema,
        )
        record = registry.ensure_worker_runtime(
            runtime_id,
            source=source,
            identity=identity,
        )
    else:
        record = registry.ensure_worker_runtime(
            runtime_id,
            source=source,
            identity=record.identity,
        )
    identity = record.identity
    if not is_action_surface(identity.action_surface):
        raise RuntimeError(
            f"worker uses unsupported action surface {identity.action_surface!r}"
        )
    if canonical_graph_revision(identity.graph_revision) != WORKER_GRAPH_REVISION:
        raise RuntimeError(
            "worker runtime has an incompatible graph revision "
            f"({identity.graph_revision!r} != {WORKER_GRAPH_REVISION!r})"
        )
    if saved_surface and saved_surface != identity.action_surface:
        raise RuntimeError(
            "worker checkpoint action surface differs from its pinned runtime "
            f"({saved_surface!r} != {identity.action_surface!r})"
        )
    if saved_graph and saved_graph != identity.graph_revision:
        raise RuntimeError(
            "worker checkpoint graph revision differs from its pinned runtime "
            f"({saved_graph!r} != {identity.graph_revision!r})"
        )

    from session_catalog.service import IPYTHON_PROVIDER_SPEC

    provider_specs = [json.loads(json.dumps(IPYTHON_PROVIDER_SPEC))]
    runtime_prompt = installed.catalog.runtime_prompt(runtime_id, str(query or ""))
    return {
        "schema": "variant1.astb.worker-surface.v1",
        "source": str(source),
        "runtime_id": runtime_id,
        "action_surface": identity.action_surface,
        "provider_tool_schema_revision": identity.provider_tool_schema_revision,
        "graph_revision": identity.graph_revision,
        "runtime_identity": record.to_dict(),
        "runtime_prompt": runtime_prompt,
        "provider_specs": provider_specs,
        "mutation_enabled": bool(record.mutation_write_enabled),
    }


async def begin_worker_run(
    host: Any,
    assignment: dict[str, Any],
    *,
    thread_id: str,
    run_id: str,
) -> str:
    runtime_id = str(assignment.get("runtime_id") or "")
    source = str(assignment.get("source") or "")
    runtimes = host.require_runtime().session_runtimes
    runtimes.link_worker_thread(
        runtime_id, thread_id, source=source
    )
    admission_id = await runtimes.reserve_run(
        runtime_id,
        background=True,
    )
    if not admission_id:
        raise RuntimeError(f"worker runtime is already busy: {runtime_id}")
    task = asyncio.current_task()
    if task is not None:
        runtimes.bind_admission_task(admission_id, task)
    runtimes.begin_run(
        admission_id,
        run_id=str(run_id or runtime_id),
        thread_id=str(thread_id or runtime_id),
        source=source,
    )
    return admission_id


def finish_worker_run(host: Any, admission_id: str, *, status: str) -> None:
    if admission_id:
        host.require_runtime().session_runtimes.finish_run(
            admission_id, status=str(status or "completed")
        )


async def delete_worker_runtime(host: Any, *, source: str, key: str) -> bool:
    return await host.require_runtime().session_runtimes.delete_worker_runtime(
        worker_runtime_id(source, key),
        source=source,
    )
