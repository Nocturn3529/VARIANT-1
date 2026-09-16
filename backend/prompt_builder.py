"""System-prompt assembly for VARIANT-1.

Ordinary user turns receive one cache-stable instruction prefix plus fresh
current-turn context. The one provider tool contract is appended by the
runtime; there is no route-specific chat/conversation/task micro-contract or
persona layer.

Specialized worker templates (for example ``subagent.txt``) remain separately
loaded. Dependency-light: no imports from server.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
import os

# ---- specialized worker fallback (used if its template is missing) --------
SUBAGENT_CONTRACT = (
    "You are a focused SUBAGENT. The main agent handed you ONE self-contained sub-goal "
    "(below). You work on a CLEAN context — only what's provided here — and the main "
    "agent sees ONLY your final summary. Complete the sub-goal with the available "
    "tools, then stop.\n\n"
    "How to work:\n"
    "- Complete a direct-answer sub-goal by returning the requested answer. When "
    "the sub-goal requires tool work, use `ipython` and the preloaded capability "
    "globals through their declared interfaces. Do not describe or imitate calls "
    "in prose, and do not ask "
    "questions — you can't talk to the user.\n"
    "- After tools run you get their results; keep going until the sub-goal is done.\n"
    "- When finished (or genuinely blocked), reply in plain text with a final summary "
    "that STARTS with exactly one tag — DONE: (fully achieved) / PARTIAL: (some progress) "
    "/ FAILED: (couldn't do it) — then 1-2 sentences on what you accomplished and any "
    "concrete result the main agent needs (values, file paths, IDs, text). Be concise.\n\n"
    "{context}"
)

# ---- context passed in by the caller ---------------------------------------
@dataclass
class PromptContext:
    profile_block: str = ""        # approved profile projection or ""
    memory_block: str = ""         # retrieved long-term memory lines or ""
    project_instructions: str = "" # explicitly selected local project instructions
    attachment_context: str = ""   # user-selected image attachment note
    datetime: str = ""             # human-readable now
    cwd: str = ""                  # active working directory selected by the user
    project_roots: tuple[str, ...] = field(default_factory=tuple)
    # Metadata-only sizes for the session context meter. They never render into
    # provider input and therefore cannot change model behavior.
    profile_chars: int = 0
    retrieved_memory_chars: int = 0


@dataclass(frozen=True, slots=True)
class PromptProjection:
    """One stable instruction tier and one fresh current-turn tier."""

    stable: str = ""
    current: str = ""

    def combined(self) -> str:
        """Render the historical combined-string contract for non-chat callers."""

        return assemble(self.stable, self.current)


# ---- assembly --------------------------------------------------------------
def assemble(*parts) -> str:
    """Join non-empty sections with a blank line. The single place concatenation
    happens — empty sections simply vanish."""
    return "\n\n".join(p.strip() for p in parts if p and str(p).strip())


def _load(name: str, template_dirs: tuple[str, ...]) -> str:
    if not template_dirs:
        raise ValueError("prompt template directories are required")
    for d in template_dirs:
        try:
            with open(os.path.join(d, name), "r", encoding="utf-8") as f:
                txt = f.read().strip()
            if txt:
                return txt
        except Exception:
            continue
    return ""


def _core_section(profile_block: str) -> str:
    pb = (profile_block or "").strip()
    if not pb or pb.startswith("(nothing known"):
        return ""
    return "## About the user (core memory)\n" + pb


def _context_section(ctx: PromptContext) -> str:
    parts = []
    mb = (ctx.memory_block or "").strip()
    if mb and "Nothing stored" not in mb:
        parts.append("## Relevant memory\n" + mb)
    project = (ctx.project_instructions or "").strip()
    if project:
        parts.append("## Project instructions\n" + project)
    attachments = (ctx.attachment_context or "").strip()
    if attachments:
        parts.append("## Attached images\n" + attachments)
    meta = []
    if ctx.datetime:
        meta.append("Now: " + ctx.datetime)
    if meta:
        parts.append(" · ".join(meta))
    environment = _environment_section(ctx)
    if environment:
        parts.append(environment)
    return "\n\n".join(parts)


def _environment_section(ctx: PromptContext) -> str:
    """Render the run-scoped project directory as factual environment context."""
    cwd = str(ctx.cwd or "").strip()
    roots: list[str] = []
    for value in ctx.project_roots or ():
        root = str(value or "").strip()
        if root and root not in roots:
            roots.append(root)
    if cwd and cwd not in roots:
        roots.insert(0, cwd)
    if not cwd and roots:
        cwd = roots[0]
    if not cwd:
        return ""
    lines = ["<environment_context>", f"  <cwd>{escape(cwd)}</cwd>"]
    if roots:
        lines.append("  <project_roots>")
        lines.extend(f"    <root>{escape(root)}</root>" for root in roots)
        lines.append("  </project_roots>")
    lines.append("</environment_context>")
    return "\n".join(lines)


_CHAT_INSTRUCTIONS = (
    "Keep the current task and its required method in view. Treat an earlier task's files as prior work; "
    "reuse them when the user asks to continue or reuse them. If a named app or interaction is required, "
    "carry out that step and verify the requested result. Report what you observed and any unfinished "
    "requirement; a command succeeding alone does not prove the task is complete. "
    "VARIANT-1 may append a final `<variant1_current_context>` block to the latest user message. "
    "That block is fresh host context, not user-authored task text; apply any project instructions and "
    "environment facts in it to the current work."
)


def chat_prompt_projection(ctx: PromptContext) -> PromptProjection:
    """Split invariant chat instructions from context that can change each turn."""

    return PromptProjection(
        stable=_CHAT_INSTRUCTIONS,
        current=assemble(
            _core_section(ctx.profile_block),
            _context_section(ctx),
        ),
    )


def build_chat_system(ctx: PromptContext) -> str:
    """Return only the cache-stable ordinary-chat instruction prefix."""

    return chat_prompt_projection(ctx).stable


def build_chat_current_context(ctx: PromptContext) -> str:
    """Return fresh profile, project, attachment, time, and environment context."""

    return chat_prompt_projection(ctx).current


def build_task_system(ctx: PromptContext) -> str:
    """Worker/automation projection preserving the combined historical contract."""

    return chat_prompt_projection(ctx).combined()


def _subagent_contract(template_dirs: tuple[str, ...]) -> str:
    return _load("subagent.txt", template_dirs) or SUBAGENT_CONTRACT


def build_subagent_system(
    context_block: str,
    tools_block: str,
    *,
    template_dirs: tuple[str, ...],
) -> str:
    """Subagent mode: a LEAN, self-contained worker prompt — no core profile or
    main-task state. Just the subagent contract + the caller's curated
    context + tools block. `context_block` carries the sub-goal's background."""
    contract = _subagent_contract(template_dirs)
    if "{context}" in contract:
        return assemble(contract.replace("{context}", context_block or ""), tools_block)
    return assemble(contract, context_block, tools_block)
