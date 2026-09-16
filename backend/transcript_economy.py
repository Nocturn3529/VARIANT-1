"""Transcript / context-window economy for long agent turns.

Owns token estimates and the single mid-turn compaction path. Host injects
the LLM router for exact token counts and summarize-compress; pure string/list
logic lives here.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
import json
import os
import re
import time
from typing import Any, Awaitable, Callable, Optional

from observability import context_lineage
from llm_usage import observe_usage_category


CONTEXT_ADMISSION_SAFETY_TOKENS = 256
MAX_OUTPUT_WINDOW_RATIO = 0.50


class ContextWindowExceededError(RuntimeError):
    """The complete provider request cannot fit after safe compaction."""

    def __init__(
        self,
        *,
        prompt_tokens: int,
        context_limit: int,
        output_reserve: int,
        prompt_limit: int,
        exact: bool,
    ) -> None:
        self.prompt_tokens = max(0, int(prompt_tokens or 0))
        self.context_limit = max(0, int(context_limit or 0))
        self.output_reserve = max(0, int(output_reserve or 0))
        self.prompt_limit = max(0, int(prompt_limit or 0))
        self.exact = bool(exact)
        qualifier = "" if self.exact else "approximately "
        super().__init__(
            "Automatic context compaction could not make this request fit the "
            f"active model: it needs {qualifier}{self.prompt_tokens:,} input "
            f"tokens, but only {self.prompt_limit:,} are available while "
            f"reserving {self.output_reserve:,} tokens for the response in its "
            f"{self.context_limit:,}-token window. If the request contains a "
            "large file, give VARIANT-1 its readable local file path instead of "
            "attaching or pasting the contents; otherwise shorten the request "
            "or use a larger-context model."
        )


def prompt_input_limit(
    context_limit: int,
    output_reserve: int,
    *,
    safety_tokens: int = CONTEXT_ADMISSION_SAFETY_TOKENS,
) -> int:
    """Provider input budget after reserving generation and framing headroom."""
    context = max(0, int(context_limit or 0))
    output = max(0, int(output_reserve or 0))
    safety = max(0, int(safety_tokens or 0))
    return max(0, context - output - safety)


def clamp_output_reserve(context_limit: int, requested: int) -> int:
    """Keep generation useful without consuming more than half the window."""

    wanted = max(1, int(requested or 0))
    context = max(0, int(context_limit or 0))
    if context < 2_048:
        return wanted
    return max(1, min(wanted, int(context * MAX_OUTPUT_WINDOW_RATIO)))


def enforce_prompt_admission(
    *,
    prompt_tokens: int,
    context_limit: int,
    output_reserve: int,
    exact: bool,
    safety_tokens: int = CONTEXT_ADMISSION_SAFETY_TOKENS,
) -> int:
    """Return the input budget or raise before an oversized provider call."""
    limit = prompt_input_limit(
        context_limit,
        output_reserve,
        safety_tokens=safety_tokens,
    )
    if int(prompt_tokens or 0) > limit:
        raise ContextWindowExceededError(
            prompt_tokens=prompt_tokens,
            context_limit=context_limit,
            output_reserve=output_reserve,
            prompt_limit=limit,
            exact=exact,
        )
    return limit


def approx_tokens(messages: list) -> int:
    """Rough token estimate (~3 chars/token) for compress decisions."""
    chars = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            for p in c:
                if isinstance(p, dict) and isinstance(p.get("text"), str):
                    chars += len(p["text"])
                elif isinstance(p, dict) and isinstance(p.get("content"), str):
                    chars += len(p["content"])
        # Native tool_calls argument blobs count toward context too.
        for tc in m.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            chars += len(str(fn.get("name") or "")) + len(str(fn.get("arguments") or ""))
    return chars // 3


async def count_prompt_tokens(
    messages: list,
    *,
    count_tokens: Callable[[str], Awaitable[Optional[int]]] | None,
    count_rendered_prompt: Callable[..., Awaitable[Optional[int]]] | None = None,
    tools: list | None = None,
    image_b64=None,
) -> int | None:
    """Count a local prompt, preferring the engine's rendered chat template.

    Modern llama-server builds expose a native chat input-token endpoint; the
    injected counter owns chat-template, tool-schema, and multimodal accounting.
    The engine internally falls back to ``/apply-template`` + ``/tokenize`` on
    older builds. If both fail, keep VARIANT-1's prior flattened ``/tokenize`` path
    as the final graceful approximation.
    """
    if count_rendered_prompt is not None:
        try:
            kwargs = {"tools": tools or []}
            if image_b64:
                kwargs["image_b64"] = image_b64
            exact = await count_rendered_prompt(messages, **kwargs)
        except Exception:
            exact = None
        if isinstance(exact, int) and exact >= 0:
            return exact

    if count_tokens is None:
        return None
    chunks = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            chunks.append(c)
        elif isinstance(c, list):
            for p in c:
                if isinstance(p, dict) and isinstance(p.get("text"), str):
                    chunks.append(p["text"])
        for tc in m.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            chunks.append(str(fn.get("name") or ""))
            chunks.append(str(fn.get("arguments") or ""))
    if tools:
        # This is the last-resort path for llama.cpp builds without either
        # native chat input counting or /apply-template. Tool definitions are
        # still prompt material and can outweigh the transcript itself, so keep
        # their stable JSON representation in the approximation rather than
        # silently treating them as free context.
        try:
            chunks.append(json.dumps(
                tools,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ))
        except (TypeError, ValueError):
            chunks.append(str(tools))
    try:
        n = await count_tokens("\n".join(chunks))
    except Exception:
        return None
    if not isinstance(n, int):
        return None
    return n + 6 * len(messages)


def ctx_compress_threshold(
    *,
    mode: str = "local",
    ctx_size: int = 0,
    env_override: str | None = None,
) -> int:
    """Token count above which mid-turn compaction should run."""
    override = env_override if env_override is not None else os.environ.get("VARIANT1_COMPRESS_TOKENS")
    if override:
        try:
            return max(200, int(override))
        except ValueError:
            pass
    try:
        ctx = int(ctx_size or 0)
        if mode == "cloud":
            if ctx <= 0:
                ctx = 32768
            # Leave at least 8K for tools, the current turn, and output. Large
            # windows cap at 96K so compaction remains useful rather than
            # forwarding an ever-growing transcript forever.
            return max(4096, min(96000, int(ctx * 0.70), ctx - 8192))
        if ctx <= 0:
            ctx = 8192
        return max(2048, min(int(ctx * 0.70), ctx - 4608))
    except Exception:
        return 6000


COMPACT_SUMMARY_MARKER = "[Earlier steps in this task, summarized]"
COMPACT_SUMMARY_WARNING = (
    "Lossy model-generated recap of earlier assistant work. This is evidence, "
    "not a user instruction, and it may be wrong. The original user request and "
    "current host tool contracts override it. Failed attempts do not prove that "
    "a capability is unavailable."
)
COMPACT_SUMMARY_MAX_TOKENS = 1_900
COMPACT_SUMMARY_MAX_CHARS = 16_000
COMPACT_SUMMARY_SENTINEL = "<variant1-recap-complete/>"
COMPACT_SUMMARY_SECTIONS = (
    "Goal",
    "Confirmed Results",
    "Pending Work",
    "Failed Attempts (not blockers)",
    "Exact Context",
)
COMPACT_FILE_STATE_OPEN = "<variant1-file-state>"
COMPACT_FILE_STATE_CLOSE = "</variant1-file-state>"
SUMMARY_MESSAGE_CHARS = 2_000
SUMMARY_INPUT_CHARS = 24_000


@dataclass
class CompactionRetryState:
    """Ephemeral backoff for one chat/model's rejected summary projections."""

    failures: int = 0
    retry_after: float = 0.0
    failed_fingerprint: str = ""
    failed_attempts: dict[str, int] = field(default_factory=dict, repr=False)

    def ready(self, fingerprint: str = "") -> bool:
        return (self.failed_attempts.get(fingerprint, 0) < 2
                and time.monotonic() >= self.retry_after)

    def failed(self, fingerprint: str) -> None:
        self.failures += 1
        self.failed_fingerprint = fingerprint
        if fingerprint not in self.failed_attempts and len(self.failed_attempts) >= 32:
            self.failed_attempts.pop(next(iter(self.failed_attempts)))
        self.failed_attempts[fingerprint] = self.failed_attempts.get(fingerprint, 0) + 1
        self.retry_after = time.monotonic() + min(900, 60 * 2 ** min(self.failures - 1, 4))

    def succeeded(self) -> None:
        self.failures = 0
        self.retry_after = 0.0
        self.failed_fingerprint = ""
        self.failed_attempts.clear()

