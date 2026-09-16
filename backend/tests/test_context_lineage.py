"""Privacy and boundedness contracts for metadata-only context lineage."""

from __future__ import annotations

import json

from observability import context_lineage as lineage


SECRET = "C:/private/SENTINEL-secret-value.txt"


def test_receipt_sanitizer_keeps_only_allowlisted_metadata():
    receipt = lineage.new_receipt("main_chat_step")
    lineage.add_selection(
        receipt,
        kind="conversation_history",
        source="conversation_store",
        trust="user_supplied",
        reason="recent_tail",
        considered=20,
        kept=8,
        dropped=12,
    )
    lineage.add_item(
        receipt,
        kind="retrieved_memory",
        source="builtin_memory",
        trust="retrieved_personal",
        decision="kept",
        reason="top_k",
        relevance="high",
        rank=1,
        chars_before=120,
        chars_after=120,
        injected=SECRET,
    )
    receipt["prompt"] = SECRET
    receipt["items"][0]["path"] = SECRET

    clean = lineage.sanitize_receipt(receipt)
    encoded = json.dumps(clean, sort_keys=True)

    assert SECRET not in encoded
    assert clean["selection"] == {"considered": 20, "kept": 8, "dropped": 12}
    assert clean["items"][0]["kind"] == "retrieved_memory"
    assert clean["items"][0]["rank"] == 1
    assert "path" not in clean["items"][0]


def test_user_attachment_image_context_has_first_class_lineage():
    receipt = lineage.new_receipt("main_chat_step")
    lineage.add_item(
        receipt,
        kind="image_context",
        source="attachment",
        trust="user_supplied",
        decision="projected",
        reason="user_attachment",
        relevance="selected",
        image_count=2,
    )

    item = lineage.sanitize_receipt(receipt)["items"][0]
    assert item["kind"] == "image_context"
    assert item["source"] == "attachment"
    assert item["reason"] == "user_attachment"
    assert item["image_count"] == 2


def test_caps_preserve_aggregate_totals_and_by_kind():
    receipt = lineage.new_receipt("main_chat_step")
    for index in range(lineage.MAX_ITEMS + 7):
        lineage.add_item(
            receipt,
            kind="tool_observation",
            source="tool_result",
            trust="tool_output",
            decision="projected",
            reason="tool_execution",
            rank=index,
        )
    for _ in range(lineage.MAX_TRANSFORMS + 5):
        lineage.add_transform(
            receipt,
            kind="tool_output_clipped",
            reason="size_limit",
            affected_count=1,
        )

    clean = lineage.sanitize_receipt(receipt)
    assert len(clean["items"]) == lineage.MAX_ITEMS
    assert clean["aggregates"]["item_total"] == lineage.MAX_ITEMS + 7
    assert clean["items_truncated_count"] == 7
    assert len(clean["transforms"]) == lineage.MAX_TRANSFORMS
    assert clean["aggregates"]["transform_total"] == lineage.MAX_TRANSFORMS + 5
    assert clean["transforms_truncated_count"] == 5


def test_sidecar_attaches_without_becoming_message_content():
    receipt = lineage.new_receipt("main_chat_step")
    messages = [
        {"role": "system", "content": f"system {SECRET}"},
        {"role": "user", "content": "hello"},
    ]
    attached = lineage.attach_to_messages(messages, receipt)

    assert attached is receipt
    assert messages[0][lineage.RESERVED_MESSAGE_KEY] is receipt
    assert lineage.receipt_from_messages(messages)["schema"] == lineage.SCHEMA
    assert lineage.RESERVED_MESSAGE_KEY not in messages[0]["content"]


def test_message_metrics_count_unicode_and_native_tool_arguments():
    messages = [
        {"role": "system", "content": "é"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "function": {"name": "read_file", "arguments": '{"x":"✓"}'},
            }],
        },
    ]
    metrics = lineage.message_metrics(messages)
    assert metrics["message_count"] == 2
    assert metrics["role_counts"] == {"system": 1, "assistant": 1}
    assert metrics["utf8_bytes"] > metrics["chars"]
