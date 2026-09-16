"""Provider tool-calling helpers.

Converts VARIANT-1 tool specs into provider schemas (OpenAI / Anthropic / Gemini),
accumulates streamed tool-call deltas, and encodes calls into the graph's
private execution representation so existing state-machine code stays unchanged.

The model uses the provider's tool interface instead of inventing free-form JSON
``actions`` arrays. VARIANT-1 projects only its single Python action there.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# Spec → provider schemas
# ---------------------------------------------------------------------------

def _param_type(spec: dict) -> str:
    t = str((spec or {}).get("type") or "string").lower()
    if t in ("int", "integer"):
        return "integer"
    if t in ("number", "float", "double"):
        return "number"
    if t in ("bool", "boolean"):
        return "boolean"
    if t in ("array", "list"):
        return "array"
    if t in ("object", "dict"):
        return "object"
    return "string"


def _json_schema_props(params: dict | None) -> tuple[dict, list[str]]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, raw in (params or {}).items():
        p = raw if isinstance(raw, dict) else {}
        properties[str(name)] = _json_schema_param(p)
        if p.get("required"):
            required.append(str(name))
    return properties, required


def _json_schema_param(raw: dict | None) -> dict[str, Any]:
    """Recursively retain the provider-neutral JSON-schema subset."""
    p = raw if isinstance(raw, dict) else {}
    entry: dict[str, Any] = {"type": _param_type(p)}
    desc = str(p.get("desc") or p.get("description") or "").strip()
    if desc:
        entry["description"] = desc
    enum = p.get("enum")
    if isinstance(enum, (list, tuple)) and enum:
        entry["enum"] = list(enum)
    for key in (
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        "minLength", "maxLength", "pattern", "minItems", "maxItems",
        "minProperties", "maxProperties", "format", "default",
    ):
        if key in p:
            entry[key] = p[key]
    if entry["type"] == "array":
        item_spec = p.get("items") if isinstance(p.get("items"), dict) else {}
        entry["items"] = _json_schema_param(item_spec)
    if entry["type"] == "object":
        nested = p.get("properties")
        if isinstance(nested, dict):
            nested_props, nested_required = _json_schema_props(nested)
            entry["properties"] = nested_props
            declared_required = p.get("required")
            if isinstance(declared_required, list):
                nested_required = [str(v) for v in declared_required]
            if nested_required:
                entry["required"] = nested_required
        if isinstance(p.get("additionalProperties"), bool):
            entry["additionalProperties"] = p["additionalProperties"]
    return entry


def _description(spec: dict) -> str:
    # Tool eligibility and overlap are resolved structurally by the disclosed
    # tool set. Provider schemas describe capability/arguments only; they do not
    # carry corrective "use when / do not use when" nudges.
    return str(spec.get("description") or "").strip()[:1024]


def to_openai_tools(specs: Iterable[dict]) -> list[dict]:
    """OpenAI / xAI / llama.cpp chat-completions tools array."""
    out = []
    for spec in specs or []:
        name = str(spec.get("name") or "").strip()
        if not name:
            continue
        props, required = _json_schema_props(spec.get("params"))
        parameters: dict[str, Any] = {
            "type": "object",
            "properties": props,
            "additionalProperties": False,
        }
        if required:
            parameters["required"] = required
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": _description(spec),
                "parameters": parameters,
            },
        })
    return out


def to_anthropic_tools(specs: Iterable[dict]) -> list[dict]:
    """Anthropic Messages API tools array."""
    out = []
    for spec in specs or []:
        name = str(spec.get("name") or "").strip()
        if not name:
            continue
        props, required = _json_schema_props(spec.get("params"))
        schema: dict[str, Any] = {
            "type": "object",
            "properties": props,
            "additionalProperties": False,
        }
        if required:
            schema["required"] = required
        out.append({
            "name": name,
            "description": _description(spec),
            "input_schema": schema,
        })
    return out


def to_gemini_tools(specs: Iterable[dict]) -> list[dict]:
    """Gemini functionDeclarations (single tool config entry)."""
    decls = []
    for spec in specs or []:
        name = str(spec.get("name") or "").strip()
        if not name:
            continue
        props, required = _json_schema_props(spec.get("params"))
        params: dict[str, Any] = {
            "type": "OBJECT",
            "properties": {
                k: _gemini_schema(v) for k, v in props.items()
            },
        }
        if required:
            params["required"] = required
        decls.append({
            "name": name,
            "description": _description(spec),
            "parameters": params,
        })
    if not decls:
        return []
    return [{"functionDeclarations": decls}]


def _gemini_schema(schema: dict | None) -> dict[str, Any]:
    """Convert schema types recursively without discarding constraints."""
    source = schema if isinstance(schema, dict) else {}
    out: dict[str, Any] = {}
    out["type"] = {
        "string": "STRING",
        "integer": "INTEGER",
        "number": "NUMBER",
        "boolean": "BOOLEAN",
        "array": "ARRAY",
        "object": "OBJECT",
    }.get(str(source.get("type") or "string").lower(), "STRING")
    for key, value in source.items():
        if key == "type":
            continue
        if key == "items" and isinstance(value, dict):
            out["items"] = _gemini_schema(value)
        elif key == "properties" and isinstance(value, dict):
            out["properties"] = {
                str(name): _gemini_schema(child)
                for name, child in value.items()
            }
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Stream accumulation
# ---------------------------------------------------------------------------

@dataclass
class ToolCallAccumulator:
    """Collect partial tool-call deltas from OpenAI / Anthropic / Gemini streams."""

    _by_index: dict[int, dict] = field(default_factory=dict)
    _anthropic_blocks: dict[int, dict] = field(default_factory=dict)
    _gemini: list[dict] = field(default_factory=list)

    def add_openai_delta(self, tool_calls: list | None) -> None:
        for tc in tool_calls or []:
            if not isinstance(tc, dict):
                continue
            try:
                idx = int(tc.get("index", 0))
            except (TypeError, ValueError):
                idx = 0
            slot = self._by_index.setdefault(
                idx, {
                    "id": "",
                    "name": "",
                    "arguments": "",
                    "provider_replay": {},
                })
            if tc.get("id"):
                slot["id"] = str(tc["id"])
            replay = tc.get("provider_replay")
            if isinstance(replay, dict):
                slot["provider_replay"].update(replay)
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            if fn.get("name"):
                # Name is usually sent once (full), but tolerate fragments.
                name = str(fn["name"])
                if not slot["name"]:
                    slot["name"] = name
                elif name.startswith(slot["name"]):
                    slot["name"] = name
                elif slot["name"] not in name:
                    slot["name"] += name
            if fn.get("arguments") is not None:
                arguments = fn.get("arguments")
                if isinstance(arguments, (dict, list)):
                    # Some compatible servers send one already-decoded object
                    # instead of JSON text deltas. It is a complete value, not
                    # a Python-repr fragment to concatenate.
                    slot["arguments"] = json.dumps(
                        arguments,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    slot["argument_snapshot"] = arguments
                    slot["post_snapshot_arguments"] = ""
                else:
                    if "argument_snapshot" in slot:
                        slot["post_snapshot_arguments"] += str(arguments or "")
                        if not slot["argument_snapshot"]:
                            slot["arguments"] = slot["post_snapshot_arguments"] or "{}"
                    else:
                        slot["arguments"] += str(arguments or "")

    def anthropic_block_start(self, index: int, block: dict) -> None:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            return
        self._anthropic_blocks[int(index)] = {
            "id": str(block.get("id") or f"toolu_{index}"),
            "name": str(block.get("name") or ""),
            "arguments": "",
            "input": block.get("input") if isinstance(block.get("input"), dict) else None,
        }

    def anthropic_input_json_delta(self, index: int, partial_json: str) -> None:
        slot = self._anthropic_blocks.get(int(index))
        if slot is None:
            return
        slot["arguments"] += str(partial_json or "")

    def anthropic_block_stop(self, index: int) -> None:
        # No-op; finalize in actions().
        return

    def add_gemini_function_call(
        self,
        call_or_name: dict | str,
        args: dict | None = None,
        *,
        call_id: str = "",
        thought_signature: str = "",
    ) -> None:
        """Add one complete Gemini functionCall without losing replay state."""
        if isinstance(call_or_name, dict):
            record = call_or_name
            name = str(record.get("name") or "")
            args = record.get("args") if isinstance(record.get("args"), dict) else {}
            call_id = str(record.get("id") or call_id or "")
            replay = record.get("provider_replay")
            provider_replay = dict(replay) if isinstance(replay, dict) else {}
        else:
            name = str(call_or_name or "")
            provider_replay = {}
        if thought_signature:
            gemini = provider_replay.setdefault("gemini", {})
            if isinstance(gemini, dict):
                gemini["thought_signature"] = str(thought_signature)
        if call_id:
            gemini = provider_replay.setdefault("gemini", {})
            if isinstance(gemini, dict):
                gemini.setdefault("id", call_id)
        if not name:
            return
        # Gemini gateways can repeat a completed part in multiple stream
        # events. IDs are best; candidate/part coordinates preserve two
        # intentional identical parallel calls while deduplicating a repeated
        # unsigned part.
        if call_id and any(
            str(existing.get("id") or "") == call_id
            for existing in self._gemini
        ):
            return
        if not call_id:
            gemini = provider_replay.get("gemini")
            def coordinate_of(value) -> tuple[int, int]:
                if not isinstance(value, dict):
                    return (-1, -1)
                try:
                    return (
                        int(value.get("candidate_index", -1)),
                        int(value.get("part_index", -1)),
                    )
                except (TypeError, ValueError):
                    return (-1, -1)

            coordinate = coordinate_of(gemini)
            canonical_args = json.dumps(
                args if isinstance(args, dict) else {},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if coordinate != (-1, -1) and any(
                str(existing.get("name") or "") == name
                and str(existing.get("arguments") or "") == canonical_args
                and coordinate_of(
                    (existing.get("provider_replay") or {}).get("gemini")
                ) == coordinate
                for existing in self._gemini
            ):
                return
        self._gemini.append({
            "id": call_id or f"gemini_{len(self._gemini)}",
            "name": str(name),
            "arguments": json.dumps(
                args if isinstance(args, dict) else {},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "provider_replay": provider_replay,
        })

    def actions(self) -> list[dict]:
        out: list[dict] = []
        for idx in sorted(self._by_index):
            slot = self._by_index[idx]
            name = (slot.get("name") or "").strip()
            if not name:
                continue
            try:
                args = _parse_args(slot.get("arguments") or "")
                argument_error = ""
                trailing = str(slot.get("post_snapshot_arguments") or "").strip()
                if trailing and slot.get("argument_snapshot"):
                    try:
                        streamed = _parse_args(trailing)
                    except ValueError:
                        # The decoded object is already a complete value;
                        # redundant partial text cannot be appended to it.
                        streamed = args
                    if streamed != args:
                        raise ValueError("conflicting structured and streamed tool arguments")
            except ValueError as exc:
                args = {}
                argument_error = str(exc)
            action = {
                "tool": name,
                "args": args,
                "id": slot.get("id") or f"call_{idx}",
            }
            if argument_error:
                action["argument_error"] = argument_error
            if slot.get("provider_replay"):
                action["provider_replay"] = dict(slot["provider_replay"])
            out.append(action)
        for idx in sorted(self._anthropic_blocks):
            slot = self._anthropic_blocks[idx]
            name = (slot.get("name") or "").strip()
            if not name:
                continue
            argument_error = ""
            if (
                isinstance(slot.get("input"), dict)
                and (slot["input"] or not str(slot.get("arguments") or "").strip())
            ):
                args = slot["input"]
            else:
                try:
                    args = _parse_args(slot.get("arguments") or "")
                except ValueError as exc:
                    args = {}
                    argument_error = str(exc)
            action = {
                "tool": name,
                "args": args,
                "id": slot.get("id") or f"toolu_{idx}",
            }
            if argument_error:
                action["argument_error"] = argument_error
            out.append(action)
        for slot in self._gemini:
            name = (slot.get("name") or "").strip()
            if not name:
                continue
            try:
                args = _parse_args(slot.get("arguments") or "")
                argument_error = ""
            except ValueError as exc:
                args = {}
                argument_error = str(exc)
            action = {"tool": name, "args": args, "id": slot.get("id") or "gemini_0"}
            if argument_error:
                action["argument_error"] = argument_error
            if slot.get("provider_replay"):
                action["provider_replay"] = dict(slot["provider_replay"])
            out.append(action)
        return out

    @property
    def has_calls(self) -> bool:
        return bool(self.actions())


def _parse_args(raw: str) -> dict:
    s = (raw or "").strip()
    if not s:
        return {}
    def _reject_constant(token: str):
        raise ValueError(f"non-finite JSON number {token!r} is not allowed")
    try:
        val = json.loads(s, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        position = int(getattr(exc, "pos", 0) or 0)
        raise ValueError(
            f"malformed tool arguments at character {position}; "
            "send one complete JSON object with finite numbers"
        ) from exc
    if not isinstance(val, dict):
        raise ValueError("tool arguments must be one JSON object")
    return val


def _exact_outcome_segments(
    actions: list[dict],
    outcomes: list[dict],
) -> tuple[list[str], list[dict]]:
    """Bind executor outcomes to native calls without parsing tool-owned text.

    ``ToolBatchResult.text`` is a display/log string. It is not a
    causal transport: a tool is allowed to return arbitrary bracketed text, so
    regex-splitting that aggregate can misattribute one call's data to another.
    Exact call IDs win; no-ID outcomes fall back to stable tool order.
    """
    clean_outcomes = [
        dict(row) for row in (outcomes or []) if isinstance(row, dict)
    ]
    by_id = {
        str(row.get("call_id") or ""): index
        for index, row in enumerate(clean_outcomes)
        if str(row.get("call_id") or "")
    }
    used: set[int] = set()
    segments: list[str] = []
    aligned: list[dict] = []
    for index, action in enumerate(actions or []):
        tool = str((action or {}).get("tool") or "")
        call_id = str((action or {}).get("id") or f"call_{index}")
        outcome_index = by_id.get(call_id)
        if outcome_index in used:
            outcome_index = None
        if outcome_index is None:
            outcome_index = next((
                candidate
                for candidate, row in enumerate(clean_outcomes)
                if candidate not in used
                and str(row.get("tool") or "") == tool
            ), None)
        if outcome_index is None:
            missing = {
                "tool": tool,
                "call_id": call_id,
                "model_result": "no result returned for this call",
                "result": "",
                "ok": False,
                "executed": False,
                "source_chars": 0,
                "visible_chars": 0,
                "truncated": False,
            }
            aligned.append(missing)
            segments.append("no result returned for this call")
            continue
        used.add(outcome_index)
        row = clean_outcomes[outcome_index]
        model_result = str(
            row.get("model_result")
            if row.get("model_result") is not None
            else row.get("result") or ""
        )
        aligned.append(row)
        segments.append(model_result)
    return segments, aligned


def format_assistant_turn_message(
    actions: list[dict],
    *,
    assistant_text: str = "",
) -> dict:
    """Encode one assistant turn, including its provider-native tool calls."""
    actions = [a for a in (actions or []) if isinstance(a, dict) and a.get("tool")]
    tool_calls = []
    for i, a in enumerate(actions):
        tid = str(a.get("id") or f"call_{i}")
        args = a.get("args") if isinstance(a.get("args"), dict) else {}
        replay = a.get("provider_replay")
        replay = dict(replay) if isinstance(replay, dict) else {}
        tool_calls.append({
            "id": tid,
            "type": "function",
            "function": {
                "name": str(a["tool"]),
                "arguments": json.dumps(args, ensure_ascii=False),
            },
            **({"provider_replay": replay} if replay else {}),
        })
    message: dict = {
        "role": "assistant",
        # Empty string (not null) — some OpenAI-compatible servers reject null content.
        "content": (assistant_text or "").strip(),
        "tool_calls": tool_calls,
    }
    if not tool_calls:
        message.pop("tool_calls", None)
    return message


def durable_provider_replay_actions(actions: list[dict]) -> list[dict]:
    """Normalize tool actions while retaining provider replay durably."""

    durable: list[dict] = []
    for raw in actions or []:
        if not isinstance(raw, dict):
            continue
        action = dict(raw)
        replay = action.get("provider_replay")
        if isinstance(replay, dict) and replay:
            action["provider_replay"] = dict(replay)
        else:
            action.pop("provider_replay", None)
        durable.append(action)
    return durable


def format_tool_result_messages(
    actions: list[dict],
    *,
    outcomes: list[dict],
) -> list[dict]:
    """Encode exact call-bound results without repeating the assistant turn."""
    actions = [a for a in (actions or []) if isinstance(a, dict) and a.get("tool")]
    raw_results, aligned = _exact_outcome_segments(actions, outcomes)
    messages = []
    for i, content in enumerate(raw_results):
        tid = str(actions[i].get("id") or f"call_{i}")
        outcome = aligned[i]
        is_error = bool(outcome.get("is_error")) or outcome.get("ok") is False
        messages.append({
            "role": "tool",
            "tool_call_id": tid,
            "content": content,
            **({"is_error": True} if is_error else {}),
        })
    return messages


def should_send_provider_tools(*, mode: str, tool_specs: list | None,
                               force: Optional[bool] = None) -> bool:
    """Whether this turn should send provider function schemas."""
    if force is not None:
        return bool(force) and bool(tool_specs)
    if not tool_specs:
        return False
    # Cloud always supports tools; local uses OpenAI-compatible tools (llama.cpp).
    return True
