"""Synthesized tools disclose the effect class the broker enforces for them."""

from __future__ import annotations

import pytest

from test_mutation import _enable_mutation, mutable_stack  # noqa: F401


@pytest.mark.asyncio
async def test_plain_python_synthesized_tool_is_not_disclosed_as_pure(mutable_stack, tmp_path):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "effect-disclosure"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    target = (tmp_path / "written.txt").as_posix()
    try:
        created = await manager.execute(
            chat_id=chat_id,
            code=(
                "def write_note(text):\n"
                "    from pathlib import Path\n"
                f"    Path({target!r}).write_text(text, encoding='utf-8')\n"
                "    return 'ok'\n"
                "toolbelt.synthesize(write_note)\n"
            ),
            run_id="effect-run", outer_tool_call_id="effect-create",
        )
        assert created.ok, created.to_dict()
        shown = await manager.execute(
            chat_id=chat_id,
            code="doc = tools.write_note.documentation()\nprint(doc['effect_class'])\n",
            run_id="effect-run", outer_tool_call_id="effect-show",
        )
        assert shown.ok, shown.to_dict()
        assert shown.output.text().strip() == "external_side_effect"
    finally:
        await manager.shutdown()
