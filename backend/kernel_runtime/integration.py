"""Provider seam for VARIANT-1's single persistent-Python action surface."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
from typing import Any, Iterator

import background_tasks

from prompt_builder import PromptProjection
from run_context import current_run_context
from tools import Tool, ToolError
from tool_core import ToolProjectionResult

from session_catalog.profiles import (
    CHAT_GRAPH_REVISION,
    ACTION_SURFACE,
    IPYTHON_SCHEMA_REVISION,
    is_action_surface,
)
from session_catalog.service import IPYTHON_PROVIDER_SPEC

from .contracts import KernelAutoRestoreError, KernelExecutionError


IPYTHON_TOOL_NAME = "ipython"
OUTER_TOOL_CALL_ID: ContextVar[str] = ContextVar(
    "variant1_outer_tool_call_id", default=""
)


def preserve_persistent_kernel_workspace(
    kernel: Any,
    chat_id: str,
    requested_roots: tuple[str, ...],
    work_scope: dict[str, Any],
) -> tuple[tuple[str, ...], dict[str, Any]]:
    """Keep a live namespace across ordinary chat-project default changes.

    Explicit workspace/worktree identities and revisions remain hard manager
    fences.  Only an unversioned project default reuses the roots and revision
    with which the existing Python worker was created.
    """

    roots = tuple(str(root) for root in requested_roots if str(root or "").strip())
    scope = dict(work_scope or {})
    explicit_workspace = bool(
        str(scope.get("workspace_id") or "").strip()
        or str(scope.get("worktree_id") or "").strip()
        or int(scope.get("workspace_revision") or 0) > 0
    )
    if explicit_workspace:
        return roots, scope
    status = kernel.status(str(chat_id or ""))
    if str(status.get("state") or "") not in {"ready", "busy"}:
        return roots, scope
    pinned_roots = tuple(
        str(root) for root in (status.get("workspace_roots") or ())
        if str(root or "").strip()
    )
    if not pinned_roots:
        return roots, scope
    scope["workspace_revision"] = max(
        0, int(status.get("workspace_revision") or 0),
    )
    return pinned_roots, scope


def _mount_selection_observation(
    selection: dict[str, Any], *, fallback_category: str
) -> str:
    """Project a category mount without leaking its host bookkeeping."""

    category_id = str(
        selection.get("category_id") or fallback_category or "category"
    )
    if selection.get("unchanged"):
        return (
            f"{category_id.title()} is already mounted. Execute Python code now "
            "or choose another category."
        )
    card = str(selection.get("mount_card") or "").strip()
    mutation = (
        selection.get("mutation")
        if isinstance(selection.get("mutation"), dict)
        else {}
    )
    authority = (
        mutation.get("authority")
        if isinstance(mutation.get("authority"), dict)
        else {}
    )
    mutation_enabled = bool(
        authority.get("effective_write_enabled")
        if "effective_write_enabled" in authority
        else authority.get("write_enabled")
    )
    status = (
        "Mutation: on. When a mounted callable blocks progress, use "
        "toolbelt.mutate(toolbelt.last_failure(), using=helper, invoke={...}); "
        "preserve a reusable helper with "
        "toolbelt.synthesize(helper, invoke={...})."
        if mutation_enabled
        else "Mutation: off."
    )
    return "\n".join(part for part in (card, status) if part)


@contextmanager
def bind_outer_tool_call_id(call_id: str) -> Iterator[None]:
    token = OUTER_TOOL_CALL_ID.set(str(call_id or ""))
    try:
        yield
    finally:
        OUTER_TOOL_CALL_ID.reset(token)


def current_runtime_record(session_runtimes: Any, *, session: Any = None) -> Any:
    chat_id = ""
    ctx = current_run_context()
    if ctx is not None:
        chat_id = str((ctx.metadata or {}).get("chat_id") or "")
    if not chat_id and session is not None:
        active = getattr(session, "active", None)
        chat_id = str(
            getattr(active, "turn_session_id", "")
            or getattr(session, "viewed_session_id", "")
            or ""
        )
    if not chat_id:
        return None
    if session_runtimes is None:
        return None
    return session_runtimes.ensure_runtime(chat_id)


def is_ipython_record(record: Any) -> bool:
    surface = str(
        getattr(getattr(record, "identity", None), "action_surface", "")
    )
    return is_action_surface(surface)


def _require_astb_record(record: Any) -> Any:
    if record is None:
        raise RuntimeError("model request has no durable VARIANT-1 runtime")
    surface = str(getattr(record.identity, "action_surface", ""))
    if surface != ACTION_SURFACE:
        raise RuntimeError(
            f"chat uses unsupported action surface {surface!r}; "
            "start a new VARIANT-1 chat"
        )
    return record


def provider_specs(session_runtimes: Any, session: Any) -> list[dict]:
    _require_astb_record(current_runtime_record(session_runtimes, session=session))
    return [json.loads(json.dumps(IPYTHON_PROVIDER_SPEC))]


def runtime_prompt_projection(
    catalog: Any,
    session_runtimes: Any,
    session: Any,
    query: str = "",
) -> PromptProjection:
    record = _require_astb_record(
        current_runtime_record(session_runtimes, session=session)
    )
    if catalog is None:
        raise RuntimeError("runtime prompt requested without a capability catalog")
    projection = catalog.runtime_prompt_projection(
        record.chat_id, str(query or "")
    )
    if not isinstance(projection, PromptProjection) or not projection.stable:
        raise RuntimeError("runtime prompt projection is unavailable")
    return projection


def runtime_prompt(
    catalog: Any,
    session_runtimes: Any,
    session: Any,
    query: str = "",
) -> str:
    """Preserve the combined prompt contract for non-chat integrations."""

    return runtime_prompt_projection(
        catalog,
        session_runtimes,
        session,
        query,
    ).combined()


def graph_revision(session_runtimes: Any, session: Any) -> str:
    record = _require_astb_record(
        current_runtime_record(session_runtimes, session=session)
    )
    return str(record.identity.graph_revision or CHAT_GRAPH_REVISION)


def runtime_identity(session_runtimes: Any, session: Any) -> dict[str, Any]:
    """Return the exact durable profile pinned to the current chat."""
    record = _require_astb_record(
        current_runtime_record(session_runtimes, session=session)
    )
    # Include only checkpoint-relevant mutable authority beside the pinned
    # identity; unrelated budgets and lifecycle fields stay out of run metadata.
    return {
        **record.identity.to_dict(),
        "mutation_write_enabled": bool(record.mutation_write_enabled),
        "mutation_authority_revision": int(
            record.mutation_authority_revision
        ),
    }


def broker_enabled_names(registry: Any) -> set[str]:
    """All registered handlers; catalog grants own model admission."""
    if registry is None:
        raise RuntimeError("CapabilityBroker requires the runtime registry")
    return {
        str(tool.name)
        for tool in registry.all()
        if str(getattr(tool, "name", "") or "").strip()
    }


def register_ipython_tool(registry: Any, runtime_provider) -> None:
    if registry is None or not callable(runtime_provider):
        raise TypeError("registry and runtime_provider are required")
    if registry.get(IPYTHON_TOOL_NAME) is not None:
        return

    async def _execute(args: dict[str, Any]) -> str:
        def auto_restore_error(exc: KernelAutoRestoreError) -> ToolError:
            return ToolError(json.dumps(
                {
                    "schema": "variant1.kernel-auto-restore-error.v1",
                    "error": {
                        "code": exc.code,
                        "message": str(exc),
                    },
                    "outcome": dict(exc.outcome),
                },
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ))

        code = args.get("code")
        category = args.get("category")
        nonempty_code = isinstance(code, str) and bool(code.strip())
        nonempty_category = isinstance(category, str) and bool(category.strip())
        if not nonempty_code and not nonempty_category:
            raise ToolError(
                "ipython requires a non-empty category, code, or both"
            )
        ctx = current_run_context()
        if ctx is None:
            raise ToolError("ipython requires a bound VARIANT-1 run context")
        chat_id = str((ctx.metadata or {}).get("chat_id") or "").strip()
        if not chat_id:
            raise ToolError("ipython requires a durable chat identity")
        runtime = runtime_provider()
        record = runtime.session_runtimes.ensure_runtime(chat_id)
        if not is_ipython_record(record):
            raise ToolError(
                f"chat profile {record.identity.action_surface!r} does not admit ipython"
            )
        # Provider/model eligibility is checked against the pinned support
        # matrix before provider I/O.  "trusted-local" describes the execution
        # boundary, not whether the reasoning model itself is local or cloud.
        selection: dict[str, Any] | None = None
        mount_observation = ""
        if nonempty_category:
            try:
                selection = runtime.catalog.select(chat_id, str(category))
            except Exception as exc:
                raise ToolError(str(exc)) from exc
            if not nonempty_code:
                return ToolProjectionResult(
                    _mount_selection_observation(
                        selection, fallback_category=str(category or "")
                    ),
                    programmatic_value=selection,
                    receipt_metadata={
                        "projection": "astb-mount-selection-v2",
                        "category_id": str(
                            selection.get("category_id") or category or ""
                        ),
                        "mount_revision": int(
                            selection.get("mount_revision") or 0
                        ),
                        "unchanged": bool(selection.get("unchanged")),
                    },
                )
            if not bool(selection.get("unchanged")):
                mount_observation = _mount_selection_observation(
                    selection, fallback_category=str(category or "")
                )
        from project_context import current_project_context

        roots = current_project_context(runtime.kernel.app_root).roots
        work_scope = (
            ctx.work_scope.to_dict()
            if callable(getattr(getattr(ctx, "work_scope", None), "to_dict", None))
            else dict(getattr(ctx, "work_scope", None) or {})
        )
        roots, work_scope = preserve_persistent_kernel_workspace(
            runtime.kernel, chat_id, roots, work_scope,
        )
        def on_chunk(chunk: dict[str, Any]) -> None:
            sink = getattr(ctx, "activity_sink", None)
            if callable(sink):
                background_tasks.spawn(
                    sink(
                        "kernel:output",
                        run_id=ctx.run_id,
                        source=ctx.source,
                        text=str(chunk.get("text") or "")[:400],
                        media_type=str(chunk.get("media_type") or "text/plain"),
                    ),
                    name="kernel-output-activity",
                )

        try:
            result = await runtime.kernel.execute(
                chat_id=chat_id,
                code=str(code),
                run_id=ctx.run_id,
                outer_tool_call_id=OUTER_TOOL_CALL_ID.get() or "ipython-call",
                workspace_roots=roots,
                work_scope=work_scope,
                cancellation=ctx.should_stop,
                on_chunk=on_chunk,
            )
        except KernelAutoRestoreError as exc:
            raise auto_restore_error(exc) from exc
        if not result.ok:
            # Selection has already changed the live namespace. A Python error
            # must not hide the new callable contract from the receiving model.
            raise KernelExecutionError(result, context=mount_observation)
        model_observation = result.model_observation()
        # A terminal nested result is itself the final user-facing projection;
        # do not prefix a mount card that no later model turn can consume.
        if mount_observation and not bool(result.terminate):
            model_observation = mount_observation + "\n\n" + model_observation
        receipt_metadata = {
            "projection": "kernel-execution-v2",
            "execution_id": result.execution_id,
            "kernel_generation": int(result.generation),
            "ledger_sequence": int(result.ledger_sequence),
        }
        if selection is not None:
            receipt_metadata.update({
                "category_id": str(
                    selection.get("category_id") or category or ""
                ),
                "mount_revision": int(selection.get("mount_revision") or 0),
                "mount_disclosed": bool(
                    mount_observation and not bool(result.terminate)
                ),
            })
        return ToolProjectionResult(
            model_observation,
            programmatic_value=result.to_dict(),
            receipt_metadata=receipt_metadata,
            terminate=bool(result.terminate),
        )

    registry.register(Tool(
        name=IPYTHON_TOOL_NAME,
        description=str(IPYTHON_PROVIDER_SPEC["description"]),
        handler=_execute,
        category="infrastructure",
        params=json.loads(json.dumps(IPYTHON_PROVIDER_SPEC["params"])),
        hidden=True,
        visibility="provider",
        effect_class="external_side_effect",
        parallel_safe=False,
        idempotency="none",
        # The persistent Python surface can legitimately supervise long-running
        # work. Stop and Steer are explicit cancellation authorities; elapsed
        # wall time alone is not.
        default_deadline_ms=0,
        schema_revision=f"variant1.{IPYTHON_SCHEMA_REVISION}",
        handler_revision="variant1.ipython.kernel-manager.v6",
        result_projection="kernel-execution-v2",
    ))
