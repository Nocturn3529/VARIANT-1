"""Structured clarification over Work Fabric's durable interaction record.

The model proposes one to three concise questions through ``ask_user``.  The
harness validates them, renders the composer card, pauses the current tool
call, and resumes the same model loop with the user's answers or a skip result.
"""

from __future__ import annotations

import copy
from typing import Any

import tools
from run_context import current_run_context
from tool_core import ToolProjectionResult


CLARIFICATION_TIMEOUT_S = 1800
MAX_QUESTIONS = 3
MAX_OPTIONS = 4


def interaction_request(record: Any) -> dict | None:
    """Project an open Work interaction onto the inline composer protocol."""

    if record is None or getattr(record, "terminal", True):
        return None
    kind = str(getattr(record, "kind", "") or "")
    if kind == "clarification":
        questions = list(getattr(record, "schema", {}).get("questions") or ())
    elif kind == "goal_input":
        metadata = dict(getattr(record, "metadata", {}) or {})
        questions = [{
            "id": "q1",
            "question": str(getattr(record, "prompt", "") or "Input required"),
            "header": str(metadata.get("title") or "Goal input"),
            "multiSelect": False,
            "options": [],
        }]
    else:
        return None
    return {
        "type": "clarification:request",
        "id": str(record.interaction_id),
        "kind": kind,
        "chat_id": str(getattr(record.scope, "chat_id", "") or ""),
        "run_id": str(getattr(record, "metadata", {}).get("run_id") or ""),
        "questions": questions,
    }


def pending_interactions(interactions: Any, chat_id: str) -> list[dict]:
    """Complete open-question projection for one explicitly selected chat."""
    if not chat_id:
        return []
    rows = interactions.list(status="open", chat_id=chat_id, limit=1000)
    return [projected for row in sorted(rows, key=lambda item: item.created_at)
            if str(row.scope.chat_id or "") == chat_id
            and (projected := interaction_request(row)) is not None]


def pending_goal_input(interactions: Any, chat_id: str) -> Any | None:
    """Return the oldest open goal question owned by one durable chat."""

    target = str(chat_id or "")
    if not target:
        return None
    rows = interactions.list(status="open", chat_id=target,
                             kind="goal_input", oldest_first=True, limit=1)
    matching = [
        row for row in rows
        if row.kind == "goal_input" and str(row.scope.chat_id or "") == target
    ]
    return min(matching, key=lambda row: row.created_at) if matching else None

def _clean_text(value: object, *, field: str, maximum: int) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        raise tools.ToolError(f"ask_user: {field} cannot be empty")
    if len(text) > maximum:
        raise tools.ToolError(f"ask_user: {field} must be at most {maximum} characters")
    return text


def normalize_questions(raw: object) -> list[dict]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_QUESTIONS:
        raise tools.ToolError("ask_user needs 1 to 3 questions")
    normalized: list[dict] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise tools.ToolError(f"ask_user: question {index} must be an object")
        unknown = set(item) - {"question", "header", "multiSelect", "options"}
        if unknown:
            raise tools.ToolError(
                f"ask_user: question {index} has unknown fields: {', '.join(sorted(unknown))}"
            )
        question = _clean_text(item.get("question"), field="question", maximum=300)
        header = _clean_text(item.get("header"), field="header", maximum=40)
        multi_select = item.get("multiSelect", False)
        if not isinstance(multi_select, bool):
            raise tools.ToolError(f"ask_user: question {index} multiSelect must be boolean")
        options = item.get("options")
        if not isinstance(options, list) or not 2 <= len(options) <= MAX_OPTIONS:
            raise tools.ToolError(
                f"ask_user: question {index} needs 2 to {MAX_OPTIONS} options"
            )
        seen: set[str] = set()
        normalized_options = []
        for option_index, option in enumerate(options, start=1):
            if not isinstance(option, dict):
                raise tools.ToolError(
                    f"ask_user: question {index} option {option_index} must be an object"
                )
            unknown_option = set(option) - {"label", "description"}
            if unknown_option:
                raise tools.ToolError(
                    f"ask_user: option {option_index} has unknown fields: "
                    f"{', '.join(sorted(unknown_option))}"
                )
            label = _clean_text(option.get("label"), field="option label", maximum=80)
            description = _clean_text(
                option.get("description"), field="option description", maximum=240,
            )
            key = label.casefold()
            if key == "other":
                raise tools.ToolError("ask_user: 'Other' is supplied by the interface")
            if key in seen:
                raise tools.ToolError(f"ask_user: duplicate option label {label!r}")
            seen.add(key)
            normalized_options.append({"label": label, "description": description})
        normalized.append({
            "id": f"q{index}",
            "question": question,
            "header": header,
            "multiSelect": multi_select,
            "options": normalized_options,
        })
    return normalized


