"""WorkScope propagation across native state, run context, and receipts."""

from __future__ import annotations

import pytest

from agent_engine.state import new_run_state
from capability_broker import CapabilityBroker, InvocationContext
from run_context import Variant1RunContext, bind_run_context
from chat_session import ConnectionSession
from tools import Tool, ToolRegistry
from work_fabric.scope import (
    WorkScope,
    current_work_scope,
    effective_work_scope,
    scoped_owner_id,
)
from tests.support.astb_runtime import StaticRuntimeRegistry


def _scope() -> WorkScope:
    return WorkScope(
        chat_id="chat-1",
        conversation_id="conversation-1",
        branch_id="branch-main",
        workspace_id="workspace-1",
        workspace_revision=4,
        goal_id="goal-1",
        goal_run_id="goal-run-2",
        step_id="step-3",
        attempt=2,
        worktree_id="worktree-8",
        kernel_generation=7,
        catalog_release_id="catalog-5",
    )


def test_work_scope_is_bounded_immutable_and_snapshotted():
    scope = _scope()
    state = new_run_state(
        source="chat",
        title="Scoped run",
        goal="Keep attribution",
        work_scope={**scope.to_dict(), "ignored": "not persisted"},
    )

    assert state["work_scope"] == scope.to_dict()
    assert "ignored" not in state["work_scope"]
    with pytest.raises((AttributeError, TypeError)):
        scope.goal_id = "other"


def test_effective_work_scope_fills_only_a_missing_chat_identity():
    base = dict(
        run_id="run",
        outer_tool_call_id="outer",
        cell_execution_id="cell",
        nested_call_id="nested",
        catalog_release_id="astb.test.release.v1",
    )
    inferred = effective_work_scope(InvocationContext(
        chat_id="chat-host",
        work_scope=WorkScope(workspace_id="workspace"),
        **base,
    ))
    pinned = effective_work_scope(InvocationContext(
        chat_id="chat-host",
        work_scope=WorkScope(chat_id="chat-scope", workspace_id="workspace"),
        **base,
    ))

    assert inferred == WorkScope(chat_id="chat-host", workspace_id="workspace")
    assert pinned.chat_id == "chat-scope"


def test_scoped_owner_id_has_one_chat_binding_precedence():
    scope = WorkScope(
        conversation_id="conversation",
        chat_id="chat",
        branch_id="branch",
    )

    assert scoped_owner_id("chat", "fallback", scope) == "branch"
    assert scoped_owner_id("run", "run-owner", scope) == "run-owner"
    assert scoped_owner_id("chat", "fallback", WorkScope(chat_id="chat")) == "chat"
    assert scoped_owner_id("chat", "fallback", WorkScope()) == "fallback"


def test_bound_run_context_binds_and_restores_work_scope():
    outer = current_work_scope()
    scope = _scope()
    context = Variant1RunContext.create(source="chat", work_scope=scope)

    with bind_run_context(context):
        assert current_work_scope() == scope
    assert current_work_scope() == outer


def test_viewed_chat_projects_into_run_scope():
    session = ConnectionSession(viewed_session_id="chat-main")
    host = type("Host", (), {
        "sessions": type("Sessions", (), {"get_active": lambda self: "other"})(),
        "session_runtimes": None,
        "emit_activity": None,
    })()

    from host_run_context import make_run_context

    context = make_run_context(host, "chat", "turn", session=session)
    assert context.work_scope.to_dict(include_empty=False) == {
        "chat_id": "chat-main",
    }

    session.viewed_session_id = "another-chat"
    context = make_run_context(host, "chat", "turn", session=session)
    assert context.work_scope.chat_id == "another-chat"

@pytest.mark.asyncio
async def test_capability_receipt_contains_bounded_work_scope():
    registry = ToolRegistry()

    async def read_value(_args):
        return {"value": 7}

    registry.register(Tool(
        "read_value",
        "Return one value.",
        read_value,
        effect_class="read",
        may_return_secrets=False,
    ))
    broker = CapabilityBroker(
        registry=registry,
        runtime_registry=StaticRuntimeRegistry(),
        enabled_resolver=lambda: {"read_value"},
    )
    scope = _scope()
    context = InvocationContext(
        chat_id=scope.chat_id,
        run_id="run-1",
        outer_tool_call_id="outer-1",
        cell_execution_id="cell-1",
        nested_call_id="nested-1",
        catalog_release_id="astb.test.release.v1",
        work_scope=scope.to_dict(),
    )

    receipt = await broker.invoke_name("read_value", {}, context)

    assert receipt.ok
    assert receipt.attribution["work_scope"] == scope.to_dict()
