"""Native schemas are canonical; prompt prose stays small."""

from __future__ import annotations

import server


def _spec(name):
    return {"name": name, "description": "d", "params": {}}


def test_tools_block_is_empty_when_native_schema_has_all_context():
    out = server.APP.tools_prompt_block(
        {"web_search"}, [_spec("web_search")])

    assert out == ""
    assert "- web_search(" not in out


def test_tools_block_retains_selected_skill_catalogs():
    from tools_prompt import tools_prompt_block

    out = tools_prompt_block(
        {"ipython"}, [_spec("ipython")],
        skills_catalog="SKILLS: tidy-folder",
        apps_catalog="APPS: focus-coach",
    )
    assert out == "SKILLS: tidy-folder\n\nAPPS: focus-coach"


def test_tool_lines_describe_only_new_native_schemas():
    out = server.APP.tool_lines([_spec("computer")])

    assert out == "- computer(): d"
