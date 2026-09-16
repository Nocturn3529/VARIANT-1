"""Unified runtime context for VARIANT-1 agent runs.

``RunState`` remains the serializable native snapshot state. This module
holds the live per-run objects that should not be serialized: Fabric bindings,
interactive chat transport, image sinks, activity sinks, cancellation, and
working-directory and tool-disclosure state.

The ContextVar carries this same run identity into tool handlers and desktop
facade calls. Prefer explicit parameters at module boundaries; use
``current_run_context()`` for callbacks that inherit the active run.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass, field
from contextvars import ContextVar, Token
from typing import Any, Awaitable, Callable, Iterator

from core_invariants import cancellation_is_requested

from work_fabric.scope import (
    WorkScope,
    bind_work_scope,
    coerce_work_scope,
)


ActivitySink = Callable[..., Awaitable[None]]


@dataclass
class Variant1RunContext:
    """Live, per-run runtime context.

    Belongs here: identity, parentage, run-scoped cancellation, desktop binding,
    image sink, activity sink, and runtime configuration that should be stable
    for this run.

    Does not belong here: app-wide services such as the LLM router, global tool
    registry, persisted config stores, memory database, MCP
    client, or WebSocket hub. Those remain application singletons and should be
    injected explicitly where possible.
    """

    run_id: str
    source: str
    work_scope: WorkScope = field(default_factory=WorkScope)
    thread_id: str = ""
    parent_run_id: str = ""
    session_id: str = ""
    # The interactive ConnectionSession this run belongs to (or None for background
    # runs). Run-scoped live object, like desktop_binding — the concurrency-safe
    # replacement for the old process-global _ACTIVE_SESSION.
    chat_session: Any = None
    # Interactive transport used by structured clarification. It is copied
    # explicitly when the chat graph rebinds this live context and is never
    # placed in serializable RunState or the generic metadata bag.
    chat_transport: Any = None
    desktop_binding: Any = None
    browser_binding: Any = None
    image_sink: dict | None = None
    activity_sink: ActivitySink | None = None
    cancellation: Any = None
    run_config: Any = None
    # Complete task catalog plus the provider-visible subset for this run.
    # These live schemas intentionally stay out of checkpoints.
    available_tool_specs: tuple[dict, ...] = field(default_factory=tuple)
    disclosed_tool_specs: tuple[dict, ...] = field(default_factory=tuple)
    # Live metadata-only sidecar describing how model-visible context was
    # selected and transformed. It is deliberately separate from checkpointed
    # snapshot state and never contains prompt/tool/image values.
    model_input_receipt: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        source: str,
        work_scope: WorkScope | dict[str, Any] | None = None,
        title: str = "",
        run_id: str | None = None,
        thread_id: str = "",
        parent_run_id: str = "",
        session_id: str = "",
        chat_session: Any = None,
        chat_transport: Any = None,
        desktop_binding: Any = None,
        browser_binding: Any = None,
        image_sink: dict | None = None,
        activity_sink: ActivitySink | None = None,
        cancellation: Any = None,
        run_config: Any = None,
        available_tool_specs: Any = None,
        disclosed_tool_specs: Any = None,
        model_input_receipt: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "Variant1RunContext":
        rid = run_id or uuid.uuid4().hex[:12]
        return cls(
            run_id=rid,
            source=source,
            work_scope=coerce_work_scope(work_scope),
            thread_id=thread_id or rid,
            parent_run_id=parent_run_id or "",
            session_id=session_id or "",
            chat_session=chat_session,
            chat_transport=chat_transport,
            desktop_binding=desktop_binding,
            browser_binding=browser_binding,
            image_sink=image_sink,
            activity_sink=activity_sink,
            cancellation=cancellation,
            run_config=run_config,
            available_tool_specs=tuple(
                dict(spec) for spec in (available_tool_specs or ()) if isinstance(spec, dict)
            ),
            disclosed_tool_specs=tuple(
                dict(spec) for spec in (disclosed_tool_specs or ()) if isinstance(spec, dict)
            ),
            model_input_receipt=model_input_receipt,
            metadata={"title": (title or "").strip()[:200], **dict(metadata or {})},
        )

    @property
    def title(self) -> str:
        return str((self.metadata or {}).get("title") or "")

    @property
    def desktop_binding_id(self) -> str:
        if self.desktop_binding is not None:
            return str(getattr(self.desktop_binding, "binding_id", "") or "")
        return ""

    def should_stop(self) -> bool:
        return cancellation_is_requested(self.cancellation)

    def activity_fields(self) -> dict[str, Any]:
        fields = {
            "run_id": self.run_id,
            "source": self.source,
            "work_scope": self.work_scope.to_dict(include_empty=False),
        }
        binding_id = self.desktop_binding_id
        if binding_id:
            fields["desktop_binding_id"] = binding_id
        return fields


CURRENT_RUN_CONTEXT: ContextVar[Variant1RunContext | None] = ContextVar(
    "variant1_run_context",
    default=None,
)


def current_run_context() -> Variant1RunContext | None:
    return CURRENT_RUN_CONTEXT.get()


def clear_run_context() -> None:
    CURRENT_RUN_CONTEXT.set(None)


@contextlib.contextmanager
def bind_run_context(ctx: Variant1RunContext | None) -> Iterator[Variant1RunContext | None]:
    token: Token = CURRENT_RUN_CONTEXT.set(ctx)
    scope_binding = bind_work_scope(ctx.work_scope if ctx is not None else None)
    scope_binding.__enter__()
    desktop_token = None
    browser_token = None
    image_token = None
    try:
        if ctx is not None:
            if ctx.desktop_binding is not None:
                try:
                    from desktop_fabric.binding import CURRENT_DESKTOP_BINDING

                    desktop_token = CURRENT_DESKTOP_BINDING.set(ctx.desktop_binding)
                except Exception:
                    desktop_token = None
            if ctx.browser_binding is not None:
                try:
                    from browser_fabric.binding import CURRENT_BROWSER_BINDING

                    browser_token = CURRENT_BROWSER_BINDING.set(ctx.browser_binding)
                except Exception:
                    browser_token = None
            if ctx.image_sink is not None:
                try:
                    import desktop.service as dservice

                    image_token = dservice.install_image_sink(ctx.image_sink)
                except Exception:
                    image_token = None
        yield ctx
    finally:
        if image_token is not None:
            try:
                import desktop.service as dservice

                dservice.reset_image_sink(image_token)
            except Exception:
                pass
        if desktop_token is not None:
            try:
                from desktop_fabric.binding import CURRENT_DESKTOP_BINDING

                CURRENT_DESKTOP_BINDING.reset(desktop_token)
            except Exception:
                pass
        if browser_token is not None:
            try:
                from browser_fabric.binding import CURRENT_BROWSER_BINDING

                CURRENT_BROWSER_BINDING.reset(browser_token)
            except Exception:
                pass
        scope_binding.__exit__(None, None, None)
        CURRENT_RUN_CONTEXT.reset(token)
