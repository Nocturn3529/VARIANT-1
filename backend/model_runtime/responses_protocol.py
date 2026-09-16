"""Shared OpenAI Responses protocol projections."""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class ResponsesStreamEvent:
    payload: dict | None = None
    done: bool = False


class ResponsesSSEDecoder:
    """Shared framing and terminal accounting for Responses SSE adapters."""

    TERMINAL_TYPES = frozenset({
        "response.completed",
        "response.incomplete",
        "response.failed",
    })

    def __init__(self) -> None:
        self.saw_terminal = False

    def decode_line(self, line: str) -> ResponsesStreamEvent | None:
        if not line or not line.startswith("data:"):
            return None
        data = line[5:].strip()
        if not data:
            return None
        if data == "[DONE]":
            self.saw_terminal = True
            return ResponsesStreamEvent(done=True)
        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        if responses_event_type(payload) in self.TERMINAL_TYPES:
            self.saw_terminal = True
        return ResponsesStreamEvent(payload=payload)


def responses_event_type(payload: dict) -> str:
    return str(payload.get("type") or "")


def responses_tools(tools: list | None) -> list | None:
    """Project VARIANT-1 tool specifications onto Responses function tools."""
    if not tools:
        return None
    import tool_calling

    projected = []
    for item in tool_calling.to_openai_tools(tools):
        function = (item or {}).get("function") or {}
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        projected.append({
            "type": "function",
            "name": name,
            "description": function.get("description") or "",
            "strict": False,
            "parameters": function.get("parameters")
            or {"type": "object", "properties": {}},
        })
    return projected or None


def push_function_call(
    tool_call_sink,
    item: dict,
    index: int,
    *,
    responses_replay: dict | None = None,
) -> None:
    """Admit one identified, de-duplicated Responses function call."""
    if tool_call_sink is None or not isinstance(item, dict):
        return
    name = str(item.get("name") or "").strip()
    if not name:
        return
    arguments = item.get("arguments")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments or {}, ensure_ascii=False)

    # Argument-completion events can precede the authoritative output item.
    # Waiting for provider identity prevents duplicate side effects.
    call_id = str(item.get("call_id") or item.get("id") or "")
    if not call_id:
        return
    seen = getattr(tool_call_sink, "_responses_seen_call_ids", None)
    if not isinstance(seen, set):
        seen = set()
        try:
            setattr(tool_call_sink, "_responses_seen_call_ids", seen)
        except Exception:
            pass
    if call_id in seen:
        return
    seen.add(call_id)

    replay = {}
    if item.get("id"):
        replay = {"responses": {"item_id": str(item["id"])}}
    if isinstance(responses_replay, dict) and responses_replay:
        replay.setdefault("responses", {}).update(responses_replay)
    try:
        tool_call_sink.add_openai_delta([{
            "index": index,
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": arguments},
            **({"provider_replay": replay} if replay else {}),
        }])
    except Exception:
        pass
