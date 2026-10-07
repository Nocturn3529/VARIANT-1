"""The kernel's ergonomic authoring API and the host's explicit API share rules.

A name the kernel cannot mount is refused at registration: once active, it
would stop that chat's kernel from booting. Values the kernel derives for the
model (a purpose from a docstring) fit the host's bounds instead of failing.
"""

from __future__ import annotations

import pytest

from kernel_runtime.candidate_contract import MAX_EXAMPLE_CASES, MAX_PURPOSE_CHARS
from session_catalog.mutation import MutationError
from test_mutation import _enable_mutation, mutable_stack  # noqa: F401

SOURCE = "def run(arguments):\n    return arguments['value']\n"


def _schema(param: str = "value") -> dict:
    return {
        "type": "object", "required": [param],
        "properties": {param: {"type": "string"}},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("alias", "param", "code"), [
    ("lambda", "value", "invalid_alias"),        # Python keyword
    ("methods", "value", "invalid_alias"),       # tools' own discovery API
    ("echo", "from", "invalid_schema"),          # keyword parameter
    ("echo", "_deadline_ms", "invalid_schema"),  # added by proxy.async_
])
async def test_unmountable_names_are_refused_and_the_kernel_still_boots(
    mutable_stack, alias, param, code,
):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = f"names-{alias}-{param.strip('_')}"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    try:
        with pytest.raises(MutationError) as refused:
            await service.mutation.synthesize(
                chat_id, slot="build/7", alias=alias, purpose="Unmountable name.",
                schema=_schema(param),
                source=f"def run(arguments):\n    return arguments[{param!r}]\n",
            )
        assert refused.value.code == code
        result = await manager.execute(
            chat_id=chat_id, code="print('kernel ok')",
            run_id=f"run-{chat_id}", outer_tool_call_id=f"outer-{chat_id}",
        )
        assert result.ok, result.to_dict()
        assert result.output.text().strip() == "kernel ok"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_docstring_purpose_longer_than_the_host_bound_is_clipped(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "long-docstring"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    try:
        result = await manager.execute(
            chat_id=chat_id,
            code=(
                "def shout(text):\n"
                f"    '''{'Shout it. ' * 300}'''\n"
                "    return text.upper()\n"
                "registered = toolbelt.synthesize(shout)\n"
                "print(registered['slot_version'], tools.shout('hi'))\n"
            ),
            run_id="long-docstring-run", outer_tool_call_id="long-docstring-outer",
        )
        assert result.ok, result.to_dict()
        assert result.output.text().strip() == "1 HI"
        with service.mutation._lock, service.mutation._connect() as conn:
            purpose = conn.execute(
                "SELECT purpose FROM mutation_draft WHERE chat_id=? AND alias='shout'",
                (chat_id,),
            ).fetchone()[0]
        assert purpose.startswith("Shout it.")
        assert len(purpose) <= MAX_PURPOSE_CHARS
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_explicit_mutate_on_an_object_points_to_the_kernel_form(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "object-explicit-mutate"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "explore")
    try:
        with pytest.raises(MutationError) as refused:
            await service.mutation.mutate(
                chat_id, slot="computer", source="def run(arguments):\n    return 1\n",
            )
        assert refused.value.code == "atomic_contract_unavailable"
        message = str(refused.value)
        assert "toolbelt.mutate(" in message and "using=" in message
        assert "staged" not in message
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_examples_bound_is_described_as_a_payload_bound(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "examples-bound"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    try:
        with pytest.raises(MutationError) as refused:
            service.mutation.propose(
                chat_id, kind="create", slot="build/7", alias="echo",
                purpose="Echo.", schema=_schema(), source=SOURCE,
                tests=[{"arguments": {"value": "x"}}] * (MAX_EXAMPLE_CASES + 1),
            )
        assert refused.value.code == "test_quota"
        assert "quota" not in str(refused.value)
        assert str(MAX_EXAMPLE_CASES) in str(refused.value)
    finally:
        await manager.shutdown()
