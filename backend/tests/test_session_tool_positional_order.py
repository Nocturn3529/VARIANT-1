"""Session tools bind positional calls in their declared parameter order.

Bridge frames and stored contracts are canonical JSON with sorted keys, so the
declared order has to survive registration explicitly.
"""

from __future__ import annotations

import sqlite3

import pytest

from session_catalog.mutation import MutationManager
from session_catalog.mutation_schema import ordered_params, wrapped_helper_order
from test_mutation import _enable_mutation, mutable_stack  # noqa: F401


def test_order_comes_from_the_promoted_helper_only():
    promoted = (
        "def write_note(text, path='notes.txt', *, mode='w'):\n"
        "    return text\n"
        "\n"
        "def run(arguments):\n"
        "    return write_note(**arguments)\n"
    )
    assert wrapped_helper_order(promoted) == ["text", "path", "mode"]
    explicit = "def run(arguments):\n    return arguments['path']\n"
    assert wrapped_helper_order(explicit) == []
    params = {"path": {"type": "string"}, "text": {"type": "string"}}
    assert list(ordered_params(params, ["text", "path"])) == ["text", "path"]
    # Drafts stored before the order was recorded keep their sorted contract.
    assert list(ordered_params(params, [])) == ["path", "text"]


def test_existing_draft_tables_gain_the_order_column(tmp_path):
    database = tmp_path / "astb.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE mutation_draft (draft_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, "
            "catalog_release_id TEXT NOT NULL, category_id TEXT NOT NULL, "
            "position INTEGER NOT NULL, slot_id TEXT NOT NULL, declared_kind TEXT NOT NULL, "
            "parent_slot_id TEXT NOT NULL, alias TEXT NOT NULL, purpose TEXT NOT NULL, "
            "schema_json TEXT NOT NULL, params_json TEXT NOT NULL, source_ref TEXT NOT NULL, "
            "source_sha256 TEXT NOT NULL, proposal_fingerprint TEXT NOT NULL DEFAULT '', "
            "dependencies_json TEXT NOT NULL, tests_json TEXT NOT NULL, status TEXT NOT NULL, "
            "validation_json TEXT NOT NULL DEFAULT '', host_test_json TEXT NOT NULL DEFAULT '', "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
    MutationManager(
        str(database), artifact_store=None, catalog_repository=None,
        runtime_registry=None, broker=None, registry=None,
        enabled_resolver=lambda: set(), worker_root=str(tmp_path / "workers"),
    )
    with sqlite3.connect(database) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(mutation_draft)")}
    assert "parameter_order_json" in columns


async def _cell(manager, chat_id, code, call_id):
    result = await manager.execute(
        chat_id=chat_id, code=code, run_id="order-run", outer_tool_call_id=call_id,
    )
    assert result.ok, result.to_dict()
    return result.output.text().strip()


@pytest.mark.asyncio
async def test_synthesized_helper_keeps_its_positional_order(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "synthesized-order"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    try:
        await _cell(manager, chat_id, (
            "def write_note(text, path):\n"
            "    return f'{path}:{text}'\n"
            "toolbelt.synthesize(write_note)\n"
        ), "create")
        shown = await _cell(manager, chat_id, "print(tools.write_note('hello', 'notes.txt'))\n", "call")
        assert shown == "notes.txt:hello"
        signature = await _cell(manager, chat_id, "print(tools.write_note.__signature__)\n", "sig")
        assert signature.startswith("(text")
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_mutated_seed_keeps_the_seed_positional_order(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "mutated-order"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    try:
        await _cell(manager, chat_id, (
            "def run_command(value=None, cwd=None):\n"
            "    return {'value': value, 'cwd': cwd}\n"
            "toolbelt.mutate(tools.run_command, using=run_command)\n"
        ), "mutate")
        shown = await _cell(manager, chat_id, (
            "result = tools.run_command('dir', 'C:/work')\n"
            "print(result['value'], result['cwd'])\n"
        ), "call")
        assert shown == "dir C:/work"
    finally:
        await manager.shutdown()