def _normalize_answers(
    questions: list[dict], raw: object,
) -> dict[str, str | list[str]]:
    if not isinstance(raw, dict):
        raise ValueError("answers must be an object")
    output: dict[str, str | list[str]] = {}
    for question in questions:
        qid = question["id"]
        if qid not in raw:
            continue
        value = raw[qid]
        if question["multiSelect"]:
            if not isinstance(value, list):
                raise ValueError(f"{qid} must be an array")
            cleaned = []
            seen: set[str] = set()
            for item in value:
                text = " ".join(str(item or "").split()).strip()[:500]
                if text and text.casefold() not in seen:
                    seen.add(text.casefold())
                    cleaned.append(text)
            if cleaned:
                output[qid] = cleaned
        else:
            text = " ".join(str(value or "").split()).strip()[:500]
            if text:
                output[qid] = text
    return output


def resolve_response(
    interactions: Any,
    request_id: str,
    answers: object,
    *,
    skipped: bool,
    goals: Any = None,
    chat_id: str | None = None,
) -> bool:
    try:
        pending = interactions.get(str(request_id or ""))
    except Exception:
        return False
    if pending.kind not in {"clarification", "goal_input"} or pending.terminal:
        return False
    if chat_id is not None and str(pending.scope.chat_id or "") != chat_id:
        return False
    if pending.kind == "goal_input":
        if goals is None:
            return False
        raw = answers if isinstance(answers, dict) else {}
        answer = " ".join(str(raw.get("q1") or "").split()).strip()[:8000]
        if not skipped and not answer:
            return False
        try:
            if skipped:
                goals.dismiss_input(
                    pending.interaction_id,
                    expected_attention_version=pending.version,
                )
            else:
                goal = goals.get(pending.owner_id)
                if goal is None:
                    return False
                goals.answer_input(
                    pending.interaction_id,
                    answer,
                    expected_version=goal.version,
                    expected_attention_version=pending.version,
                )
            return True
        except Exception:
            return False
    questions = list(pending.schema.get("questions") or ())
    try:
        normalized = {} if skipped else _normalize_answers(questions, answers)
    except ValueError:
        return False
    try:
        interactions.resolve(
            pending.interaction_id,
            status=("skipped" if skipped else "answered"),
            response={"answers": normalized, "skipped": bool(skipped)},
            expected_version=pending.version,
        )
        return True
    except Exception:
        return False


def _format_result(questions: list[dict], result: dict) -> str:
    if result.get("skipped"):
        return "The user skipped these optional questions. Continue using reasonable assumptions."
    answers = result.get("answers") or {}
    rows = []
    for question in questions:
        if question["id"] not in answers:
            continue
        value = answers[question["id"]]
        rendered = ", ".join(value) if isinstance(value, list) else str(value)
        rows.append(f'"{question["question"]}" = "{rendered}"')
    if not rows:
        return "The user submitted no answers. Continue using reasonable assumptions."
    return "The user answered: " + "; ".join(rows) + ". Continue with these choices."


