"""Golden contracts for the provider-neutral model conversation graph."""

from __future__ import annotations

import base64

import pytest

from model_runtime.message_graph import (
    ImageObservation,
    ProjectionError,
    build_message_graph,
    coerce_image_observation,
    render_anthropic_messages,
    render_gemini_generate_content,
    render_openai_chat,
    render_openai_responses,
)


_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNg"
    "YAAAAAMAASsJTYQAAAAASUVORK5CYII="
)
_JPEG = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 16).decode()


def _tool_history(*, later_user: bool = True):
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "inspect"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_7",
                "type": "function",
                "function": {
                    "name": "computer",
                    "arguments": '{"action":"click","x":10,"y":20}',
                },
            }],
        },
        {"role": "tool", "tool_call_id": "call_7", "content": "clicked"},
    ]
    if later_user:
        messages.append({
            "role": "user",
            "content": "Additional tools now available: read_file",
        })
    return messages


def _tool_image(data: str = _PNG):
    return ImageObservation(
        data_b64=data,
        media_type="image/png",
        detected_media_type="image/png",
        origin="tool_result",
        tool_call_ids=("call_7",),
    )


def test_tool_image_stays_before_later_user_state_across_renderers():
    graph = build_message_graph(_tool_history(), _tool_image())

    openai = render_openai_chat(graph)
    result_index = next(i for i, m in enumerate(openai) if m["role"] == "tool")
    image_index = next(
        i for i, m in enumerate(openai)
        if isinstance(m.get("content"), list)
        and any(p.get("type") == "image_url" for p in m["content"])
    )
    later_index = next(
        i for i, m in enumerate(openai)
        if m.get("content") == "Additional tools now available: read_file"
    )
    assert result_index < image_index < later_index

    _, anthropic = render_anthropic_messages(graph)
    user_blocks = [
        block
        for message in anthropic
        if message["role"] == "user"
        for block in message["content"]
    ]
    kinds = [block["type"] for block in user_blocks]
    tool_result_pos = kinds.index("tool_result")
    image_pos = kinds.index("image", tool_result_pos)
    later_pos = next(
        i for i, block in enumerate(user_blocks)
        if block.get("text") == "Additional tools now available: read_file"
    )
    assert tool_result_pos < image_pos < later_pos

    _, gemini = render_gemini_generate_content(graph)
    flat = [
        part
        for content in gemini
        for part in content["parts"]
    ]
    response_pos = next(i for i, p in enumerate(flat) if "functionResponse" in p)
    inline_pos = next(i for i, p in enumerate(flat) if "inline_data" in p)
    later_pos = next(
        i for i, p in enumerate(flat)
        if p.get("text") == "Additional tools now available: read_file"
    )
    assert response_pos < inline_pos < later_pos

    _, responses, has_images = render_openai_responses(graph)
    result_pos = next(
        i for i, item in enumerate(responses)
        if item["type"] == "function_call_output"
    )
    image_pos = next(
        i for i, item in enumerate(responses)
        if item["type"] == "message"
        and item["content"][0]["type"] == "input_image"
    )
    later_pos = next(
        i for i, item in enumerate(responses)
        if item["type"] == "message"
        and item["content"][0].get("text")
        == "Additional tools now available: read_file"
    )
    assert has_images is True
    assert result_pos < image_pos < later_pos


def test_gemini_preserves_native_call_result_and_thought_signature():
    history = _tool_history(later_user=False)
    history[2]["tool_calls"][0]["provider_replay"] = {
        "gemini": {
            "id": "provider-call-abc",
            "thought_signature": "opaque-signature",
        }
    }
    graph = build_message_graph(history)
    _, contents = render_gemini_generate_content(graph)

    call = next(
        part
        for content in contents
        for part in content["parts"]
        if "functionCall" in part
    )
    result = next(
        part
        for content in contents
        for part in content["parts"]
        if "functionResponse" in part
    )
    assert call["functionCall"]["id"] == "provider-call-abc"
    assert call["thoughtSignature"] == "opaque-signature"
    assert result["functionResponse"]["id"] == "provider-call-abc"


