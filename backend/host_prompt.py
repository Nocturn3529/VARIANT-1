"""Prompt-building helpers extracted from the server composition root.

Receives the process ``AppHost`` explicitly.
"""

from __future__ import annotations

from datetime import datetime
import prompt_builder
from prompt_builder import PromptContext
from project_context import current_project_context, host_project_context


def format_local_datetime(value: datetime | None = None) -> str:
    """Render an aware local timestamp for default prompt context."""
    if value is None:
        current = datetime.now().astimezone()
    elif value.tzinfo is None:
        current = value.astimezone()
    else:
        current = value
    offset = current.strftime("%z")
    if len(offset) == 5:
        offset = offset[:3] + ":" + offset[3:]
    zone = current.tzname() or "local"
    return current.strftime("%A, %B %d, %Y, %I:%M %p ") + f"{zone} (UTC{offset})"


def prompt_context(h, memories: list, attachment_context: str = "") -> PromptContext:
    """Gather factual memory, attachment, time, and project prompt inputs.

    Product personality is deliberately not injected.
    """
    memory_store = h.require_runtime().memory.store
    profile_block = memory_store.render_profile()
    builtin_memory_block = "\n".join("- " + m for m in memories) if memories else ""
    memory_block = builtin_memory_block[:2000]
    fallback = host_project_context(h)
    project = current_project_context(default_cwd=fallback.cwd)
    return PromptContext(
        profile_block=profile_block,
        memory_block=memory_block,
        attachment_context=attachment_context,
        datetime=format_local_datetime(),
        cwd=project.cwd,
        project_roots=project.roots,
        profile_chars=len(profile_block),
        retrieved_memory_chars=len(memory_block),
    )


def build_system_prompt(h, memories: list, attachment_context: str = "") -> str:
    """Full task prompt for user-visible scheduled/headless automation output."""
    ctx = prompt_context(h, memories, attachment_context=attachment_context)
    return prompt_builder.build_task_system(ctx)


def build_internal_system_prompt(h, memories: list,
                                 attachment_context: str = "") -> str:
    """Mechanical worker prompt: contracts and factual context."""
    ctx = prompt_context(h, memories, attachment_context=attachment_context)
    return prompt_builder.build_task_system(ctx)


def tools_prompt_block(h, enabled_now: set, tspec: list) -> str:
    """Render the provider-action context used by headless ASTB workers."""
    import tools_prompt as tp

    del h
    return tp.tools_prompt_block(enabled_now, tspec)


def tool_lines(h, specs: list) -> str:
    """Render descriptions for provider schemas disclosed after task start."""
    import tools_prompt as tp

    return tp.tool_lines(specs)


def state_block(h, task) -> str:
    """The volatile task status. Kept
    OUT of the system prefix and folded into the latest user message so the big static
    prefix stays byte-identical across steps and the KV cache is reused."""
    if task is None:
        return ""
    return prompt_builder.assemble(task.render_state())
