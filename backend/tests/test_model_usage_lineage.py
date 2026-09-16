"""Provider usage correlation for privacy-safe model request receipts."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from observability import context_lineage
from llm_router import LLMRouter
from llm_manifest_bus import ModelRequestManifestBus
from llm_usage import normalize_manifest_usage
from model_runtime.request_manifest import (
    build_model_request_manifest,
    model_request_event_hooks,
)


@pytest.mark.parametrize('field', ['input_tokens_details','prompt_tokens_details'])
@pytest.mark.parametrize('written', [0, 25])
def test_inclusive_provider_nested_cache_writes_are_reported_without_double_counting(field, written):
    usage = normalize_manifest_usage('openai-codex',raw_usage={
        'input_tokens':100,'output_tokens':8,'total_tokens':108,
        field:{'cached_tokens':40,'cache_write_tokens':written},
    })
    assert usage['cache_write_input_tokens'] == written
    assert usage['prompt_token_volume'] == 100
    assert usage['uncached_input_tokens'] == 60
    assert usage['token_volume'] == 108
    missing = normalize_manifest_usage('openai-codex',raw_usage={
        'input_tokens':100,'output_tokens':8,'total_tokens':108,
    })
    assert missing['cache_write_input_tokens'] is None


def _manifest() -> dict:
    return build_model_request_manifest(
        provider="openai",
        api_style="openai",
        transport="chat_completions",
        adapter="test",
        adapter_version="1",
        model="test-model",
        payload={
            "model": "test-model",
            "messages": [{"role": "user", "content": "safe request"}],
            "stream": True,
        },
        source_messages=[{"role": "user", "content": "safe request"}],
    )


@pytest.mark.asyncio
async def test_event_hooks_are_dict_compatible_and_expose_exact_emitted_ref():
    captured = []

    class Router:
        async def _record_model_request_manifest(self, manifest):
            captured.append(manifest)

    hooks = model_request_event_hooks(
        Router(),
        provider="openai",
        api_style="openai",
        transport="chat_completions",
        adapter="test",
        adapter_version="1",
        model="test-model",
        payload={"messages": [{"role": "user", "content": "hello"}]},
        source_messages=[{"role": "user", "content": "hello"}],
    )

    assert isinstance(hooks, dict)
    assert hooks.request_ref == {"manifest_id": ""}
    await hooks["request"][0](SimpleNamespace(content=b'{"messages":[]}'))

    assert len(captured) == 1
    assert hooks.request_ref == {"manifest_id": captured[0]["manifest_id"]}


@pytest.mark.parametrize(
    ("provider", "raw_usage", "expected"),
    [
        (
            "openai",
            {
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "total_tokens": 150,
                "prompt_tokens_details": {"cached_tokens": 40},
                "completion_tokens_details": {"reasoning_tokens": 12},
            },
            {
                "input_tokens": 120,
                "output_tokens": 30,
                "total_tokens": 150,
                "cached_input_tokens": 40,
                "reasoning_tokens": 12,
                "cache_write_input_tokens": None,
                "tool_prompt_tokens": None,
                "prompt_token_volume": 120,
                "uncached_input_tokens": 80,
                "cache_share": 0.33333333,
                "token_volume": 150,
            },
        ),
        (
            "xai",
            {
                "input_tokens": 90,
                "output_tokens": 10,
                "total_tokens": 100,
                "input_tokens_details": {"cached_tokens": 25},
                "output_tokens_details": {"reasoning_tokens": 4},
            },
            {
                "input_tokens": 90,
                "output_tokens": 10,
                "total_tokens": 100,
                "cached_input_tokens": 25,
                "reasoning_tokens": 4,
                "cache_write_input_tokens": None,
                "tool_prompt_tokens": None,
                "prompt_token_volume": 90,
                "uncached_input_tokens": 65,
                "cache_share": 0.27777778,
                "token_volume": 100,
            },
        ),
        (
            "anthropic",
            {
                "input_tokens": 80,
                "output_tokens": 20,
                "cache_read_input_tokens": 35,
                "cache_creation_input_tokens": 15,
            },
            {
                "input_tokens": 80,
                "output_tokens": 20,
                "total_tokens": 100,
                "cached_input_tokens": 35,
                "reasoning_tokens": None,
                "cache_write_input_tokens": 15,
                "tool_prompt_tokens": None,
                "prompt_token_volume": 130,
                "uncached_input_tokens": 95,
                "cache_share": 0.26923077,
                "token_volume": 150,
            },
        ),
        (
            "gemini",
            {
                "promptTokenCount": 70,
                "candidatesTokenCount": 18,
                "totalTokenCount": 100,
                "cachedContentTokenCount": 30,
                "thoughtsTokenCount": 9,
                "toolUsePromptTokenCount": 3,
            },
            {
                "input_tokens": 70,
                "output_tokens": 18,
                "total_tokens": 100,
                "cached_input_tokens": 30,
                "reasoning_tokens": 9,
                "cache_write_input_tokens": None,
                "tool_prompt_tokens": 3,
                "prompt_token_volume": 70,
                "uncached_input_tokens": 40,
                "cache_share": 0.42857143,
                "token_volume": 100,
            },
        ),
    ],
)
def test_provider_usage_is_normalized_to_allowlisted_fields(
    provider, raw_usage, expected,
):
    normalized = normalize_manifest_usage(
        provider, raw_usage=raw_usage)

    assert normalized["measurement"] == "provider_reported"
    assert normalized["provider_reported"] is True
    assert normalized["estimated"] is False
    for key, value in expected.items():
        assert normalized[key] == value


def test_missing_provider_usage_marks_fallback_estimates():
    normalized = normalize_manifest_usage(
        "local",
        prompt_tokens=24,
        completion_tokens=7,
        raw_usage={},
    )

    assert normalized == {
        "measurement": "estimated",
        "provider_reported": False,
        "estimated": True,
        "input_tokens": 24,
        "output_tokens": 7,
        "total_tokens": 31,
        "cached_input_tokens": None,
        "reasoning_tokens": None,
        "cache_write_input_tokens": None,
        "tool_prompt_tokens": None,
        "prompt_token_volume": 24,
        "uncached_input_tokens": 24,
        "cache_share": 0.0,
        "token_volume": 31,
    }


@pytest.mark.asyncio
async def test_manifest_usage_totals_outlive_bounded_receipt_window():
    bus = ModelRequestManifestBus(maxlen=2)
    for index in range(3):
        manifest = _manifest()
        manifest["manifest_id"] = f"mreq-{index}"
        await bus.record(manifest)
        bus.patch_usage(manifest["manifest_id"], {
            "provider_reported": True,
            "estimated": False,
            "input_tokens": 100 + index,
            "output_tokens": 10,
            "total_tokens": 110 + index,
            "token_volume": 110 + index,
            "prompt_token_volume": 100 + index,
            "cached_input_tokens": 50,
            "uncached_input_tokens": 50 + index,
            "reasoning_tokens": 4,
        })

    snapshot = bus.snapshot()
    assert len(snapshot["items"]) == 2
    assert snapshot["usage_scope"] == "router_lifetime"
    assert snapshot["usage_totals"]["calls"] == 3
    assert snapshot["usage_totals"]["prompt_token_volume"] == 303
    assert snapshot["usage_totals"]["token_volume"] == 333
    assert snapshot["usage_totals"]["cached_input_tokens"] == 150
    assert snapshot["usage_totals"]["uncached_input_tokens"] == 153
    assert snapshot["usage_totals"]["reasoning_tokens"] == 12
    assert snapshot["usage_totals"]["cache_share"] == round(150 / 303, 8)


def test_manifest_flags_observation_and_supersession_lineage():
    receipt = context_lineage.new_receipt("main_chat_step")
    context_lineage.add_selection(
        receipt,
        kind="conversation_history",
        source="conversation_store",
        trust="user_supplied",
        reason="recent_tail",
        considered=10,
        kept=4,
    )
    context_lineage.add_item(
        receipt,
        kind="tool_observation",
        source="tool_result",
        trust="tool_output",
        decision="projected",
        reason="tool_execution",
    )
    context_lineage.add_transform(
        receipt,
        kind="provider_projection",
        reason="provider_projection",
        input_count=2,
        output_count=2,
        truncated=True,
    )
    context_lineage.add_transform(
        receipt,
        kind="image_superseded",
        reason="newer_observation",
        affected_count=1,
    )
    messages = [{
        "role": "user",
        "content": "hello",
        context_lineage.RESERVED_MESSAGE_KEY: receipt,
    }]
    manifest = build_model_request_manifest(
        provider="openai",
        api_style="openai",
        transport="chat_completions",
        adapter="test",
        adapter_version="1",
        model="test-model",
        payload={"messages": [{"role": "user", "content": "hello"}]},
        source_messages=messages,
    )

    assert manifest["provenance"]["selection_available"] is True
    assert manifest["provenance"]["observation_projection_available"] is True
    assert manifest["provenance"]["supersession_available"] is True
    assert manifest["provenance"]["usage_available"] is False


def test_manifest_copies_only_sanitized_context_and_compression_lineage():
    receipt = context_lineage.new_receipt("main_chat_step")
    context_lineage.add_transform(
        receipt,
        kind="context_compression",
        reason="token_threshold",
        input_count=9,
        output_count=3,
    )
    receipt["private_note"] = "must-not-survive"
    messages = [{
        "role": "user",
        "content": "hello",
        context_lineage.RESERVED_MESSAGE_KEY: receipt,
    }]
    manifest = build_model_request_manifest(
        provider="openai",
        api_style="openai",
        transport="chat_completions",
        adapter="test",
        adapter_version="1",
        model="test-model",
        payload={"messages": [{"role": "user", "content": "hello"}]},
        source_messages=messages,
    )

    assert manifest["provenance"]["context_receipt_available"] is True
    assert manifest["provenance"]["compression_receipt_available"] is True
    assert manifest["provenance"]["usage_available"] is False
    assert manifest["usage"] is None
    assert manifest["context_lineage"]["purpose"] == "main_chat_step"
    assert manifest["context_lineage"]["transforms"][0]["input_count"] == 9
    assert "must-not-survive" not in json.dumps(manifest)


def test_manifest_has_stable_empty_lineage_when_no_sidecar_exists():
    manifest = _manifest()

    assert manifest["context_lineage"]["schema"] == "variant1.context-lineage.v1"
    assert manifest["context_lineage"]["items"] == []
    assert manifest["provenance"]["context_receipt_available"] is False
    assert manifest["provenance"]["compression_receipt_available"] is False
    assert manifest["provenance"]["observation_projection_available"] is False
    assert manifest["provenance"]["supersession_available"] is False
    assert manifest["provenance"]["usage_available"] is False
    assert manifest["usage"] is None


@pytest.mark.asyncio
async def test_usage_patches_exact_manifest_and_republishes_without_raw_data(
    tmp_path,
):
    router = LLMRouter(
        {"mode": "local", "local": {}, "sampling": {}},
        str(tmp_path),
    )
    published = []
    updated = asyncio.Event()

    def sink(item):
        published.append(item)
        if isinstance(item.get("usage"), dict):
            updated.set()

    router.set_model_request_manifest_sink(sink)
    manifest = _manifest()
    await router._record_model_request_manifest(manifest)

    secret = "must-not-enter-the-receipt"
    router._record_usage(
        "openai",
        11,
        4,
        15,
        model="test-model",
        raw_usage={
            "prompt_tokens": 11,
            "completion_tokens": 4,
            "total_tokens": 15,
            "prompt_tokens_details": {"cached_tokens": 3},
            "id": secret,
            "response_text": secret,
            "error": secret,
            "headers": {"authorization": secret},
        },
        manifest_ref={"manifest_id": manifest["manifest_id"]},
    )

    await asyncio.wait_for(updated.wait(), timeout=1.0)
    snapshot = router.model_request_manifest_snapshot()
    assert len(snapshot["items"]) == 1
    stored = snapshot["items"][0]
    assert stored["manifest_id"] == manifest["manifest_id"]
    assert stored["usage"]["cached_input_tokens"] == 3
    assert stored["provenance"]["usage_available"] is True
    assert set(stored["usage"]) == {
        "call_category",
        "measurement",
        "provider_reported",
        "estimated",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_input_tokens",
        "reasoning_tokens",
        "cache_write_input_tokens",
        "tool_prompt_tokens",
        "prompt_token_volume",
        "uncached_input_tokens",
        "cache_share",
        "token_volume",
        "cost_usd",
    }
    assert snapshot["usage_totals"]["calls"] == 1
    assert snapshot["usage_totals"]["prompt_token_volume"] == 11
    assert snapshot["usage_totals"]["cached_input_tokens"] == 3
    assert snapshot["usage_totals"]["uncached_input_tokens"] == 8
    assert published[-1]["manifest_id"] == manifest["manifest_id"]
    assert published[-1]["usage"] == stored["usage"]
    assert secret not in json.dumps(stored)

    filtered = router.model_request_manifest_snapshot(
        manifest_id=manifest["manifest_id"])
    assert len(filtered["items"]) == 1
    assert filtered["filter_manifest_id"] == manifest["manifest_id"]
    missing = router.model_request_manifest_snapshot(manifest_id="mreq_missing")
    assert missing["items"] == []

    await router.stop()
