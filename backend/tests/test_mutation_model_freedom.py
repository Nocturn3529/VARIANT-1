"""Session tools stay the model's to create, revise and keep (owner decision 2026-10-04).

No mandatory tests, no lifetime lockouts, no automatic rollback; mechanical
checks (contract, authority, CAS, process cleanup) remain.
"""

from __future__ import annotations

import pytest

from session_catalog.mutation import RETAINED_SLOT_VERSIONS, MutationError
from test_mutation import _enable_mutation, mutable_stack  # noqa: F401

SCHEMA = {
    "type": "object", "required": ["value"],
    "properties": {"value": {"type": "string"}},
}


@pytest.mark.asyncio
async def test_unactivated_drafts_never_block_new_proposals(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "many-drafts"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    try:
        for index in range(10):  # the old draft quota stopped at eight
            service.mutation.propose(
                chat_id, kind="create", slot="build/7", alias="echo",
                purpose=f"Draft {index}.", schema=SCHEMA,
                source="def run(arguments):\n    return arguments['value']\n",
            )
        activated = await service.mutation.synthesize(
            chat_id, slot="build/7", alias="echo", purpose="Echo.",
            schema=SCHEMA, source="def run(arguments):\n    return arguments['value']\n",
        )
        assert activated["slot_version"] == 1
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_revisions_retire_old_inactive_versions_instead_of_refusing(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "many-revisions"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    try:
        await service.mutation.synthesize(
            chat_id, slot="build/7", alias="echo", purpose="Echo v1.",
            schema=SCHEMA, source="def run(arguments):\n    return 'v1'\n",
        )
        for version in range(2, 12):  # the old version quota stopped at eight
            revised = await service.mutation.mutate(
                chat_id, slot="7",
                source=f"def run(arguments):\n    return 'v{version}'\n",
            )
            assert revised["slot_version"] == version
        with service.mutation._lock, service.mutation._connect() as conn:
            retained = [int(row[0]) for row in conn.execute(
                "SELECT version FROM astb_slot_version WHERE chat_id=? "
                "AND status<>'garbage_collected' ORDER BY version", (chat_id,),
            ).fetchall()]
            total = int(conn.execute(
                "SELECT COUNT(*) FROM astb_slot_version WHERE chat_id=?", (chat_id,),
            ).fetchone()[0])
        assert total == 11  # retired rows stay as provenance
        assert len(retained) <= RETAINED_SLOT_VERSIONS and 11 in retained and 10 in retained
        assert service.mutation.status(chat_id)["active"][0]["version"] == 11
        with pytest.raises(MutationError) as retired:
            service.mutation.rollback(chat_id, "7", to_version=1)
        assert retired.value.code == "unknown_version"
        assert service.mutation.rollback(chat_id, "7", to_version=10)["slot_version"] == 10
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_unmocked_example_reports_the_missing_mock_without_blocking(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "unmocked-example"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    try:
        activated = await service.mutation.synthesize(
            chat_id, slot="build/7", alias="read_upper", purpose="Read and upper-case.",
            schema={"type": "object", "required": ["path"],
                    "properties": {"path": {"type": "string"}}},
            source="def run(arguments):\n    return str(tools.read_file(path=arguments['path'])).upper()\n",
            tests=[{"arguments": {"path": "notes.txt"}}],
        )
        assert activated["slot_version"] == 1
        assert activated["examples"]["cases"] == 1 and activated["examples"]["passed"] == 0
        assert "unmocked_proxy" in activated["examples"]["failures"][0]
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_registration_checks_source_in_the_host_without_a_worker(mutable_stack, monkeypatch):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "host-check"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")

    async def no_worker(*_args, **_kwargs):
        raise AssertionError("registration without examples must not start a worker")

    monkeypatch.setattr(service.mutation.worker, "run", no_worker)
    try:
        valid = service.mutation.propose(
            chat_id, kind="create", slot="build/7", alias="echo", purpose="Echo.",
            schema=SCHEMA, source="def run(arguments):\n    return arguments['value']\n",
        )
        assert (await service.mutation.validate(chat_id, valid["draft_id"]))["ok"] is True
        invalid = service.mutation.propose(
            chat_id, kind="create", slot="build/8", alias="broken", purpose="Broken.",
            schema=SCHEMA, source="def helper(arguments):\n    return arguments\n",
        )
        report = await service.mutation.validate(chat_id, invalid["draft_id"])
        assert report["ok"] is False
        assert report["error"]["code"] == "candidate_contract_error"
        activated = await service.mutation.activate(chat_id, valid["draft_id"])
        assert activated["slot_version"] == 1
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_kernel_hides_the_staged_api_and_reports_kernel_assertions(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "kernel-surface"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    try:
        result = await manager.execute(
            chat_id=chat_id,
            code=(
                "staged = [name for name in ('propose', 'validate', 'test', 'activate', "
                "'propose_activate') if hasattr(toolbelt, name) or name in toolbelt.methods()]\n"
                "def shout(text):\n"
                "    return text.upper()\n"
                "registered = toolbelt.synthesize(shout, tests=[lambda: shout('a') == 'A'])\n"
                "print(staged, registered['kernel_assertions'], registered['examples']['cases'])\n"
                "print(tools.shout('hi'))\n"
            ),
            run_id="kernel-surface-run", outer_tool_call_id="kernel-surface-outer",
        )
        assert result.ok, result.to_dict()
        lines = result.output.text().strip().splitlines()
        assert lines[0] == "[] 1 0"
        assert lines[1] == "HI"
    finally:
        await manager.shutdown()