def _project_result(questions: list[dict], result: dict, terminal: Any) -> ToolProjectionResult:
    status = str(terminal.status)
    if status == "timed_out":
        display = "The optional clarification timed out. Continue using reasonable assumptions."
    elif status == "cancelled":
        display = "The clarification was cancelled."
    elif status == "skipped":
        display = _format_result(questions, {"skipped": True})
    else:
        display = _format_result(questions, result)
    answers = result.get("answers")
    value = {
        "interaction_id": terminal.interaction_id,
        "status": status,
        "answers": copy.deepcopy(answers) if status == "answered" and isinstance(answers, dict) else {},
    }
    return ToolProjectionResult(
        display,
        programmatic_value=value,
        receipt_metadata={"projection": "clarification-answer-v1", "status": status},
    )


async def tool_ask_user(interactions: Any, args: dict) -> ToolProjectionResult:
    questions = normalize_questions(args.get("questions"))
    ctx = current_run_context()
    if ctx is None or ctx.source != "chat" or ctx.chat_session is None:
        raise tools.ToolError("ask_user is available only in an interactive chat")
    websocket = ctx.chat_transport
    if websocket is None:
        raise tools.ToolError("ask_user cannot reach the active chat interface")

    try:
        from capability_broker import current_capability_invocation
        invocation = current_capability_invocation()
    except Exception:
        invocation = None
    idempotency_key = str(
        getattr(invocation, "idempotency_key", "")
        or getattr(invocation, "nested_call_id", "")
        or getattr(invocation, "outer_tool_call_id", "")
        or ""
    )
    pending = interactions.create(
        kind="clarification",
        prompt=" / ".join(item["question"] for item in questions),
        schema={"questions": questions},
        metadata={
            "source": "ask_user",
            "title": questions[0]["header"],
            "run_id": ctx.run_id,
        },
        owner_kind="run",
        owner_id=ctx.run_id,
        scope=ctx.work_scope,
        idempotency_key=idempotency_key,
        correlation_id=str(
            getattr(invocation, "nested_call_id", "")
            or getattr(invocation, "outer_tool_call_id", "")
            or ctx.run_id
        ),
    )
    waiter = getattr(invocation, "user_wait", None)
    if waiter is not None:
        waiter.set()
    try:
        from chat_pipeline import stream_meta
        await websocket.send_json({
            "type": "clarification:request",
            "id": pending.interaction_id,
            "run_id": ctx.run_id,
            "questions": questions,
            **stream_meta(ctx.chat_session),
            "chat_id": str(ctx.work_scope.chat_id or ""),
            "session_id": str(ctx.work_scope.chat_id or ""),
        })
        terminal = await interactions.wait(
            pending.interaction_id,
            timeout_s=CLARIFICATION_TIMEOUT_S,
        )
        if isinstance(terminal.response, dict):
            response = dict(terminal.response)
        elif terminal.response not in (None, ""):
            response = {
                "answers": {questions[0]["id"]: str(terminal.response)},
                "skipped": False,
            }
        else:
            response = {"skipped": terminal.status == "skipped"}
        return _project_result(questions, response, terminal)
    finally:
        if waiter is not None:
            waiter.clear()
        # An interrupted waiter must not leave an orphan question available
        # after the owning cell has ended. A racing submitted answer wins.
        from work_fabric.models import WorkConflict
        try:
            current = interactions.get(pending.interaction_id)
            if not current.terminal:
                interactions.resolve(pending.interaction_id, status="cancelled",
                                     expected_version=current.version)
        except WorkConflict:
            pass
        except Exception:
            # Cleanup failures must not replace the original answer or Stop.
            import logging
            logging.getLogger(__name__).exception("failed to retire interrupted clarification")
        try:
            await websocket.send_json({
                "type": "clarification:closed",
                "id": pending.interaction_id,
                "run_id": ctx.run_id,
                "chat_id": str(ctx.work_scope.chat_id or ""),
            })
        except Exception:
            pass