def test_repeated_legacy_call_ids_are_repaired_without_breaking_linkage():
    history = _tool_history(later_user=False)
    history.extend([
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_7",
                "type": "function",
                "function": {"name": "computer", "arguments": '{"action":"screenshot"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "call_7", "content": "captured"},
    ])
    graph = build_message_graph(history)
    rendered = render_openai_chat(graph)
    call_ids = [
        call["id"]
        for message in rendered
        for call in message.get("tool_calls", [])
    ]
    result_ids = [
        message["tool_call_id"]
        for message in rendered
        if message["role"] == "tool"
    ]
    assert len(call_ids) == len(set(call_ids)) == 2
    assert result_ids == call_ids


def test_jpeg_magic_bytes_override_wrong_png_declaration_for_every_provider():
    observation = coerce_image_observation(
        f"data:image/png;base64,{_JPEG}")
    assert observation is not None
    assert observation.declared_media_type == "image/png"
    assert observation.detected_media_type == "image/jpeg"
    assert observation.media_type == "image/jpeg"

    graph = build_message_graph(
        [{"role": "user", "content": "look"}], observation)
    openai = render_openai_chat(graph)
    assert "data:image/jpeg;base64," in openai[0]["content"][-1]["image_url"]["url"]

    _, anthropic = render_anthropic_messages(graph)
    image = next(
        b for b in anthropic[0]["content"] if b["type"] == "image")
    assert image["source"]["media_type"] == "image/jpeg"

    _, gemini = render_gemini_generate_content(graph)
    inline = next(
        p["inline_data"]
        for p in gemini[0]["parts"]
        if "inline_data" in p
    )
    assert inline["mime_type"] == "image/jpeg"

    _, responses, _ = render_openai_responses(graph)
    image_url = next(
        p["image_url"]
        for item in responses
        for p in item.get("content", [])
        if p.get("type") == "input_image"
    )
    assert image_url.startswith("data:image/jpeg;base64,")


def test_multiple_current_user_images_render_on_every_provider():
    graph = build_message_graph(
        [{"role": "user", "content": "compare these"}],
        [
            ImageObservation(data_b64=_PNG, media_type="image/png", detail="high"),
            ImageObservation(data_b64=_JPEG, media_type="image/jpeg", detail="high"),
        ],
    )
    assert len(graph.images) == 2

    openai = render_openai_chat(graph)
    image_blocks = [
        block for block in openai[0]["content"]
        if block.get("type") == "image_url"
    ]
    assert len(image_blocks) == 2
    assert all(block["image_url"]["detail"] == "high" for block in image_blocks)

    _, anthropic = render_anthropic_messages(graph)
    assert sum(block["type"] == "image" for block in anthropic[0]["content"]) == 2

    _, gemini = render_gemini_generate_content(graph)
    assert sum("inline_data" in part for part in gemini[0]["parts"]) == 2

    _, responses, has_images = render_openai_responses(graph)
    assert has_images is True
    assert sum(
        part.get("type") == "input_image"
        for item in responses
        for part in item.get("content", [])
    ) == 2


def test_missing_tool_image_anchor_fails_without_relocation():
    observation = ImageObservation(
        data_b64=_PNG,
        origin="tool_result",
        tool_call_ids=("missing-call",),
    )
    with pytest.raises(ProjectionError, match="anchor not found"):
        build_message_graph(_tool_history(), observation)


def test_orphan_tool_result_fails_structurally():
    with pytest.raises(ProjectionError, match="orphan tool result"):
        build_message_graph([
            {"role": "user", "content": "go"},
            {"role": "tool", "tool_call_id": "missing", "content": "no"},
        ])


def test_renderers_add_no_model_facing_text():
    source = _tool_history()
    source_text = [
        message["content"]
        for message in source
        if isinstance(message.get("content"), str)
    ]
    graph = build_message_graph(source, _tool_image())

    openai_text = [
        message["content"]
        for message in render_openai_chat(graph)
        if isinstance(message.get("content"), str)
    ]
    assert all(text in source_text for text in openai_text)

    system, gemini = render_gemini_generate_content(graph)
    gemini_text = [system] + [
        part["text"]
        for content in gemini
        for part in content["parts"]
        if "text" in part
    ]
    assert all(text in source_text for text in gemini_text)
