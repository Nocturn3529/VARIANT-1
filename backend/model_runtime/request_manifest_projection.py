"""Provider-wire projection for metadata-only model request manifests.

Each provider renderer consumes its finalized wire payload and returns the same
structural message projection. Prompt and tool values never leave this module.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from typing import Any, Callable


MAX_ARGUMENT_KEYS = 64
_SAFE_ROLES = {"system", "developer", "user", "assistant", "tool", "model"}
_SAFE_PART_TYPES = {
    "text", "input_text", "output_text", "image", "image_url", "input_image",
    "tool_use", "tool_result", "function_call", "function_call_output",
    "functionCall", "functionResponse", "inline_data", "inlineData",
}
_MIME_RE = re.compile(r"^[a-z0-9.+-]+/[a-z0-9.+-]+$", re.IGNORECASE)


def _safe_role(value: Any) -> str:
    role = str(value or "").strip().lower()
    return role if role in _SAFE_ROLES else "other"


def _safe_part_type(value: Any) -> str:
    kind = str(value or "").strip()
    return kind if kind in _SAFE_PART_TYPES else "other"


def _safe_name(value: Any) -> str:
    # Tool names and argument keys are capability metadata, not content.  Keep
    # them bounded so a hostile remote schema cannot turn telemetry into a
    # second unbounded observation channel.
    if not isinstance(value, (str, int, float, bool)):
        return ""
    return str(value or "").strip()[:160]


def _safe_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    return value


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _safe_mime(value: Any) -> str:
    mime = str(value or "").strip().lower()[:100]
    return mime if _MIME_RE.fullmatch(mime) else "application/octet-stream"


def _utf8_bytes(value: Any) -> int:
    if value is None:
        return 0
    if not isinstance(value, str):
        value = str(value)
    return len(value.encode("utf-8", errors="replace"))


def _chars(value: Any) -> int:
    if value is None:
        return 0
    return len(value if isinstance(value, str) else str(value))


def _canonical_json_chunks(value: Any):
    try:
        yield from json.JSONEncoder(
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).iterencode(value)
    except (TypeError, ValueError):
        yield json.dumps(str(type(value).__name__), separators=(",", ":"))


def _json_metrics(value: Any) -> tuple[int, int]:
    chars = 0
    byte_count = 0
    for chunk in _canonical_json_chunks(value):
        chars += len(chunk)
        byte_count += len(chunk.encode("utf-8", errors="replace"))
    return chars, byte_count


def _canonical_json_hash_metrics(value: Any) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    for chunk in _canonical_json_chunks(value):
        encoded = chunk.encode("utf-8", errors="replace")
        digest.update(encoded)
        byte_count += len(encoded)
    return digest.hexdigest(), byte_count


def _estimated_tokens(byte_count: int) -> int:
    return int(math.ceil(max(0, int(byte_count or 0)) / 4.0))


def _base64_decoded_size(value: str) -> int:
    data = str(value or "").strip()
    if not data:
        return 0
    padding = 2 if data.endswith("==") else 1 if data.endswith("=") else 0
    return max(0, (len(data) * 3) // 4 - padding)


def _png_dimensions(value: str) -> tuple[int | None, int | None]:
    """Read only the small PNG header needed for dimensions."""
    try:
        prefix = base64.b64decode(str(value or "")[:44], validate=False)
        if len(prefix) >= 24 and prefix[:8] == b"\x89PNG\r\n\x1a\n":
            return (
                int.from_bytes(prefix[16:20], "big"),
                int.from_bytes(prefix[20:24], "big"),
            )
    except Exception:
        pass
    return None, None


def _image_descriptor(
    data: Any,
    *,
    mime: Any,
    anchor: int,
    role: Any,
) -> dict:
    encoded = str(data or "")
    width, height = _png_dimensions(encoded) if "png" in str(mime or "").lower() \
        else (None, None)
    return {
        "anchor": int(anchor),
        "role": _safe_role(role),
        "mime": _safe_mime(mime),
        "encoded_chars": len(encoded),
        "estimated_decoded_bytes": _base64_decoded_size(encoded),
        "width": width,
        "height": height,
    }


def _data_url_image(value: Any, *, anchor: int, role: Any) -> dict | None:
    url = str(value or "")
    if not url.startswith("data:") or ";base64," not in url:
        return None
    head, data = url.split(";base64,", 1)
    return _image_descriptor(
        data, mime=head[5:] or "application/octet-stream",
        anchor=anchor, role=role)


def _message_entry(position: int, role: Any, kind: str = "text") -> dict:
    return {
        "position": int(position),
        "role": _safe_role(role),
        "kind": _safe_part_type(kind),
        "text_chars": 0,
        "text_utf8_bytes": 0,
        "tool_argument_chars": 0,
        "tool_argument_utf8_bytes": 0,
        "tool_result_chars": 0,
        "tool_result_utf8_bytes": 0,
        "tool_calls": 0,
        "tool_results": 0,
        "tool_names": [],
        "images": 0,
        "part_types": [],
    }


def _add_part_type(entry: dict, value: Any) -> None:
    kind = _safe_part_type(value)
    if kind not in entry["part_types"]:
        entry["part_types"].append(kind)


def _add_text(entry: dict, value: Any) -> None:
    entry["text_chars"] += _chars(value)
    entry["text_utf8_bytes"] += _utf8_bytes(value)
    _add_part_type(entry, "text")


def _add_tool_args(entry: dict, value: Any) -> None:
    if isinstance(value, str):
        chars, byte_count = _chars(value), _utf8_bytes(value)
    else:
        chars, byte_count = _json_metrics(value)
    entry["tool_argument_chars"] += chars
    entry["tool_argument_utf8_bytes"] += byte_count


def _add_tool_result(entry: dict, value: Any) -> None:
    if isinstance(value, str):
        chars, byte_count = _chars(value), _utf8_bytes(value)
    else:
        chars, byte_count = _json_metrics(value)
    entry["tool_result_chars"] += chars
    entry["tool_result_utf8_bytes"] += byte_count


def _add_tool_name(entry: dict, value: Any) -> None:
    name = _safe_name(value)
    names = entry.setdefault("tool_names", [])
    if name and name not in names:
        names.append(name)


def _call_key(value: Any) -> str:
    key = str(value or "").strip()
    return key[3:] if key.startswith("fc_") else key


def _source_messages(messages: list | None) -> tuple[list[dict], list[str], list[str]]:
    entries: list[dict] = []
    calls: list[str] = []
    results: list[str] = []
    call_names: dict[str, str] = {}
    for position, msg in enumerate(messages or []):
        if not isinstance(msg, dict):
            continue
        role = _safe_role(msg.get("role"))
        entry = _message_entry(position, role)
        content = msg.get("content")
        if role == "tool":
            _add_tool_result(entry, content)
            entry["tool_results"] += 1
            _add_part_type(entry, "tool_result")
            result_id = _call_key(msg.get("tool_call_id") or msg.get("id"))
            results.append(result_id)
            _add_tool_name(entry, msg.get("name") or call_names.get(result_id))
        elif isinstance(content, str):
            _add_text(entry, content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    _add_text(entry, block)
                    continue
                kind = block.get("type")
                _add_part_type(entry, kind)
                if kind in {"text", "input_text", "output_text"}:
                    _add_text(entry, block.get("text", ""))
                elif kind in {"image", "image_url", "input_image"}:
                    entry["images"] += 1
        for call in msg.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            entry["tool_calls"] += 1
            _add_part_type(entry, "function_call")
            _add_tool_args(entry, fn.get("arguments"))
            call_id = _call_key(call.get("id"))
            name = _safe_name(fn.get("name"))
            calls.append(call_id)
            _add_tool_name(entry, name)
            if call_id and name:
                call_names[call_id] = name
        entries.append(entry)
    return entries, calls, results


def _openai_messages(payload: dict) -> tuple[list[dict], list[str], list[str], list[dict]]:
    entries: list[dict] = []
    calls: list[str] = []
    results: list[str] = []
    images: list[dict] = []
    call_names: dict[str, str] = {}
    for position, msg in enumerate(payload.get("messages") or []):
        if not isinstance(msg, dict):
            continue
        role = _safe_role(msg.get("role"))
        entry = _message_entry(position, role)
        content = msg.get("content")
        if role == "tool":
            _add_tool_result(entry, content)
            entry["tool_results"] += 1
            _add_part_type(entry, "tool_result")
            result_id = _call_key(msg.get("tool_call_id") or msg.get("id"))
            results.append(result_id)
            _add_tool_name(entry, msg.get("name") or call_names.get(result_id))
        elif isinstance(content, str):
            _add_text(entry, content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    _add_text(entry, block)
                    continue
                kind = block.get("type")
                _add_part_type(entry, kind)
                if kind in {"text", "input_text", "output_text"}:
                    _add_text(entry, block.get("text", ""))
                elif kind in {"image_url", "input_image"}:
                    image = block.get("image_url")
                    url = image.get("url") if isinstance(image, dict) else image
                    descriptor = _data_url_image(url, anchor=position, role=role)
                    if descriptor:
                        images.append(descriptor)
                        entry["images"] += 1
        for call in msg.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            entry["tool_calls"] += 1
            _add_part_type(entry, "function_call")
            _add_tool_args(entry, fn.get("arguments"))
            call_id = _call_key(call.get("id"))
            name = _safe_name(fn.get("name"))
            calls.append(call_id)
            _add_tool_name(entry, name)
            if call_id and name:
                call_names[call_id] = name
        entries.append(entry)
    return entries, calls, results, images


def _anthropic_messages(payload: dict) -> tuple[list[dict], list[str], list[str], list[dict]]:
    entries: list[dict] = []
    calls: list[str] = []
    results: list[str] = []
    images: list[dict] = []
    call_names: dict[str, str] = {}
    position = 0
    system = payload.get("system")
    if system:
        entry = _message_entry(position, "system")
        if isinstance(system, str):
            _add_text(entry, system)
        elif isinstance(system, list):
            for block in system:
                if isinstance(block, dict) and block.get("type") == "text":
                    _add_text(entry, block.get("text", ""))
        entries.append(entry)
        position += 1
    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = _safe_role(msg.get("role"))
        entry = _message_entry(position, role)
        content = msg.get("content")
        if isinstance(content, str):
            _add_text(entry, content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    _add_text(entry, block)
                    continue
                kind = block.get("type")
                _add_part_type(entry, kind)
                if kind == "text":
                    _add_text(entry, block.get("text", ""))
                elif kind == "image":
                    source = block.get("source") if isinstance(block.get("source"), dict) else {}
                    descriptor = _image_descriptor(
                        source.get("data"), mime=source.get("media_type"),
                        anchor=position, role=role)
                    images.append(descriptor)
                    entry["images"] += 1
                elif kind == "tool_use":
                    entry["tool_calls"] += 1
                    _add_tool_args(entry, block.get("input"))
                    call_id = _call_key(block.get("id"))
                    name = _safe_name(block.get("name"))
                    calls.append(call_id)
                    _add_tool_name(entry, name)
                    if call_id and name:
                        call_names[call_id] = name
                elif kind == "tool_result":
                    entry["tool_results"] += 1
                    _add_tool_result(entry, block.get("content"))
                    result_id = _call_key(block.get("tool_use_id"))
                    results.append(result_id)
                    _add_tool_name(entry, call_names.get(result_id))
        entries.append(entry)
        position += 1
    return entries, calls, results, images


def _gemini_messages(payload: dict) -> tuple[list[dict], list[str], list[str], list[dict]]:
    entries: list[dict] = []
    calls: list[str] = []
    results: list[str] = []
    images: list[dict] = []
    call_ordinals: dict[str, int] = {}
    result_ordinals: dict[str, int] = {}
    position = 0
    system = payload.get("systemInstruction")
    if isinstance(system, dict):
        entry = _message_entry(position, "system")
        for part in system.get("parts") or []:
            if isinstance(part, dict) and "text" in part:
                _add_text(entry, part.get("text", ""))
        entries.append(entry)
        position += 1
    for content in payload.get("contents") or []:
        if not isinstance(content, dict):
            continue
        raw_role = content.get("role")
        role = "assistant" if raw_role == "model" else _safe_role(raw_role)
        entry = _message_entry(position, role)
        for part_index, part in enumerate(content.get("parts") or []):
            if not isinstance(part, dict):
                _add_text(entry, part)
                continue
            if "text" in part:
                _add_text(entry, part.get("text", ""))
            inline = part.get("inline_data") or part.get("inlineData")
            if isinstance(inline, dict):
                _add_part_type(entry, "inline_data")
                descriptor = _image_descriptor(
                    inline.get("data"),
                    mime=inline.get("mime_type") or inline.get("mimeType"),
                    anchor=position, role=role)
                images.append(descriptor)
                entry["images"] += 1
            call = part.get("functionCall")
            if isinstance(call, dict):
                entry["tool_calls"] += 1
                _add_part_type(entry, "functionCall")
                _add_tool_args(entry, call.get("args"))
                _add_tool_name(entry, call.get("name"))
                call_id = _call_key(call.get("id"))
                if call_id:
                    calls.append(call_id)
                else:
                    name = _safe_name(call.get("name"))
                    ordinal = call_ordinals.get(name, 0)
                    call_ordinals[name] = ordinal + 1
                    calls.append(f"{name}:{ordinal}")
            result = part.get("functionResponse")
            if isinstance(result, dict):
                entry["tool_results"] += 1
                _add_part_type(entry, "functionResponse")
                _add_tool_result(entry, result.get("response"))
                _add_tool_name(entry, result.get("name"))
                result_id = _call_key(result.get("id"))
                if result_id:
                    results.append(result_id)
                else:
                    name = _safe_name(result.get("name"))
                    ordinal = result_ordinals.get(name, 0)
                    result_ordinals[name] = ordinal + 1
                    results.append(f"{name}:{ordinal}")
        entries.append(entry)
        position += 1
    return entries, calls, results, images


def _responses_messages(payload: dict) -> tuple[list[dict], list[str], list[str], list[dict]]:
    entries: list[dict] = []
    calls: list[str] = []
    results: list[str] = []
    images: list[dict] = []
    position = 0
    instructions = payload.get("instructions")
    if instructions:
        entry = _message_entry(position, "system")
        _add_text(entry, instructions)
        entries.append(entry)
        position += 1
    for item in payload.get("input") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "function_call":
            entry = _message_entry(position, "assistant", "function_call")
            entry["tool_calls"] = 1
            _add_part_type(entry, "function_call")
            _add_tool_args(entry, item.get("arguments"))
            _add_tool_name(entry, item.get("name"))
            calls.append(_call_key(item.get("call_id") or item.get("id")))
        elif kind == "function_call_output":
            entry = _message_entry(position, "tool", "function_call_output")
            entry["tool_results"] = 1
            _add_part_type(entry, "function_call_output")
            _add_tool_result(entry, item.get("output"))
            _add_tool_name(entry, item.get("name"))
            results.append(_call_key(item.get("call_id")))
        else:
            role = _safe_role(item.get("role"))
            entry = _message_entry(position, role, "text")
            content = item.get("content")
            if isinstance(content, str):
                _add_text(entry, content)
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        _add_text(entry, part)
                        continue
                    part_type = part.get("type")
                    _add_part_type(entry, part_type)
                    if part_type in {"input_text", "output_text", "text"}:
                        _add_text(entry, part.get("text", ""))
                    elif part_type in {"input_image", "image_url"}:
                        url = part.get("image_url") or part.get("url")
                        descriptor = _data_url_image(url, anchor=position, role=role)
                        if descriptor:
                            images.append(descriptor)
                            entry["images"] += 1
        entries.append(entry)
        position += 1
    return entries, calls, results, images


_MESSAGE_RENDERERS: dict[str, Callable[[dict], tuple[
    list[dict], list[str], list[str], list[dict]
]]] = {
    "chat_completions": _openai_messages,
    "messages": _anthropic_messages,
    "generate_content": _gemini_messages,
    "responses": _responses_messages,
}


def _rendered_messages(
    payload: dict, transport: str
) -> tuple[list[dict], list[str], list[str], list[dict]]:
    if transport == 'cloud_code_assist':
        return _gemini_messages(payload.get('request') or {})
    renderer = _MESSAGE_RENDERERS.get(transport, _openai_messages)
    return renderer(payload)


def _summarize_entries(entries: list[dict]) -> dict:
    role_counts: dict[str, int] = {}
    totals = {
        "text_chars": 0,
        "text_utf8_bytes": 0,
        "tool_argument_chars": 0,
        "tool_argument_utf8_bytes": 0,
        "tool_result_chars": 0,
        "tool_result_utf8_bytes": 0,
        "images": 0,
    }
    for entry in entries:
        role = entry["role"]
        role_counts[role] = role_counts.get(role, 0) + 1
        for key in totals:
            totals[key] += int(entry.get(key) or 0)
    return {"count": len(entries), "role_counts": role_counts, **totals}


def _schema_entry(
    name: Any,
    schema: Any,
    *,
    category: Any = "",
    rendered: Any = None,
) -> dict:
    schema = schema if isinstance(schema, dict) else {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    argument_keys = sorted(_safe_name(k) for k in properties)
    required_arguments = sorted(_safe_name(k) for k in required)
    entry = {
        "name": _safe_name(name),
        "argument_key_count": len(argument_keys),
        "argument_keys": argument_keys[:MAX_ARGUMENT_KEYS],
        "argument_keys_truncated_count": max(
            0, len(argument_keys) - MAX_ARGUMENT_KEYS),
        "required_argument_count": len(required_arguments),
        "required_arguments": required_arguments[:MAX_ARGUMENT_KEYS],
        "required_arguments_truncated_count": max(
            0, len(required_arguments) - MAX_ARGUMENT_KEYS),
    }
    safe_category = _safe_name(category)
    if safe_category:
        entry["category"] = safe_category
    if rendered is not None:
        _, rendered_bytes = _json_metrics(rendered)
        entry["rendered_schema_bytes"] = rendered_bytes
        entry["estimated_schema_tokens"] = _estimated_tokens(rendered_bytes)
    return entry


def _rendered_tools(payload: dict, transport: str) -> tuple[list[dict], list[Any]]:
    if transport == 'cloud_code_assist':
        payload, transport = payload.get('request') or {}, 'generate_content'
    entries: list[dict] = []
    canonical_items: list[Any] = []
    tools = payload.get("tools") or []
    if transport == "generate_content":
        for group in tools:
            if not isinstance(group, dict):
                continue
            declarations = group.get("functionDeclarations") or \
                group.get("function_declarations") or []
            for declaration in declarations:
                if not isinstance(declaration, dict):
                    continue
                schema = declaration.get("parameters") or {}
                entries.append(_schema_entry(
                    declaration.get("name"), schema, rendered=declaration))
                canonical_items.append(declaration)
        return entries, canonical_items
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if transport == "messages":
            name = tool.get("name")
            schema = tool.get("input_schema") or {}
            canonical = tool
        elif transport == "responses":
            name = tool.get("name")
            schema = tool.get("parameters") or {}
            canonical = tool
        else:
            fn = tool.get("function") if isinstance(tool.get("function"), dict) else {}
            name = fn.get("name") or tool.get("name") or tool.get("type")
            schema = fn.get("parameters") or tool.get("parameters") or {}
            canonical = tool
        entries.append(_schema_entry(name, schema, rendered=canonical))
        canonical_items.append(canonical)
    return entries, canonical_items


def _source_tool_entries(tools: list | None) -> list[dict]:
    out = []
    for spec in tools or []:
        if not isinstance(spec, dict):
            continue
        params = spec.get("params")
        if not isinstance(params, dict):
            params = {}
        properties = {}
        required = []
        for name, detail in params.items():
            properties[str(name)] = detail
            if isinstance(detail, dict) and detail.get("required"):
                required.append(str(name))
        out.append(_schema_entry(
            spec.get("name"),
            {"properties": properties, "required": required},
            category=spec.get("category"),
        ))
    return out


def _linkage(calls: list[str], results: list[str]) -> dict:
    call_ids = [item for item in calls if item]
    result_ids = [item for item in results if item]
    duplicate_calls = len(call_ids) - len(set(call_ids))
    call_set, result_set = set(call_ids), set(result_ids)
    return {
        "calls": len(calls),
        "results": len(results),
        "calls_with_ids": len(call_ids),
        "results_with_ids": len(result_ids),
        "duplicate_call_ids": duplicate_calls,
        "orphan_results": len(result_set - call_set),
        "unanswered_calls": len(call_set - result_set),
    }


def _generation(payload: dict, transport: str, rendered_tool_count: int) -> dict:
    if transport == 'cloud_code_assist':
        payload, transport = payload.get('request') or {}, 'generate_content'
    config = payload.get("generationConfig") if transport == "generate_content" else payload
    config = config if isinstance(config, dict) else {}
    max_output = config.get("maxOutputTokens") if transport == "generate_content" \
        else config.get("max_output_tokens", config.get("max_tokens"))
    response_format = None
    if transport == "generate_content":
        response_format = config.get("responseMimeType")
    elif transport == "responses":
        fmt = ((payload.get("text") or {}).get("format")
               if isinstance(payload.get("text"), dict) else None)
        response_format = fmt.get("type") if isinstance(fmt, dict) else None
    else:
        fmt = payload.get("response_format")
        response_format = fmt.get("type") if isinstance(fmt, dict) else None
    reasoning = payload.get("reasoning")
    reasoning_effort = payload.get("reasoning_effort")
    if isinstance(reasoning, dict):
        reasoning_effort = reasoning.get("effort", reasoning_effort)
    template = payload.get("chat_template_kwargs")
    thinking_enabled = template.get("enable_thinking") \
        if isinstance(template, dict) and "enable_thinking" in template else None
    if not isinstance(thinking_enabled, bool):
        thinking_enabled = None
    tool_choice = payload.get("tool_choice")
    if isinstance(tool_choice, dict):
        function = tool_choice.get("function")
        tool_choice = function.get("name") if isinstance(function, dict) else \
            tool_choice.get("type")
    return {
        "mode": "provider_tools" if rendered_tool_count else
                "json" if response_format else "text",
        "temperature": _safe_number(config.get("temperature")),
        "top_p": _safe_number(config.get("topP", config.get("top_p"))),
        "max_output_tokens": max(0, _safe_int(max_output)),
        "reasoning_budget": _safe_number(payload.get("reasoning_budget")),
        "reasoning_effort": _safe_name(reasoning_effort) if reasoning_effort else None,
        "thinking_enabled": thinking_enabled,
        "response_format": _safe_name(response_format) if response_format else None,
        "tool_choice": _safe_name(tool_choice) if tool_choice is not None else None,
        "stream": bool(payload.get("stream", False)),
    }
