"""Provider-neutral, request-local model conversation graph.

VARIANT-1 stores model history in an OpenAI-compatible shape because that is a
convenient internal interchange format.  Provider adapters must not interpret
that shape independently: doing so previously dropped Gemini tool history,
misplaced screenshots, mislabeled JPEGs as PNGs, and discarded Responses
images.

This module parses the interchange shape once, validates its causal links, and
renders the four wire protocols VARIANT-1 uses. Image bytes remain transient;
provider-native replay metadata stays in the durable canonical tool-call state
so a process restart cannot invalidate a resumable turn.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable


class ProjectionError(ValueError):
    """A privacy-safe structural error raised before provider I/O."""


_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+);base64,(?P<data>.*)$",
    re.DOTALL,
)
_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_.:-]+")


def _clean_id(value: Any, fallback: str) -> str:
    value = _SAFE_ID_RE.sub("_", str(value or "").strip())
    return (value or fallback)[:160]


def detect_image_media_type(data_b64: str) -> str:
    """Detect common image formats from a base64 prefix without retaining bytes."""
    raw = str(data_b64 or "").strip()
    if not raw:
        return ""
    # A short prefix is enough for every signature below.  Decode a complete
    # multiple of four so a large screenshot is never copied unnecessarily.
    prefix = raw[:128]
    prefix += "=" * ((4 - len(prefix) % 4) % 4)
    try:
        header = base64.b64decode(prefix, validate=False)
    except Exception:
        return ""
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return ""


@dataclass(frozen=True)
class ImageObservation:
    """One model-visible image plus its causal anchor.

    ``origin`` is ``current_user`` for an image attached by the current user,
    ``tool_result`` for an image produced while executing a tool,
    and ``transcript`` for an image already embedded in source messages.
    """

    data_b64: str = ""
    media_type: str = "image/png"
    origin: str = "current_user"
    tool_call_ids: tuple[str, ...] = ()
    url: str = ""
    detail: str = "high"
    declared_media_type: str = ""
    detected_media_type: str = ""

    @property
    def data_url(self) -> str:
        if self.url:
            return self.url
        return f"data:{self.media_type};base64,{self.data_b64}"


def coerce_image_observation(
    value: Any,
    *,
    origin: str = "current_user",
    tool_call_ids: Iterable[str] = (),
    media_type: str = "",
) -> ImageObservation | None:
    """Normalize legacy bare-base64 and typed observations.

    MIME is detected from the bytes.  A declared data-URL MIME is retained only
    as diagnostic metadata; the detected MIME wins so JPEG attachments can
    never be sent under a PNG label.
    """
    if value is None or value == "":
        return None
    if isinstance(value, ImageObservation):
        if value.url:
            return value
        detected = value.detected_media_type or detect_image_media_type(
            value.data_b64)
        resolved = detected or value.media_type or value.declared_media_type or "image/png"
        return replace(
            value,
            media_type=resolved,
            detected_media_type=detected,
        )
    if isinstance(value, dict):
        return coerce_image_observation(
            ImageObservation(
                data_b64=str(value.get("data_b64") or value.get("data") or ""),
                media_type=str(value.get("media_type") or media_type or "image/png"),
                origin=str(value.get("origin") or origin),
                tool_call_ids=tuple(
                    str(v) for v in (
                        value.get("tool_call_ids")
                        or ([value.get("tool_call_id")] if value.get("tool_call_id") else ())
                    )
                    if v
                ),
                url=str(value.get("url") or ""),
                detail=str(value.get("detail") or "high"),
                declared_media_type=str(value.get("declared_media_type") or ""),
                detected_media_type=str(value.get("detected_media_type") or ""),
            )
        )
    raw = str(value or "").strip()
    declared = str(media_type or "").strip().lower()
    match = _DATA_URL_RE.match(raw)
    if match:
        declared = str(match.group("mime") or declared).lower()
        raw = match.group("data")
    if raw.startswith(("http://", "https://")):
        return ImageObservation(
            url=raw,
            media_type=declared or "image/jpeg",
            origin=origin,
            tool_call_ids=tuple(str(v) for v in tool_call_ids if v),
            declared_media_type=declared,
        )
    detected = detect_image_media_type(raw)
    resolved = detected or declared or "image/png"
    return ImageObservation(
        data_b64=raw,
        media_type=resolved,
        origin=origin,
        tool_call_ids=tuple(str(v) for v in tool_call_ids if v),
        declared_media_type=declared,
        detected_media_type=detected,
    )


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ImagePart:
    observation: ImageObservation
    anchor_call_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolCallPart:
    ref: str
    source_id: str
    name: str
    arguments: dict[str, Any]
    provider_replay: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResultPart:
    call_ref: str
    source_id: str
    name: str
    output: str
    is_error: bool = False


ModelPart = TextPart | ImagePart | ToolCallPart | ToolResultPart


@dataclass(frozen=True)
class MessageNode:
    id: str
    role: str
    parts: tuple[ModelPart, ...]
    source_index: int


@dataclass(frozen=True)
class MessageGraph:
    messages: tuple[MessageNode, ...]

    @property
    def calls(self) -> tuple[ToolCallPart, ...]:
        return tuple(
            part
            for message in self.messages
            for part in message.parts
            if isinstance(part, ToolCallPart)
        )

    @property
    def results(self) -> tuple[ToolResultPart, ...]:
        return tuple(
            part
            for message in self.messages
            for part in message.parts
            if isinstance(part, ToolResultPart)
        )

    @property
    def images(self) -> tuple[ImagePart, ...]:
        return tuple(
            part
            for message in self.messages
            for part in message.parts
            if isinstance(part, ImagePart)
        )

    def call_by_ref(self) -> dict[str, ToolCallPart]:
        return {call.ref: call for call in self.calls}


def _json_object(value: Any, *, node: int, tool: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ProjectionError(
                f"message {node}: malformed arguments for tool {tool!r}"
            ) from exc
        if isinstance(decoded, dict):
            return decoded
    raise ProjectionError(
        f"message {node}: non-object arguments for tool {tool!r}"
    )


def _text_or_image_parts(content: Any, *, node: int, role: str) -> list[ModelPart]:
    if content is None:
        return []
    if isinstance(content, str):
        return [TextPart(content)]
    if not isinstance(content, list):
        raise ProjectionError(f"message {node}: unsupported {role} content")
    parts: list[ModelPart] = []
    for part_index, block in enumerate(content):
        if isinstance(block, str):
            parts.append(TextPart(block))
            continue
        if not isinstance(block, dict):
            raise ProjectionError(
                f"message {node} part {part_index}: unsupported content block"
            )
        kind = str(block.get("type") or "")
        if kind in {"text", "input_text", "output_text"}:
            parts.append(TextPart(str(block.get("text") or "")))
            continue
        if kind in {"image_url", "input_image"}:
            raw = block.get("image_url") or block.get("url")
            if isinstance(raw, dict):
                raw = raw.get("url")
            observation = coerce_image_observation(
                raw,
                origin="transcript",
                media_type=str(block.get("media_type") or ""),
            )
            if observation is None:
                raise ProjectionError(
                    f"message {node} part {part_index}: empty image"
                )
            parts.append(ImagePart(observation))
            continue
        if kind == "image":
            source = block.get("source") if isinstance(block.get("source"), dict) else {}
            raw = source.get("data") or source.get("url")
            observation = coerce_image_observation(
                raw,
                origin="transcript",
                media_type=str(source.get("media_type") or ""),
            )
            if observation is None:
                raise ProjectionError(
                    f"message {node} part {part_index}: empty image"
                )
            parts.append(ImagePart(observation))
            continue
        raise ProjectionError(
            f"message {node} part {part_index}: unsupported block type {kind!r}"
        )
    return parts


def build_message_graph(
    messages: list | None,
    image: Any = None,
) -> MessageGraph:
    """Parse history, validate links, and anchor transient image observations."""
    nodes: list[MessageNode] = []
    calls_by_ref: dict[str, ToolCallPart] = {}
    pending_by_source_id: dict[str, list[str]] = {}
    pending_refs: list[str] = []

    for source_index, raw_message in enumerate(messages or []):
        if not isinstance(raw_message, dict):
            raise ProjectionError(f"message {source_index}: expected object")
        raw_role = str(raw_message.get("role") or "").strip().lower()
        role = "system" if raw_role == "developer" else raw_role
        if role not in {"system", "user", "assistant", "tool"}:
            raise ProjectionError(
                f"message {source_index}: unsupported role {raw_role!r}"
            )
        node_id = f"m{source_index}"
        if role == "tool":
            source_id = str(
                raw_message.get("tool_call_id") or raw_message.get("id") or ""
            ).strip()
            candidates = pending_by_source_id.get(source_id, []) if source_id else []
            if len(candidates) > 1:
                raise ProjectionError(
                    f"message {source_index}: ambiguous result id {source_id!r}"
                )
            if candidates:
                call_ref = candidates[-1]
            elif not source_id and len(pending_refs) == 1:
                call_ref = pending_refs[-1]
            else:
                raise ProjectionError(
                    f"message {source_index}: orphan tool result id {source_id!r}"
                )
            call = calls_by_ref[call_ref]
            pending_refs.remove(call_ref)
            source_queue = pending_by_source_id.get(call.source_id) or []
            if call_ref in source_queue:
                source_queue.remove(call_ref)
            content = raw_message.get("content")
            if isinstance(content, str):
                output = content
            elif content is None:
                output = ""
            else:
                # Tool results are provider-neutral strings at VARIANT-1's tool
                # boundary.  Preserve structured results deterministically.
                output = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
            nodes.append(MessageNode(
                id=node_id,
                role="tool",
                parts=(ToolResultPart(
                    call_ref=call_ref,
                    source_id=call.source_id,
                    name=call.name,
                    output=output,
                    is_error=bool(raw_message.get("is_error")),
                ),),
                source_index=source_index,
            ))
            continue

        parts = _text_or_image_parts(
            raw_message.get("content"), node=source_index, role=role)
        if role == "assistant":
            for call_index, raw_call in enumerate(raw_message.get("tool_calls") or []):
                if not isinstance(raw_call, dict):
                    raise ProjectionError(
                        f"message {source_index}: invalid tool call {call_index}"
                    )
                fn = raw_call.get("function")
                if not isinstance(fn, dict):
                    raise ProjectionError(
                        f"message {source_index}: invalid function call {call_index}"
                    )
                name = str(fn.get("name") or "").strip()
                if not name:
                    raise ProjectionError(
                        f"message {source_index}: unnamed function call {call_index}"
                    )
                source_id = str(
                    raw_call.get("id") or f"call_{source_index}_{call_index}"
                ).strip()
                if pending_by_source_id.get(source_id):
                    raise ProjectionError(
                        f"message {source_index}: duplicate unresolved call id "
                        f"{source_id!r}"
                    )
                ref = f"c{source_index}_{call_index}"
                replay = raw_call.get("provider_replay")
                if not isinstance(replay, dict):
                    replay = {}
                call = ToolCallPart(
                    ref=ref,
                    source_id=source_id,
                    name=name,
                    arguments=_json_object(
                        fn.get("arguments"), node=source_index, tool=name),
                    provider_replay=dict(replay),
                )
                calls_by_ref[ref] = call
                pending_refs.append(ref)
                pending_by_source_id.setdefault(source_id, []).append(ref)
                parts.append(call)
        nodes.append(MessageNode(
            id=node_id,
            role=role,
            parts=tuple(parts),
            source_index=source_index,
        ))

    if pending_refs:
        refs = ", ".join(pending_refs[:4])
        raise ProjectionError(f"unanswered tool calls: {refs}")

    image_inputs = image if isinstance(image, (list, tuple)) else [image]
    observations = [
        observation
        for observation in (
            coerce_image_observation(item) for item in image_inputs
        )
        if observation is not None
    ]
    for observation in observations:
        if observation.origin == "tool_result":
            if not observation.tool_call_ids:
                raise ProjectionError("tool image has no call id anchor")
            result_nodes: list[tuple[int, ToolResultPart]] = []
            requested_ids = set(observation.tool_call_ids)
            for node_index, node in enumerate(nodes):
                for part in node.parts:
                    if (
                        isinstance(part, ToolResultPart)
                        and part.source_id in requested_ids
                    ):
                        result_nodes.append((node_index, part))
            found_ids = {part.source_id for _, part in result_nodes}
            missing = requested_ids - found_ids
            if missing:
                clean = ", ".join(sorted(missing)[:4])
                raise ProjectionError(f"tool image anchor not found: {clean}")
            anchor_refs = tuple(
                part.call_ref for _, part in result_nodes
                if part.source_id in requested_ids
            )
            insert_at = max(index for index, _ in result_nodes) + 1
            image_node = MessageNode(
                id=f"image_tool_{insert_at}",
                role="user",
                parts=(ImagePart(observation, anchor_call_refs=anchor_refs),),
                source_index=-1,
            )
            nodes.insert(insert_at, image_node)
        else:
            # A current-user observation belongs to the current user message.
            # This is the only backwards scan allowed: the caller explicitly
            # declared a user-origin image, rather than leaving adapters to guess.
            target = next(
                (i for i in range(len(nodes) - 1, -1, -1)
                 if nodes[i].role == "user"),
                None,
            )
            image_part = ImagePart(observation)
            if target is None:
                nodes.append(MessageNode(
                    id="image_user",
                    role="user",
                    parts=(image_part,),
                    source_index=-1,
                ))
            else:
                nodes[target] = replace(
                    nodes[target], parts=nodes[target].parts + (image_part,))

    return MessageGraph(tuple(nodes))


def _wire_call_ids(graph: MessageGraph, provider: str) -> dict[str, str]:
    used: set[str] = set()
    out: dict[str, str] = {}
    for ordinal, call in enumerate(graph.calls):
        candidate = call.source_id
        if provider == "gemini":
            gemini = call.provider_replay.get("gemini")
            if isinstance(gemini, dict) and gemini.get("id"):
                candidate = str(gemini["id"])
        candidate = _clean_id(candidate, f"call_{ordinal}")
        if candidate in used:
            # Signed Gemini calls must retain the exact provider id.  A duplicate
            # signed id cannot be repaired without corrupting provider replay.
            gemini = call.provider_replay.get("gemini")
            if (
                provider == "gemini"
                and isinstance(gemini, dict)
                and gemini.get("thought_signature")
            ):
                raise ProjectionError(
                    f"duplicate signed Gemini call id {candidate!r}")
            candidate = _clean_id(f"{candidate}__{ordinal}", f"call_{ordinal}")
        used.add(candidate)
        out[call.ref] = candidate
    return out


def _openai_content(parts: tuple[ModelPart, ...], *, role: str) -> Any:
    texts = [part.text for part in parts if isinstance(part, TextPart)]
    images = [part.observation for part in parts if isinstance(part, ImagePart)]
    if not images:
        return "".join(texts)
    if role not in {"user"}:
        raise ProjectionError(f"OpenAI chat cannot project {role} image content")
    blocks: list[dict] = [{"type": "text", "text": text} for text in texts]
    for image in images:
        image_url = {"url": image.data_url}
        if image.detail:
            image_url["detail"] = image.detail
        blocks.append({
            "type": "image_url",
            "image_url": image_url,
        })
    return blocks


def render_openai_chat(graph: MessageGraph) -> list[dict]:
    """Render Chat Completions / llama.cpp messages without added prose."""
    call_ids = _wire_call_ids(graph, "openai")
    out: list[dict] = []
    for node in graph.messages:
        calls = [p for p in node.parts if isinstance(p, ToolCallPart)]
        results = [p for p in node.parts if isinstance(p, ToolResultPart)]
        if results:
            if len(results) != 1:
                raise ProjectionError(
                    f"message {node.source_index}: multiple tool results")
            result = results[0]
            out.append({
                "role": "tool",
                "tool_call_id": call_ids[result.call_ref],
                "content": result.output,
            })
            continue
        if node.role not in {"system", "user", "assistant"}:
            raise ProjectionError(
                f"message {node.source_index}: invalid OpenAI role {node.role!r}")
        message: dict[str, Any] = {
            "role": node.role,
            "content": _openai_content(node.parts, role=node.role),
        }
        if calls:
            if node.role != "assistant":
                raise ProjectionError(
                    f"message {node.source_index}: tool call outside assistant")
            message["tool_calls"] = [{
                "id": call_ids[call.ref],
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(
                        call.arguments, ensure_ascii=False, separators=(",", ":")),
                },
            } for call in calls]
        out.append(message)
    return out


def _anthropic_image(image: ImageObservation) -> dict:
    if image.url:
        return {"type": "image", "source": {"type": "url", "url": image.url}}
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": image.media_type,
            "data": image.data_b64,
        },
    }


def _merge_provider_message(out: list[dict], role: str, blocks: list[dict]) -> None:
    if not blocks:
        return
    if out and out[-1]["role"] == role:
        existing = out[-1].get("content")
        if isinstance(existing, list):
            existing.extend(blocks)
            return
    out.append({"role": role, "content": blocks})


def render_anthropic_messages(graph: MessageGraph) -> tuple[str, list[dict]]:
    """Render Anthropic system text and Messages API conversation."""
    call_ids = _wire_call_ids(graph, "anthropic")
    system_parts: list[str] = []
    out: list[dict] = []
    for node in graph.messages:
        if node.role == "system":
            for part in node.parts:
                if not isinstance(part, TextPart):
                    raise ProjectionError(
                        f"message {node.source_index}: non-text Anthropic system part")
                system_parts.append(part.text)
            continue
        blocks: list[dict] = []
        provider_role = node.role
        if node.role == "tool":
            provider_role = "user"
        elif node.role not in {"user", "assistant"}:
            raise ProjectionError(
                f"message {node.source_index}: invalid Anthropic role {node.role!r}")
        for part in node.parts:
            if isinstance(part, TextPart):
                if part.text:
                    blocks.append({"type": "text", "text": part.text})
            elif isinstance(part, ImagePart):
                if provider_role != "user":
                    raise ProjectionError(
                        f"message {node.source_index}: Anthropic assistant image")
                blocks.append(_anthropic_image(part.observation))
            elif isinstance(part, ToolCallPart):
                if provider_role != "assistant":
                    raise ProjectionError(
                        f"message {node.source_index}: Anthropic tool call role")
                blocks.append({
                    "type": "tool_use",
                    "id": call_ids[part.ref],
                    "name": part.name,
                    "input": part.arguments,
                })
            elif isinstance(part, ToolResultPart):
                blocks.append({
                    "type": "tool_result",
                    "tool_use_id": call_ids[part.call_ref],
                    "content": part.output,
                    **({"is_error": True} if part.is_error else {}),
                })
        _merge_provider_message(out, provider_role, blocks)
    return "\n\n".join(system_parts), out


def _gemini_call_part(call: ToolCallPart, call_id: str) -> dict:
    part: dict[str, Any] = {
        "functionCall": {
            "id": call_id,
            "name": call.name,
            "args": call.arguments,
        }
    }
    gemini = call.provider_replay.get("gemini")
    if isinstance(gemini, dict):
        signature = gemini.get("thought_signature")
        if signature:
            part["thoughtSignature"] = signature
    return part


def render_gemini_generate_content(
    graph: MessageGraph,
) -> tuple[str, list[dict]]:
    """Render Gemini GenerateContent system text and causal contents."""
    call_ids = _wire_call_ids(graph, "gemini")
    calls = graph.call_by_ref()
    system_parts: list[str] = []
    out: list[dict] = []
    for node in graph.messages:
        if node.role == "system":
            for part in node.parts:
                if not isinstance(part, TextPart):
                    raise ProjectionError(
                        f"message {node.source_index}: non-text Gemini system part")
                system_parts.append(part.text)
            continue
        role = "model" if node.role == "assistant" else "user"
        parts: list[dict] = []
        for part in node.parts:
            if isinstance(part, TextPart):
                if part.text:
                    parts.append({"text": part.text})
            elif isinstance(part, ImagePart):
                if role != "user":
                    raise ProjectionError(
                        f"message {node.source_index}: Gemini model image")
                if part.observation.url:
                    raise ProjectionError(
                        f"message {node.source_index}: Gemini URL image unsupported")
                parts.append({
                    "inline_data": {
                        "mime_type": part.observation.media_type,
                        "data": part.observation.data_b64,
                    }
                })
            elif isinstance(part, ToolCallPart):
                if role != "model":
                    raise ProjectionError(
                        f"message {node.source_index}: Gemini function call role")
                parts.append(_gemini_call_part(part, call_ids[part.ref]))
            elif isinstance(part, ToolResultPart):
                call = calls[part.call_ref]
                parts.append({
                    "functionResponse": {
                        "id": call_ids[part.call_ref],
                        "name": call.name,
                        "response": {"result": part.output},
                    }
                })
        if not parts:
            continue
        # Consecutive user contents are one user turn on the Gemini wire.  Keep
        # model contents separate because signed Gemini parts must retain their
        # original response boundary.
        if role == "user" and out and out[-1].get("role") == "user":
            out[-1]["parts"].extend(parts)
        else:
            out.append({"role": role, "parts": parts})
    return "\n\n".join(system_parts), out


def render_openai_responses(
    graph: MessageGraph,
) -> tuple[str, list[dict], bool]:
    """Render Responses ``instructions`` and ordered input items."""
    call_ids = _wire_call_ids(graph, "responses")
    system_parts: list[str] = []
    items: list[dict] = []
    has_images = False
    replayed_reasoning: set[str] = set()
    for node in graph.messages:
        if node.role == "system":
            for part in node.parts:
                if not isinstance(part, TextPart):
                    raise ProjectionError(
                        f"message {node.source_index}: non-text Responses system part")
                system_parts.append(part.text)
            continue
        for part in node.parts:
            if isinstance(part, TextPart):
                if not part.text:
                    continue
                part_type = "output_text" if node.role == "assistant" else "input_text"
                items.append({
                    "type": "message",
                    "role": node.role,
                    **({"status": "completed"} if node.role == "assistant" else {}),
                    "content": [{"type": part_type, "text": part.text}],
                })
            elif isinstance(part, ImagePart):
                if node.role != "user":
                    raise ProjectionError(
                        f"message {node.source_index}: Responses image role")
                has_images = True
                items.append({
                    "type": "message",
                    "role": "user",
                    "content": [{
                        "type": "input_image",
                        "image_url": part.observation.data_url,
                        "detail": part.observation.detail,
                    }],
                })
            elif isinstance(part, ToolCallPart):
                call_id = call_ids[part.ref]
                replay = part.provider_replay.get("responses")
                reasoning_items = (
                    replay.get("reasoning_items")
                    if isinstance(replay, dict)
                    else None
                )
                for raw_reasoning in (
                    reasoning_items if isinstance(reasoning_items, list) else []
                ):
                    if not isinstance(raw_reasoning, dict):
                        continue
                    encrypted = raw_reasoning.get("encrypted_content")
                    if (
                        raw_reasoning.get("type") != "reasoning"
                        or not isinstance(encrypted, str)
                        or not encrypted
                    ):
                        continue
                    replay_id = str(raw_reasoning.get("id") or "")
                    identity = replay_id or encrypted
                    if identity in replayed_reasoning:
                        continue
                    replayed_reasoning.add(identity)
                    reasoning_item: dict[str, Any] = {
                        "type": "reasoning",
                        "encrypted_content": encrypted,
                    }
                    if replay_id:
                        reasoning_item["id"] = replay_id
                    for field in ("content", "summary"):
                        if isinstance(raw_reasoning.get(field), list):
                            reasoning_item[field] = raw_reasoning[field]
                    items.append(reasoning_item)
                item_id = (
                    str(replay.get("item_id"))
                    if isinstance(replay, dict) and replay.get("item_id")
                    else f"fc_{_clean_id(part.ref, 'call')}"
                )
                items.append({
                    "type": "function_call",
                    "id": item_id,
                    "call_id": call_id,
                    "name": part.name,
                    "arguments": json.dumps(
                        part.arguments, ensure_ascii=False, separators=(",", ":")),
                })
            elif isinstance(part, ToolResultPart):
                items.append({
                    "type": "function_call_output",
                    "call_id": call_ids[part.call_ref],
                    "output": part.output,
                })
    if not items:
        raise ProjectionError("Responses request has no model input items")
    return "\n\n".join(system_parts), items, has_images


__all__ = [
    "ImageObservation",
    "ImagePart",
    "MessageGraph",
    "MessageNode",
    "ProjectionError",
    "TextPart",
    "ToolCallPart",
    "ToolResultPart",
    "build_message_graph",
    "coerce_image_observation",
    "detect_image_media_type",
    "render_anthropic_messages",
    "render_gemini_generate_content",
    "render_openai_chat",
    "render_openai_responses",
]
