"""Prompt builder: factual context projection without a persona layer."""

from __future__ import annotations

from prompt_builder import (
    PromptContext,
    build_chat_current_context,
    build_chat_system,
    build_subagent_system,
    build_task_system,
    chat_prompt_projection,
)


def test_build_chat_system_ignores_removed_turn_contract_files(tmp_path):
    for name in ("chat.txt", "conversation.txt", "task.txt"):
        (tmp_path / name).write_text("SHOULD NOT LOAD", encoding="utf-8")
    context = PromptContext(profile_block="Prefers concise answers.")
    out = build_chat_system(context)
    current = build_chat_current_context(context)

    assert "SHOULD NOT LOAD" not in out
    assert "Prefers concise answers." not in out
    assert "Prefers concise answers." in current


def test_build_task_system_uses_factual_context_only(tmp_path):
    out = build_task_system(PromptContext(
        memory_block="- prior fact",
    ))

    assert "prior fact" in out


def test_user_image_context_is_labeled_as_attached_images(tmp_path):
    out = build_chat_current_context(PromptContext(
        attachment_context="The user attached two images.",
    ))

    assert "## Attached images\nThe user attached two images." in out
    assert "## Screen" not in out


def test_empty_attachment_context_adds_no_screen_access_claim(tmp_path):
    out = build_chat_system(PromptContext())

    assert "Attached images" not in out
    assert "screen access" not in out.lower()


def test_project_instructions_have_a_distinct_system_section(tmp_path):
    out = build_chat_current_context(PromptContext(
        project_instructions="### AGENTS.md\nRun the focused tests first.",
    ))

    assert "## Project instructions" in out
    assert "### AGENTS.md\nRun the focused tests first." in out


def test_chat_instructions_are_stable_while_current_context_stays_fresh():
    first = chat_prompt_projection(PromptContext(
        profile_block="Prefers concise answers.",
        project_instructions="Run test A.",
        datetime="Monday at 10:00",
        cwd=r"C:\first",
        project_roots=(r"C:\first",),
    ))
    second = chat_prompt_projection(PromptContext(
        profile_block="Now prefers detailed answers.",
        project_instructions="Run test B.",
        datetime="Tuesday at 11:00",
        cwd=r"D:\second",
        project_roots=(r"D:\second",),
    ))

    assert first.stable == second.stable
    assert first.current != second.current
    assert "Monday at 10:00" in first.current
    assert r"D:\second" in second.current
    assert "Run test B." in second.current
    assert "Now prefers detailed answers." in second.current
    assert "Monday at 10:00" not in first.stable
    assert r"C:\first" not in first.stable


def test_prompt_context_has_no_character_projection_fields():
    context = PromptContext()

    assert not hasattr(context, "character_kernel")
    assert not hasattr(context, "character_scenes")
    assert not hasattr(context, "include_character")


def test_task_prompt_does_not_project_avatar_state(tmp_path):
    out = build_task_system(PromptContext(datetime="NOW"))

    assert "Current mood" not in out
    assert "concerned" not in out
    assert "NOW" in out


def test_current_project_is_system_environment_not_user_prose(tmp_path):
    out = build_task_system(PromptContext(
        cwd=r"C:\Users\ExampleUser\SCRATCH",
        project_roots=(r"C:\Users\ExampleUser\SCRATCH",),
    ))

    assert "<environment_context>" in out
    assert "<cwd>C:\\Users\\ExampleUser\\SCRATCH</cwd>" in out
    assert "<root>C:\\Users\\ExampleUser\\SCRATCH</root>" in out
    assert "Selected project" not in out
    assert "Name: SCRATCH" not in out


def test_subagent_contract_describes_whichever_tool_surface_is_assigned(tmp_path):
    out = build_subagent_system(
        "Complete the bounded task.",
        "session tools block",
        template_dirs=(str(tmp_path),),
    )

    assert "`ipython` and the preloaded capability globals" in out
    assert "Complete a direct-answer sub-goal by returning the requested answer" in out
    assert "Call native tools" not in out