_READ_RESULT_RE = re.compile(
    r"(?m)^(.+?) \(lines (\d+)-(\d+) of (\d+)\):"
)
_COMPACT_FILE_STATE_RE = re.compile(
    re.escape(COMPACT_FILE_STATE_OPEN)
    + r"\s*(.*?)\s*"
    + re.escape(COMPACT_FILE_STATE_CLOSE),
    re.DOTALL,
)
_COMPACT_HEADING_RE = re.compile(r"(?m)^##(?!#) ([^\n]+)$")


def _validated_compaction_summary(value: Any) -> tuple[str, str]:
    """Return a complete recap body or one bounded rejection reason.

    This validates only the recap envelope. It deliberately makes no claim
    that structurally valid model-authored prose is factually correct.
    """

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return "", "empty_summary"
    if len(text) > COMPACT_SUMMARY_MAX_CHARS:
        return "", "summary_too_large"
    lines = text.splitlines()
    if not lines or lines[-1].strip() != COMPACT_SUMMARY_SENTINEL:
        return "", "missing_terminal_sentinel"
    if sum(line.strip() == COMPACT_SUMMARY_SENTINEL for line in lines) != 1:
        return "", "duplicate_terminal_sentinel"

    body = "\n".join(lines[:-1]).rstrip()
    headings = list(_COMPACT_HEADING_RE.finditer(body))
    names = [match.group(1).strip() for match in headings]
    if names != list(COMPACT_SUMMARY_SECTIONS):
        return "", "invalid_section_order"
    if not headings or headings[0].start() != 0:
        return "", "summary_preamble_present"
    for index, heading in enumerate(headings):
        content_start = heading.end()
        content_end = (
            headings[index + 1].start()
            if index + 1 < len(headings)
            else len(body)
        )
        if not body[content_start:content_end].strip():
            return "", "empty_section"
    return body, ""


