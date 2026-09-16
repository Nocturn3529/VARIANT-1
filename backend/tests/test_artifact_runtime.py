from __future__ import annotations

import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from artifacts import (
    ArtifactRuntimeNotFound,
    ArtifactRuntimeValidationError,
    ContentAddressedArtifactStore,
    create_artifact_runtime,
)
from artifacts.capabilities import register_artifact_tools
from capability_broker import CapabilityBroker, InvocationContext
from tools import ToolRegistry
from tests.support.astb_runtime import StaticRuntimeRegistry
from work_fabric.scope import WorkScope
from work_fabric.service import WorkService
from work_fabric.models import WorkConflict
from work_fabric.capabilities import register_work_fabric_tools


def _runtime(tmp_path):
    work = WorkService.open(str(tmp_path / "work.sqlite3"))
    artifacts = ContentAddressedArtifactStore(str(tmp_path / "cas"))
    return work, artifacts, create_artifact_runtime(work, artifacts)


def _spec(title: str = "Capability report") -> dict:
    return {
        "schema": "variant1.artifact-spec.v1",
        "title": title,
        "blocks": [
            {"type": "heading", "level": 2, "text": "Results"},
            {"type": "table", "rows": [["Area", "Score"], ["Goals", 5], ["Git", 5]]},
            {"type": "paragraph", "text": "Verified output."},
        ],
    }


def test_validation_is_bound_to_current_render_after_builder_upgrade(tmp_path, monkeypatch):
    import artifacts.runtime_service as service
    _work, cas, runtime = _runtime(tmp_path)
    created = runtime.create(title="render version", kind="report", specification=_spec(), scope={"chat_id": "a"})
    identity = created.artifact.artifact_id
    first = runtime.validate(identity, "pdf")
    original = service.build_artifact
    monkeypatch.setattr(service, "BUILDER_VERSION", "test-next")
    monkeypatch.setattr(service, "build_artifact", lambda spec, fmt: replace(
        original({**spec, "title": "new render"}, fmt), renderer_version="test-next"))
    second = runtime.validate(identity, "pdf")
    assert first.render_id != second.render_id
    assert first.validation_id != second.validation_id
    assert runtime.validate(identity, "pdf").validation_id == second.validation_id
    import json
    assert json.loads(cas.read_bytes(second.report_ref))["render_id"] == second.render_id


