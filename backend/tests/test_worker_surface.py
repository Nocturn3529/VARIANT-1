"""Contracts for durable automation action surfaces."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_engine.config import AgentRunConfig, validate_config
from agent_engine.run_contract import validate_checkpoint_contract
from session_catalog.profiles import (
    ACTION_SURFACE,
    IPYTHON_SCHEMA_REVISION,
    WORKER_GRAPH_REVISION,
)
from session_catalog.worker_surface import (
    begin_worker_run,
    finish_worker_run,
    prepare_worker_surface,
    worker_runtime_id,
)
from session_runtime import RuntimeIdentity, SessionRuntimeRegistry, SessionRuntimeRepository
from tools import ToolRegistry
from ws_automations import _automation_items


class _Control:
    def __init__(self, profile: str):
        self.profile = profile

    def profile_for_new_chat(self, _runtime_id: str) -> str:
        return self.profile


class _Catalog:
    def identity(self, *, environment_digest: str):
        return RuntimeIdentity(
            action_surface=ACTION_SURFACE,
            provider_tool_schema_revision=IPYTHON_SCHEMA_REVISION,
            graph_revision="chat.ipython.v2",
            catalog_release_id="catalog-test",
            environment_digest=environment_digest,
            trust_profile="trusted-local.v1",
            disclosure_profile_id="disclosure.topk.v1",
            disclosure_profile_revision="1",
        )

    @staticmethod
    def runtime_prompt(runtime_id: str, query: str) -> str:
        return f"worker {runtime_id}: {query}"


def _host(tmp_path, *, profile: str = ACTION_SURFACE):
    repository = SessionRuntimeRepository(str(tmp_path / "runtime.sqlite3"))
    runtimes = SessionRuntimeRegistry(repository)
    control = _Control(profile)
    runtime = SimpleNamespace(
        registry=ToolRegistry(),
        session_runtimes=runtimes,
        session_control=control,
        catalog=_Catalog(),
        kernel=SimpleNamespace(status=lambda runtime_id: {
            "state": "absent", "generation": 0, "pid": None,
            "runtime_id": runtime_id,
        }),
    )
    host = SimpleNamespace(
        version="test",
        require_runtime=lambda: runtime,
    )
    return host, control, repository


def test_new_unattended_worker_uses_the_single_surface_and_stays_pinned(
    tmp_path,
):
    host, control, _repository = _host(
        tmp_path, profile="native-tools.v1"
    )

    first = prepare_worker_surface(
        host,
        source="automation",
        key="daily digest",
        query="summarize today",
    )
    runtime_id = worker_runtime_id("automation", "daily digest")

    assert first["runtime_id"] == runtime_id
    assert first["action_surface"] == ACTION_SURFACE
    assert first["graph_revision"] == WORKER_GRAPH_REVISION
    assert first["provider_tool_schema_revision"] == IPYTHON_SCHEMA_REVISION
    assert [row["name"] for row in first["provider_specs"]] == ["ipython"]
    assert first["mutation_enabled"] is False
    assert runtime_id in first["runtime_prompt"]
    assert host.require_runtime().session_runtimes.runtime(
        runtime_id
    ).creation_saga_state == (
        "worker:automation"
    )

    control.profile = "native-tools.v1"
    replay = prepare_worker_surface(
        host,
        source="automation",
        key="daily digest",
        query="a later run",
    )
    assert replay["action_surface"] == ACTION_SURFACE
    assert replay["runtime_identity"]["catalog_release_id"] == "catalog-test"


def test_worker_resume_contract_wins_over_the_new_worker_default(tmp_path):
    host, _control, _repository = _host(tmp_path)
    with pytest.raises(RuntimeError, match="unsupported action surface"):
        prepare_worker_surface(
            host,
            source="automation",
            key="legacy native",
            resume_state={
                "action_surface": "native-tools.v1",
                "provider_tool_schema_revision": "native.saved-schema.v7",
                "graph_revision": "worker.native-tools.v1",
            },
        )


def test_worker_resume_rejects_inconsistent_profile_and_graph(tmp_path):
    host, _control, _repository = _host(tmp_path)
    with pytest.raises(RuntimeError, match="graph/profile contract is inconsistent"):
        prepare_worker_surface(
            host,
            source="automation",
            key="broken",
            resume_state={
                "action_surface": ACTION_SURFACE,
                "provider_tool_schema_revision": IPYTHON_SCHEMA_REVISION,
                "graph_revision": "worker.native-tools.v1",
            },
        )


def test_automation_worker_runtime_is_not_deleted_by_owner_reconciliation(tmp_path):
    host, _control, _repository = _host(tmp_path)
    assignment = prepare_worker_surface(
        host, source="automation", key="nightly", query="run scheduled work"
    )

    host.require_runtime().session_runtimes.reconcile_runtime_owners([])

    record = host.require_runtime().session_runtimes.runtime(
        assignment["runtime_id"]
    )
    assert record is not None
    assert record.lifecycle_state == "active"
    assert record.creation_saga_state == "worker:automation"


@pytest.mark.asyncio
async def test_worker_admission_links_checkpoint_thread_and_reports_busy(tmp_path):
    host, _control, repository = _host(tmp_path)
    assignment = prepare_worker_surface(
        host, source="automation", key="nightly", query="run"
    )
    admission_id = await begin_worker_run(
        host,
        assignment,
        thread_id="automation:nightly",
        run_id="run-nightly",
    )

    snapshot = host.require_runtime().session_runtimes.snapshot(
        assignment["runtime_id"]
    )
    assert snapshot["busy"] is True
    assert snapshot["active_run_id"] == "run-nightly"
    assert repository.thread_refs(assignment["runtime_id"]) == [
        "automation:nightly"
    ]

    finish_worker_run(host, admission_id, status="completed")
    assert host.require_runtime().session_runtimes.snapshot(
        assignment["runtime_id"]
    )["busy"] is False


def test_headless_config_accepts_only_the_ipython_worker_graph():
    validate_config(AgentRunConfig(
        name="automation",
        source="automation",
        action_surface=ACTION_SURFACE,
        provider_tool_schema_revision=IPYTHON_SCHEMA_REVISION,
        graph_revision=WORKER_GRAPH_REVISION,
    ))
    with pytest.raises(ValueError, match="unknown agent source"):
        validate_config(AgentRunConfig(
            name="unknown",
            source="unknown",
            action_surface="ptr-flat.trusted-local.v1",
            provider_tool_schema_revision="ipython.ptr.v1",
            graph_revision="worker.ptr-ipython.v1",
        ))


def test_worker_checkpoint_accepts_only_worker_family_revisions():
    state = {
        "source": "automation",
        "run_id": "run-1",
        "goal": "do work",
        "action_surface": "trusted-local.v1",
        "graph_revision": WORKER_GRAPH_REVISION,
        "state_schema_version": 1,
    }
    accepted, reason = validate_checkpoint_contract(
        state,
        expected_source="automation",
        accept_supported_revision=True,
    )
    assert reason == ""
    assert accepted["graph_revision"] == WORKER_GRAPH_REVISION

    rejected, reason = validate_checkpoint_contract(
        {**state, "graph_revision": "chat.ipython.v2"},
        expected_source="automation",
        accept_supported_revision=True,
    )
    assert rejected is None
    assert "unsupported" in reason


def test_automation_list_does_not_pin_a_worker_runtime(tmp_path):
    host, _control, _repository = _host(
        tmp_path
    )
    host.automations = SimpleNamespace(list=lambda: [{
        "id": "auto-1",
        "name": "Digest",
        "prompt": "summarize",
    }])
    host.require_runtime().kernel = SimpleNamespace(status=lambda runtime_id: {
        "state": "ready",
        "generation": 2,
        "pid": 123,
        "runtime_id": runtime_id,
    })
    prepared = []
    host.automation_ports = lambda: SimpleNamespace(agent=SimpleNamespace(
        prepare_worker_surface=lambda **kwargs: prepared.append(kwargs),
    ))

    item = _automation_items(host)[0]

    assert item["runtime"]["assignment_state"] == "unassigned"
    assert item["runtime"]["action_surface"] == ""
    assert item["runtime"]["graph_revision"] == ""
    assert item["runtime"]["mutation_enabled"] is False
    assert item["runtime"]["kernel"]["state"] == "absent"
    assert item["runtime"]["warning"] == ""
    assert prepared == []