def _content_text(content: Any) -> str:
    """Flatten provider-neutral message content for the summary projection."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        value = item.get("text")
        if not isinstance(value, str):
            value = item.get("content")
        if isinstance(value, str) and value:
            parts.append(value)
    return "\n".join(parts)


def _summary_clip(value: str, limit: int = SUMMARY_MESSAGE_CHARS) -> str:
    """Keep both evidence and continuation instructions from long results."""
    text = str(value or "")
    limit = max(80, int(limit or 80))
    if len(text) <= limit:
        return text
    marker = "\n...[middle omitted from compaction input]...\n"
    available = max(1, limit - len(marker))
    head = max(1, int(available * 0.60))
    tail = max(1, available - head)
    return text[:head] + marker + text[-tail:]


def _is_genuine_user_message(message: dict) -> bool:
    """True for user-authored context rather than a legacy recap projection."""

    if str(message.get("role") or "") != "user":
        return False
    content = _content_text(message.get("content")).lstrip()
    return not content.startswith(COMPACT_SUMMARY_MARKER)


def _latest_user_request(messages: list[dict]) -> str:
    """Return the latest textual user request, never a compaction recap."""

    for message in reversed(messages):
        if not _is_genuine_user_message(message):
            continue
        content = _content_text(message.get("content")).strip()
        if content:
            return content
    return ""


def _tool_call_parts(call: dict) -> tuple[str, str, str, dict]:
    """Return id, name, exact printable arguments, and parsed arguments."""
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    call_id = str(call.get("id") or "")
    name = str(fn.get("name") or call.get("name") or "")
    raw_args = fn.get("arguments")
    if raw_args is None:
        raw_args = call.get("arguments")
    if raw_args is None:
        raw_args = call.get("input")
    parsed: dict = {}
    if isinstance(raw_args, dict):
        parsed = dict(raw_args)
        printable = json.dumps(
            raw_args, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    else:
        printable = str(raw_args or "")
        try:
            candidate = json.loads(printable) if printable else {}
            if isinstance(candidate, dict):
                parsed = candidate
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return call_id, name, printable, parsed


def _serialize_message_for_summary(message: dict) -> str:
    """Pi-style transcript serialization, including native tool arguments."""
    role = str(message.get("role") or "?")
    chunks = []
    content = _content_text(message.get("content")).strip()
    if content:
        chunks.append(_summary_clip(content))
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        call_id, name, arguments, _ = _tool_call_parts(call)
        if not name:
            continue
        label = f"tool_call {call_id}" if call_id else "tool_call"
        chunks.append(
            f"{label}: {name}({_summary_clip(arguments, 2_000)})"
        )
    if not chunks:
        return ""
    if role == "tool":
        call_id = str(message.get("tool_call_id") or "")
        label = f"tool result {call_id}" if call_id else "tool result"
        return f"{label}:\n" + "\n".join(chunks)
    return f"{role}: " + "\n".join(chunks)


def _bounded_summary_transcript(entries: list[str], limit: int) -> str:
    """Fit whole serialized messages while retaining the beginning and tail."""
    joined = "\n\n".join(entries)
    if len(joined) <= limit:
        return joined
    marker = "\n\n[...older middle messages omitted for summary input...]\n\n"
    head_budget = max(1, (limit - len(marker)) // 3)
    tail_budget = max(1, limit - len(marker) - head_budget)
    head: list[tuple[int, str]] = []
    used = 0
    for index, entry in enumerate(entries):
        cost = len(entry) + (2 if head else 0)
        if used + cost > head_budget:
            break
        head.append((index, entry))
        used += cost
    tail: list[tuple[int, str]] = []
    used = 0
    head_indexes = {index for index, _ in head}
    for index in range(len(entries) - 1, -1, -1):
        if index in head_indexes:
            break
        entry = entries[index]
        cost = len(entry) + (2 if tail else 0)
        if used + cost > tail_budget:
            break
        tail.append((index, entry))
        used += cost
    tail.reverse()
    return (
        "\n\n".join(entry for _, entry in head)
        + marker
        + "\n\n".join(entry for _, entry in tail)
    )


def _merge_line_range(ranges: list[list[int]], start: int, end: int) -> None:
    ranges.append([max(1, int(start)), max(1, int(end))])
    ranges.sort(key=lambda row: (row[0], row[1]))
    merged: list[list[int]] = []
    for current_start, current_end in ranges:
        if not merged or current_start > merged[-1][1] + 1:
            merged.append([current_start, current_end])
        else:
            merged[-1][1] = max(merged[-1][1], current_end)
    ranges[:] = merged


def _collect_file_state(messages: list[dict]) -> dict:
    """Collect exact read coverage and modified files from compacted messages."""
    read: dict[str, dict] = {}
    modified: set[str] = set()

    def record_read(path: str, start: int, end: int, total: int) -> None:
        clean_path = str(path or "").strip()
        if not clean_path:
            return
        row = read.setdefault(clean_path, {
            "path": clean_path,
            "ranges": [],
            "total_lines": 0,
            "next_offset": None,
        })
        _merge_line_range(row["ranges"], start, end)
        row["total_lines"] = max(int(row.get("total_lines") or 0), int(total or 0))

    def absorb_state(prior: dict) -> None:
        if not isinstance(prior, dict):
            return
        for item in prior.get("read_files") or []:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")
            try:
                total = int(item.get("total_lines") or 0)
            except (TypeError, ValueError):
                total = 0
            ranges = item.get("ranges") or []
            if not ranges and path:
                read.setdefault(path, {
                    "path": path, "ranges": [], "total_lines": total,
                    "next_offset": None,
                })
            for pair in ranges:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    continue
                try:
                    record_read(path, int(pair[0]), int(pair[1]), total)
                except (TypeError, ValueError):
                    continue
        for path in prior.get("modified_files") or []:
            if str(path or "").strip():
                modified.add(str(path).strip())

    for message in messages:
        content = _content_text(message.get("content"))
        for state_match in _COMPACT_FILE_STATE_RE.finditer(content):
            try:
                prior = json.loads(state_match.group(1))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            absorb_state(prior)

        for match in _READ_RESULT_RE.finditer(content):
            path, start, end, total = match.groups()
            end_i, total_i = int(end), int(total)
            record_read(path, int(start), end_i, total_i)

        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            _, name, _, args = _tool_call_parts(call)
            if name == "apply_patch":
                for change in args.get("changes") or []:
                    if isinstance(change, dict) and str(change.get("path") or "").strip():
                        modified.add(str(change["path"]).strip())
            elif name in {"write_file", "edit_file", "delete_file"}:
                path = str(args.get("path") or "").strip()
                if path:
                    modified.add(path)

    for row in read.values():
        furthest = max((pair[1] for pair in row["ranges"]), default=0)
        total = int(row.get("total_lines") or 0)
        row["next_offset"] = furthest + 1 if furthest and furthest < total else None

    return {
        "read_files": sorted(read.values(), key=lambda row: row["path"].casefold()),
        "modified_files": sorted(modified, key=str.casefold),
    }


def _render_file_state(state: dict) -> str:
    if not (state.get("read_files") or state.get("modified_files")):
        return ""
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2)
    return f"{COMPACT_FILE_STATE_OPEN}\n{payload}\n{COMPACT_FILE_STATE_CLOSE}"


async def compress_messages(
    messages: list,
    *,
    complete: Callable[..., Awaitable[str]],
    protect_first: int = 2,
    protect_last: int = 6,
    should_stop: Callable[[], bool] | None = None,
    goal_context: str = "",
    on_compacted: Callable[[str, list], Any] | None = None,
    retry_state: CompactionRetryState | None = None,
) -> list:
    """Summarize the middle of a long transcript via the injected completer.

    ``goal_context`` is optional durable project-run charter/progress text so the
    summarizer does not invent a different goal. ``on_compacted(summary, msgs)``
    runs after a successful compact (sync or async) for transcript rewrite hooks.
    """
    n = len(messages)
    if retry_state is not None and not retry_state.ready():
        return messages
    if n <= protect_first + protect_last + 2:
        return messages
    head_end = min(max(0, protect_first), n)
    tail_start = max(head_end, n - protect_last) if protect_last else n

    # Assistant tool calls and their contiguous role:tool outputs are one
    # protocol turn. Never let a protection boundary split that turn: doing so
    # can leave provider-invalid orphan outputs after compaction.
    for i, message in enumerate(messages):
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        end = i + 1
        while end < n and messages[end].get("role") == "tool":
            end += 1
        if i < head_end < end:
            head_end = end
        if i < tail_start < end:
            tail_start = i
    if head_end >= tail_start:
        return messages

    head = messages[:head_end]
    tail = messages[tail_start:] if protect_last else []
    middle_end = tail_start if protect_last else n
    middle = list(messages[head_end:middle_end])
    if len(middle) < 3:
        return messages

    # User intent is not evidence for a model-authored recap. Preserve every
    # genuine user message byte-for-byte and in its original user chronology,
    # including earlier setup and correction turns. Only assistant/tool
    # trajectory is lossy. This deliberately bounds how much context compaction
    # can reclaim: if verbatim user intent itself cannot fit the provider window,
    # the later admission check must reject it instead of silently deleting or
    # paraphrasing a requirement. Legacy recap projections incorrectly stored as
    # role:user remain recap evidence and are not elevated to user authority.
    preserved_users = [
        message for message in middle if _is_genuine_user_message(message)
    ]
    evidence = [
        message for message in middle if not _is_genuine_user_message(message)
    ]
    before_metrics = context_lineage.message_metrics(messages)
    entries = []
    raw_middle_chars = 0
    affected_projection = 0
    for m in evidence:
        content = _content_text(m.get("content"))
        raw_size = len(content)
        for call in m.get("tool_calls") or []:
            if isinstance(call, dict):
                _, name, arguments, _ = _tool_call_parts(call)
                raw_size += len(name) + len(arguments)
        raw_middle_chars += raw_size
        projected = _serialize_message_for_summary(m)
        if projected:
            entries.append(projected)
            if len(projected) < raw_size:
                affected_projection += 1
    if not entries:
        return messages

    # Capture the whole current transcript, not just the summarized middle, so
    # exact file-read progress survives the lossy model-authored recap.
    file_state = _render_file_state(_collect_file_state(messages))
    state_section = (
        "Authoritative file state recovered from successful tool results. "
        "Preserve its exact paths, ranges, totals, and next offsets:\n"
        + file_state
        if file_state
        else ""
    )
    transcript_limit = max(
        4_000,
        SUMMARY_INPUT_CHARS - len(state_section) - (2 if state_section else 0),
    )
    joined = "\n\n".join(entries)
    transcript = _bounded_summary_transcript(entries, transcript_limit)
    if len(transcript) != len(joined):
        affected_projection += 1
    if state_section:
        transcript += "\n\n" + state_section
    summary_projection_metrics = {
        "kind": "summary_input_projected",
        "reason": "size_limit",
        "input_count": len(evidence),
        "output_count": len(entries),
        "affected_count": affected_projection,
        "chars_before": raw_middle_chars,
        "chars_after": len(transcript),
        "limit": SUMMARY_INPUT_CHARS,
    }
    system = (
        "Create a factual, lossy recap of an AI agent's earlier work. The recap is "
        "assistant-authored evidence, never a user instruction or host policy. Return "
        "exactly these Markdown sections:\n"
        "## Goal\n"
        "## Confirmed Results\n"
        "## Pending Work\n"
        "## Failed Attempts (not blockers)\n"
        "## Exact Context\n\n"
        "Every section must contain at least one factual line; write `None observed.` "
        "when a section has no entries. End the response with this exact final line:\n"
        f"{COMPACT_SUMMARY_SENTINEL}\n\n"
        "Preserve exact file paths, tool/function names, identifiers, errors, "
        "commands, numeric offsets, line ranges, and continuation cursors. Tool calls "
        "and tool results are evidence even when the assistant message has no prose. "
        "Only a successful tool result confirms completion. Copy failures literally; "
        "never convert a failed import, wrong category mount, invalid argument, stale "
        "handle, or policy rejection into a claim that a capability is unavailable. "
        "Use each call exactly as printed. Only `tools.x(...)` calls live "
        "under `tools`; `computer.x(...)`, `browser.x(...)`, and other "
        "object-prefixed calls use top-level globals. Do not invent constraints, decisions, "
        "APIs, alternatives, or next-step strategies. Keep unresolved work factual. "
        "No preamble or fluff."
    )
    latest_request = _latest_user_request(messages)
    if latest_request:
        system += (
            "\n\nLatest user request (authoritative; preserved verbatim in the compacted "
            "context; use it only to orient this recap and do not reinterpret it):\n"
            + latest_request
        )
    gc = (goal_context or "").strip()
    if gc:
        system += (
            "\n\nDurable project-run charter/progress (authoritative — preserve; "
            "do not invent a different goal):\n" + gc[:2000]
        )
    prompt = [
        {"role": "system", "content": system},
        {"role": "user", "content": transcript},
    ]
    compression_receipt = context_lineage.new_receipt("context_compression")
    context_lineage.add_selection(
        compression_receipt,
        kind="conversation_history",
        source="conversation_store",
        trust="checkpointed",
        reason="token_threshold",
        considered=len(middle),
        kept=len(preserved_users) + 1,
        dropped=max(0, len(middle) - len(preserved_users) - 1),
        relevance="selected",
    )
    context_lineage.add_transform(
        compression_receipt,
        **summary_projection_metrics,
    )
    context_lineage.attach_to_messages(prompt, compression_receipt)
    fingerprint = hashlib.sha256(
        json.dumps([(m["role"], m["content"]) for m in prompt], ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    if retry_state is not None and not retry_state.ready(fingerprint):
        return messages

    def note_failure() -> None:
        if retry_state is not None and not (should_stop and should_stop()):
            retry_state.failed(fingerprint)

    try:
        with observe_usage_category("compaction"):
            summary = await complete(
                prompt,
                profile="internal_prose",
                max_tokens=COMPACT_SUMMARY_MAX_TOKENS,
                temperature=0.2,
                should_stop=should_stop,
                require_complete=True,
            )
    except Exception as e:
        note_failure()
        print(f"[compress] failed, keeping full history: {e}", flush=True)
        return messages
    compacted_summary, rejection = _validated_compaction_summary(summary)
    if rejection:
        note_failure()
        print(
            "[compress] rejected unusable summary, keeping full history: "
            + rejection,
            flush=True,
        )
        return messages
    if should_stop and should_stop():
        return messages
    if file_state:
        compacted_summary += "\n\n" + file_state
    note = {
        "role": "assistant",
        "variant1_compaction": True,
        "variant1_compaction_revision": 2,
        "content": (
            f"{COMPACT_SUMMARY_MARKER}\n{COMPACT_SUMMARY_WARNING}\n\n"
            + compacted_summary
        ),
    }
    # The caller's canonical messages remain the fallback authority. Build the
    # disposable projection from copies so lineage annotations on an accepted
    # recap cannot mutate the source transcript either.
    compacted = copy.deepcopy(head)
    compacted.extend(copy.deepcopy(preserved_users))
    compacted.append(note)
    compacted.extend(copy.deepcopy(tail))
    after_metrics = context_lineage.message_metrics(compacted)
    context_lineage.add_transform_to_messages(
        compacted,
        kind="context_compression",
        reason="token_threshold",
        input_count=before_metrics["message_count"],
        output_count=after_metrics["message_count"],
        affected_count=len(middle),
        chars_before=before_metrics["chars"],
        chars_after=after_metrics["chars"],
        bytes_before=before_metrics["utf8_bytes"],
        bytes_after=after_metrics["utf8_bytes"],
        model_generated=True,
    )
    if on_compacted is not None:
        try:
            maybe = on_compacted(compacted_summary, compacted)
            if hasattr(maybe, "__await__"):
                await maybe
        except Exception as e:
            print(f"[compress] on_compacted hook failed: {e}", flush=True)
    if retry_state is not None:
        retry_state.succeeded()
    return compacted