@pytest.mark.asyncio
async def test_cancel_during_last_render_leaves_public_alias_and_no_success(tmp_path, monkeypatch):
    import asyncio
    import threading
    work, cas, runtime = _runtime(tmp_path)
    scope = {"chat_id": "cancel-publish"}
    old = runtime.create(title="old", kind="report", specification=_spec(), scope=scope, alias="latest")
    new = runtime.create(title="new", kind="report", specification=_spec("new"), scope=scope)
    entered, release = threading.Event(), threading.Event()
    original = runtime.render
    def render(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        release.wait(5)
        return result
    monkeypatch.setattr(runtime, "render", render)
    job = runtime.publish(new.artifact.artifact_id, "pdf", alias="latest", expected_alias_version=1, validate=False)
    await work.start()
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        await asyncio.to_thread(work.jobs.cancel, job.job_id)
        release.set()
        terminal = await work.jobs.wait(job.job_id, timeout_s=5)
        assert terminal.status == "cancelled"
        assert not terminal.result_ref
        assert runtime.resolve_alias("latest", scope=scope).artifact.artifact_id == old.artifact.artifact_id
    finally:
        release.set()
        await work.shutdown()


@pytest.mark.asyncio
async def test_publication_manifest_failure_rolls_back_alias(tmp_path, monkeypatch):
    work, cas, runtime = _runtime(tmp_path)
    scope = {"chat_id": "rollback-publish"}
    old = runtime.create(title="old", kind="report", specification=_spec(), scope=scope, alias="latest")
    new = runtime.create(title="new", kind="report", specification=_spec("new"), scope=scope)
    original = cas.put_json
    def put(value, **kwargs):
        if kwargs.get("kind") == "artifact_publication_manifest":
            raise OSError("injected publication failure")
        return original(value, **kwargs)
    monkeypatch.setattr(cas, "put_json", put)
    job = runtime.publish(new.artifact.artifact_id, "pdf", alias="latest", expected_alias_version=1, validate=False)
    await work.start()
    try:
        terminal = await work.jobs.wait(job.job_id, timeout_s=5)
        assert terminal.status == "failed"
        assert runtime.resolve_alias("latest", scope=scope).artifact.artifact_id == old.artifact.artifact_id
    finally:
        await work.shutdown()


def test_immutable_revision_render_validation_and_alias(tmp_path):
    _work, cas, runtime = _runtime(tmp_path)
    created = runtime.create(
        title="Report", kind="report", specification=_spec(),
        scope={"chat_id": "chat-a", "workspace_id": "workspace-a"},
        alias="latest",
    )
    artifact_id = created.artifact.artifact_id
    first_version = runtime.snapshot(artifact_id).artifact.version
    revised = runtime.revise(
        artifact_id, _spec("Revised"), expected_version=first_version,
    )
    assert revised.revision.revision == 2
    assert [row["revision"] for row in runtime.history(artifact_id)] == [2, 1]

    for format_name in ("docx", "pptx", "xlsx", "pdf", "latex"):
        render = runtime.render(artifact_id, format_name)
        assert cas.stat(render.output_ref, verify=True).bytes > 0
        validation = runtime.validate(artifact_id, format_name)
        assert validation.status in {"passed", "warning"}
        assert runtime.render(artifact_id, format_name).render_id == render.render_id
        assert runtime.validate(artifact_id, format_name).validation_id == validation.validation_id

    resolved = runtime.resolve_alias(
        "latest", scope={"chat_id": "chat-a", "workspace_id": "workspace-a"},
    )
    # Aliases are intentionally pinned, not implicit moving heads.
    assert resolved.revision.revision == 1


@pytest.mark.asyncio
async def test_publish_job_is_durable_and_exports_verified_output(tmp_path):
    work, cas, runtime = _runtime(tmp_path)
    created = runtime.create(
        title="Report", kind="report", specification=_spec(),
        scope={"chat_id": "chat-publish"},
    )
    job = runtime.publish(
        created.artifact.artifact_id,
        ("docx", "pdf"),
        idempotency_key="publish-report-1",
    )
    await work.start()
    try:
        completed = await work.jobs.wait(job.job_id, timeout_s=20)
    finally:
        await work.shutdown()
    assert completed.status == "succeeded"
    manifest = cas.read_bytes_scoped(completed.result_ref, "chat-publish")
    assert b'"valid":true' in manifest
    snapshot = runtime.snapshot(created.artifact.artifact_id)
    assert {item.format for item in snapshot.renders} == {"docx", "pdf"}
    assert len(snapshot.validations) == 2

    destination = tmp_path / "published.pdf"
    receipt = runtime.export(
        created.artifact.artifact_id, str(destination), format="pdf",
    )
    assert receipt["verified"] is True
    assert destination.is_file()
    assert os.path.getsize(destination) == receipt["bytes"]


@pytest.mark.asyncio
async def test_require_valid_rejection_cannot_move_public_alias(tmp_path):
    work, _cas, runtime = _runtime(tmp_path)
    scope = {"chat_id": "chat-publication"}
    published = runtime.create(
        title="Published", kind="report", specification=_spec(),
        scope=scope, alias="public",
    )
    invalid = runtime.create(
        title="Invalid", kind="analysis_report",
        specification={
            "title": "Invalid",
            "blocks": [{
                "type": "paragraph",
                "text": "Unsupported claim {{cite:missing-claim}}",
            }],
        },
        scope=scope,
    )
    job = runtime.publish(
        invalid.artifact.artifact_id,
        ("pdf",),
        require_valid=True,
        alias="public",
        expected_alias_version=1,
        idempotency_key="invalid-publication",
    )

    await work.start()
    try:
        failed = await work.jobs.wait(job.job_id, timeout_s=20)
    finally:
        await work.shutdown()

    assert failed.status == "failed"
    resolved = runtime.resolve_alias("public", scope=scope)
    assert resolved.artifact.artifact_id == published.artifact.artifact_id
    assert resolved.revision.revision == 1


def test_unresolved_citation_placeholder_fails_validation(tmp_path):
    _work, _cas, runtime = _runtime(tmp_path)
    created = runtime.create(
        title="Analysis", kind="analysis_report",
        specification={
            "title": "Analysis",
            "blocks": [{"type": "paragraph", "text": "Claim {{cite:claim-1}}"}],
        },
        scope={"chat_id": "chat-analysis"},
    )
    validation = runtime.validate(created.artifact.artifact_id, "pdf")
    assert validation.status == "failed"
    assert any(
        item.get("code") == "unresolved_citation_placeholder"
        for item in validation.findings
    )


def test_tombstoned_artifact_rejects_alias_and_all_output_mutations(tmp_path):
    _work, _cas, runtime = _runtime(tmp_path)
    scope = {"chat_id": "chat-tombstone"}
    created = runtime.create(
        title="Deleted", kind="document", specification=_spec(),
        scope=scope, alias="deleted-alias",
    )
    artifact_id = created.artifact.artifact_id
    runtime.render(artifact_id, "pdf")
    before = runtime.snapshot(artifact_id)
    tombstoned = runtime.tombstone(
        artifact_id, expected_version=before.artifact.version,
    )
    terminal_version = tombstoned["version"]

    with pytest.raises(ArtifactRuntimeNotFound):
        runtime.resolve_alias("deleted-alias", scope=scope)
    operations = (
        lambda: runtime.revise(
            artifact_id, _spec("late"), expected_version=terminal_version,
        ),
        lambda: runtime.render(artifact_id, "pdf"),
        lambda: runtime.validate(artifact_id, "pdf"),
        lambda: runtime.set_alias(
            artifact_id, "late-alias", expected_version=terminal_version,
        ),
        lambda: runtime.link(
            artifact_id, owner_kind="goal", owner_id="goal-late", role="output",
            expected_version=terminal_version,
        ),
        lambda: runtime.export(
            artifact_id, str(tmp_path / "late.pdf"), format="pdf",
        ),
    )
    for operation in operations:
        with pytest.raises(ArtifactRuntimeValidationError, match="tombstoned"):
            operation()

    after = runtime.snapshot(artifact_id)
    assert after.artifact.version == terminal_version
    assert len(after.renders) == len(before.renders)
    assert after.links == before.links
    assert not (tmp_path / "late.pdf").exists()


def test_create_and_revise_retry_by_idempotency_key_return_same_identity(tmp_path):
    _work, _cas, runtime = _runtime(tmp_path)
    first = runtime.create(
        title="Idempotent", kind="document", specification=_spec(),
        scope={"chat_id": "chat-idempotent"}, idempotency_key="create-1",
    )
    repeated = runtime.create(
        title="Idempotent", kind="document", specification=_spec(),
        scope={"chat_id": "chat-idempotent"}, idempotency_key="create-1",
    )
    assert repeated.artifact.artifact_id == first.artifact.artifact_id
    assert repeated.revision.revision == 1

    revised = runtime.revise(
        first.artifact.artifact_id, _spec("Next"),
        expected_version=first.artifact.version,
        idempotency_key="revise-1",
    )
    retried = runtime.revise(
        first.artifact.artifact_id, _spec("Next"),
        expected_version=first.artifact.version,
        idempotency_key="revise-1",
    )
    assert retried.revision.revision == revised.revision.revision == 2


def test_artifact_scope_is_applied_before_order_and_limit(tmp_path):
    _work, _cas, runtime = _runtime(tmp_path)
    wanted = runtime.create(
        title="Wanted", kind="document", specification=_spec("Wanted"),
        scope={"chat_id": "chat-wanted", "workspace_id": "workspace-wanted"},
    )
    for index in range(3):
        runtime.create(
            title=f"Other {index}", kind="document",
            specification=_spec(f"Other {index}"),
            scope={"chat_id": "chat-other", "workspace_id": "workspace-other"},
        )

    visible = runtime.list(
        scope={"chat_id": "chat-wanted", "workspace_id": "workspace-wanted"},
        limit=1,
    )

    assert [item.artifact.artifact_id for item in visible] == [
        wanted.artifact.artifact_id,
    ]


def test_reused_revision_event_key_rolls_back_artifact_domain_rows(tmp_path):
    _work, _cas, runtime = _runtime(tmp_path)
    created = runtime.create(
        title="Version fence", kind="document", specification=_spec(),
        scope={"chat_id": "chat-version-fence"},
    )
    first = runtime.revise(
        created.artifact.artifact_id,
        _spec("Revision two"),
        expected_version=created.artifact.version,
        idempotency_key="one-revision-event",
    )
    with pytest.raises(WorkConflict, match="idempotency key was reused"):
        runtime.revise(
            created.artifact.artifact_id,
            _spec("Revision three"),
            expected_version=first.artifact.version,
            idempotency_key="one-revision-event",
        )
    after = runtime.snapshot(created.artifact.artifact_id)
    assert after.artifact.version == first.artifact.version
    assert after.revision.revision == first.revision.revision == 2


def test_create_retry_reconciles_alias_after_interrupted_assignment(
    tmp_path, monkeypatch,
):
    _work, _cas, runtime = _runtime(tmp_path)
    original = runtime._reconcile_create_alias
    calls = 0

    def interrupt(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise OSError("alias persistence interrupted")

    monkeypatch.setattr(runtime, "_reconcile_create_alias", interrupt)
    with pytest.raises(OSError, match="interrupted"):
        runtime.create(
            title="Retry alias", kind="document", specification=_spec(),
            scope={"chat_id": "chat-alias-retry"},
            alias="public", idempotency_key="create-alias-retry",
        )
    assert len(runtime.list(scope={"chat_id": "chat-alias-retry"})) == 1

    monkeypatch.setattr(runtime, "_reconcile_create_alias", original)
    replay = runtime.create(
        title="Retry alias", kind="document", specification=_spec(),
        scope={"chat_id": "chat-alias-retry"},
        alias="public", idempotency_key="create-alias-retry",
    )
    resolved = runtime.resolve_alias(
        "public", scope={"chat_id": "chat-alias-retry"}
    )
    assert calls == 1
    assert resolved.artifact.artifact_id == replay.artifact.artifact_id


@pytest.mark.asyncio
async def test_one_artifacts_seed_dispatches_versioned_methods(tmp_path):
    work, cas, runtime = _runtime(tmp_path)
    registry = ToolRegistry()
    broker = CapabilityBroker(
        registry=registry,
        runtime_registry=StaticRuntimeRegistry(),
        enabled_resolver=lambda: {tool.name for tool in registry.all()},
        artifact_store=cas,
    )
    host = SimpleNamespace(
        registry=registry,
        session_artifacts=cas,
        artifact_runtime=runtime,
        capability_broker=broker,
        remote_handle_routers={},
        require_runtime=lambda: SimpleNamespace(
            artifacts=runtime,
            work=work,
            registry=registry,
            broker=broker,
            session_artifacts=cas,
        ),
    )
    register_work_fabric_tools(host)
    register_artifact_tools(host)
    context = InvocationContext(
        chat_id="chat-seed",
        run_id="run-seed",
        outer_tool_call_id="outer-seed",
        cell_execution_id="cell-seed",
        nested_call_id="nested-create",
        catalog_release_id="astb.test.release.v1",
        surface="ipython",
        work_scope=WorkScope(chat_id="chat-seed"),
    )

    created_receipt = await broker.invoke_name(
        "artifacts",
        {
            "operation": "create",
            "title": "Unified seed",
            "kind": "report",
            "specification": _spec("Unified seed"),
        },
        context,
    )
    assert created_receipt.ok, created_receipt.to_dict()
    created_handle = created_receipt.result_value
    identity = created_handle["$variant1_handle"]
    artifact_id = identity["id"]
    listed_receipt = await broker.invoke_name(
        "artifacts",
        {"operation": "list"},
        replace(context, nested_call_id="nested-list"),
    )
    assert listed_receipt.ok, listed_receipt.to_dict()
    assert listed_receipt.result_value[0]["$variant1_handle"]["id"] == artifact_id
    inspected = await broker.invoke_name(
        "remote_handle_dispatch",
        {
            "handle": {
                key: identity[key]
                for key in ("service", "kind", "id", "generation", "revision")
            },
            "method": "inspect",
            "arguments": {},
        },
        replace(context, nested_call_id="nested-inspect"),
    )
    assert inspected.ok, inspected.error
    assert inspected.result_value["artifact"]["artifact_id"] == artifact_id
    history = await broker.invoke_name(
        "remote_handle_dispatch",
        {
            "handle": {
                key: identity[key]
                for key in ("service", "kind", "id", "generation", "revision")
            },
            "method": "history",
            "arguments": {},
        },
        replace(context, nested_call_id="nested-history"),
    )
    assert history.ok, history.error
    assert history.result_value[0]["revision"] == 1
    revised = await broker.invoke_name(
        "remote_handle_dispatch",
        {
            "handle": {
                key: identity[key]
                for key in ("service", "kind", "id", "generation", "revision")
            },
            "method": "revise",
            "arguments": {"specification": _spec("Revised through handle")},
        },
        replace(context, nested_call_id="nested-revise"),
    )
    assert revised.ok, revised.error
    assert revised.result_value["$variant1_handle"]["revision"] > identity["revision"]
    stale = await broker.invoke_name(
        "remote_handle_dispatch",
        {
            "handle": {
                key: identity[key]
                for key in ("service", "kind", "id", "generation", "revision")
            },
            "method": "inspect",
            "arguments": {},
        },
        replace(context, nested_call_id="nested-stale"),
    )
    assert not stale.ok and "stale artifacts.artifact handle" in stale.error.message
    blob = cas.put_text("bounded evidence", kind="test", scope="chat-seed")
    read_receipt = await broker.invoke_name(
        "artifacts",
        {"operation": "read_text", "ref": blob.ref},
        replace(context, nested_call_id="nested-read"),
    )
    assert read_receipt.ok, read_receipt.to_dict()
    assert read_receipt.result_value["text"] == "bounded evidence"
    from artifacts.blob_handles import blob_handle_envelope
    bound = blob_handle_envelope(host, context, blob.ref)["$variant1_handle"]
    identity = {key: bound[key] for key in ("service", "kind", "id", "generation", "revision")}
    exported = await broker.invoke_name(
        "remote_handle_dispatch",
        {"handle": identity, "method": "save", "arguments": {"path": str(tmp_path / "bound.txt")}},
        replace(context, nested_call_id="bound-export", mount_revision=99),
    )
    assert exported.ok, exported.to_dict()
    assert exported.result_value["verified"]
    assert (tmp_path / "bound.txt").read_bytes() == b"bounded evidence"
    binary = await broker.invoke_name(
        "remote_handle_dispatch",
        {"handle": identity, "method": "read_bytes", "arguments": {}},
        replace(context, nested_call_id="bound-read", mount_revision=99),
    )
    assert binary.ok, binary.to_dict()
    assert binary.result_value == b"bounded evidence"
    assert {tool.name for tool in registry.all()} == {
        "artifacts", "remote_handle_dispatch",
    }
