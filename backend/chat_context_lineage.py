"""Metadata-only context evidence for one interactive chat model request.

This is an observability stage, not prompt construction. It records what was
selected or projected without copying prompt, memory, attachment, or image
content into the receipt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from chat_attachments import MAX_ATTACH_TEXT_TOTAL_CHARS
from observability import context_lineage


@dataclass(frozen=True)
class ChatContextEvidence:
    is_resume: bool
    conversation: list
    base_system: str
    prompt_context: Any
    composer_text: str
    model_text: str
    attachment_suffix: str
    display_attachments: list
    attachment_text: str
    memories: list
    catalog_specs: list
    disclosed_tool_specs: list
    previous_run_block: str = ""
    attachment_context: str = ""
    model_images: list | None = None
    evidence_snapshot: dict | None = None


def build_chat_context_receipt(evidence: ChatContextEvidence) -> dict:
    """Build a bounded lineage sidecar for the provider request."""
    receipt = context_lineage.new_receipt(
        "resume" if evidence.is_resume else "main_chat_step")
    if evidence.evidence_snapshot:
        receipt["stopped_evidence_snapshot"] = dict(evidence.evidence_snapshot)
    history_considered = len(evidence.conversation or [])
    # The setup stage already supplied the model-sized session projection.
    # Count that actual input, not the old public recent_convo() default.
    history_kept = history_considered
    context_lineage.add_selection(
        receipt,
        kind="conversation_history",
        source="conversation_store",
        trust="user_supplied",
        reason="resume_restore" if evidence.is_resume else "session_projection",
        considered=history_considered,
        kept=history_kept,
        dropped=max(0, history_considered - history_kept),
        relevance="selected",
    )
    context_lineage.add_item(
        receipt,
        kind="system_prompt",
        source="application",
        trust="trusted_application",
        decision="kept",
        reason="required",
        relevance="required",
        chars_before=len(evidence.base_system),
        chars_after=len(evidence.base_system),
        bytes_before=len(evidence.base_system.encode("utf-8", errors="replace")),
        bytes_after=len(evidence.base_system.encode("utf-8", errors="replace")),
    )
    for kind, source, producer, chars, trust in (
        (
            "profile", "profile_store", "prompt_profile",
            int(getattr(evidence.prompt_context, "profile_chars", 0) or 0),
            "retrieved_personal",
        ),
        (
            "retrieved_memory", "builtin_memory", "prompt_memory",
            int(getattr(
                evidence.prompt_context, "retrieved_memory_chars", 0) or 0),
            "retrieved_personal",
        ),
    ):
        if chars <= 0:
            continue
        context_lineage.add_item(
            receipt,
            kind=kind,
            source=source,
            trust=trust,
            decision="kept",
            reason="top_k" if producer == "prompt_memory" else "required",
            relevance="selected",
            producer=producer,
            chars_before=chars,
            chars_after=chars,
            bytes_before=chars,
            bytes_after=chars,
            estimator="chars_div_3",
        )
    context_lineage.add_item(
        receipt,
        kind="current_user",
        source="user",
        trust="user_supplied",
        decision="projected" if evidence.attachment_suffix else "kept",
        reason="current_turn",
        relevance="required",
        chars_before=len(evidence.composer_text),
        chars_after=len(evidence.model_text),
    )
    if evidence.attachment_suffix or evidence.display_attachments:
        attachment_count = max(1, len(evidence.display_attachments or []))
        context_lineage.add_selection(
            receipt,
            kind="attachment",
            source="attachment",
            trust="user_supplied",
            reason="user_attachment",
            considered=attachment_count,
            kept=attachment_count,
            dropped=0,
            relevance="selected",
        )
        truncated = (
            len(evidence.attachment_suffix) >= MAX_ATTACH_TEXT_TOTAL_CHARS)
        context_lineage.add_item(
            receipt,
            kind="attachment",
            source="attachment",
            trust="user_supplied",
            decision="projected",
            reason="size_limit" if truncated else "user_attachment",
            relevance="selected",
            chars_before=len(evidence.attachment_text or ""),
            chars_after=len(evidence.attachment_suffix),
            truncated=truncated,
        )
    context_lineage.add_selection(
        receipt,
        kind="retrieved_memory",
        source="builtin_memory",
        trust="retrieved_personal",
        reason="top_k",
        considered=len(evidence.memories or []),
        kept=len(evidence.memories or []),
        dropped=0,
        relevance="selected",
    )
    for rank, memory in enumerate(evidence.memories or [], start=1):
        memory_chars = len(str(memory or ""))
        context_lineage.add_item(
            receipt,
            kind="retrieved_memory",
            source="builtin_memory",
            trust="retrieved_personal",
            decision="kept",
            reason="top_k",
            relevance="selected",
            rank=rank,
            chars_before=memory_chars,
            chars_after=memory_chars,
        )
    context_lineage.add_selection(
        receipt,
        kind="tool_schema",
        source="tool_registry",
        trust="trusted_application",
        reason="tool_selection",
        considered=len(evidence.catalog_specs or []),
        kept=len(evidence.disclosed_tool_specs or []),
        dropped=max(
            0,
            len(evidence.catalog_specs or [])
            - len(evidence.disclosed_tool_specs or []),
        ),
        relevance="selected",
    )
    if evidence.previous_run_block:
        context_lineage.add_item(
            receipt,
            kind="previous_run_receipt",
            source="chat_session",
            trust="trusted_application",
            decision="kept",
            reason="user_requested_run_audit",
            relevance="selected",
            chars_before=len(evidence.previous_run_block),
            chars_after=len(evidence.previous_run_block),
        )
    if evidence.attachment_context:
        context_lineage.add_item(
            receipt,
            kind="image_context",
            source="attachment",
            trust="trusted_application",
            decision="projected",
            reason="user_attachment",
            relevance="selected",
            chars_before=len(evidence.attachment_context),
            chars_after=len(evidence.attachment_context),
        )
    if evidence.model_images:
        encoded_chars = sum(
            len(str(item.get("data_b64") or ""))
            for item in evidence.model_images
            if isinstance(item, dict)
        )
        context_lineage.add_item(
            receipt,
            kind="image_observation",
            source="attachment",
            trust="user_supplied",
            decision="projected",
            reason="user_attachment",
            relevance="selected",
            chars_before=encoded_chars,
            chars_after=encoded_chars,
            image_count=len(evidence.model_images),
        )
    return receipt
