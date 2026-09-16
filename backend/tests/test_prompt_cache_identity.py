"""Provider-neutral durable-chat prompt-cache identity."""

from __future__ import annotations

import sys
import pytest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from llm_cloud_stream import _cached_prompt_tokens
from model_runtime.prompt_cache import (
    apply_prompt_cache_identity,
    resolve_prompt_cache_identity,
)
from run_context import Variant1RunContext, bind_run_context


def test_cached_prompt_tokens_from_details_and_top_level():
    assert _cached_prompt_tokens({
        "prompt_tokens_details": {"cached_tokens": 42},
    }) == 42
    assert _cached_prompt_tokens({"cached_tokens": 7}) == 7
    assert _cached_prompt_tokens({}) == 0
    assert _cached_prompt_tokens(None) == 0


def test_durable_chat_identity_is_stable_across_runs_and_routes():
    first = Variant1RunContext.create(
        source="chat",
        work_scope={"chat_id": "durable-chat-7"},
        session_id="window-a",
        run_id="run-a",
    )
    second = Variant1RunContext.create(
        source="chat",
        work_scope={"chat_id": "durable-chat-7"},
        session_id="window-b",
        run_id="run-b",
    )
    with bind_run_context(first):
        first_identity = resolve_prompt_cache_identity()
    with bind_run_context(second):
        second_identity = resolve_prompt_cache_identity()

    assert first_identity == second_identity
    assert first_identity.scope == "durable_chat"
    assert first_identity.source == "work_scope.chat_id"
    assert first_identity.key.startswith("variant1-pc-v1-")
    assert "durable-chat-7" not in first_identity.key

    metadata_only = Variant1RunContext.create(
        source="chat",
        metadata={"chat_id": "durable-chat-7"},
        run_id="run-c",
    )
    with bind_run_context(metadata_only):
        metadata_identity = resolve_prompt_cache_identity()
    assert metadata_identity.key == first_identity.key
    assert metadata_identity.scope == first_identity.scope
    assert metadata_identity.source == "metadata.chat_id"


def test_different_chat_and_explicit_owners_do_not_collide():
    a = resolve_prompt_cache_identity("chat-a")
    b = resolve_prompt_cache_identity("chat-b")
    assert a.key != b.key
    assert a.scope == b.scope == "explicit"


def test_declarative_wire_projection_preserves_one_identity():
    identity = resolve_prompt_cache_identity("durable-owner")
    payload = {"model": "test"}
    headers: dict[str, str] = {}
    body_receipt = apply_prompt_cache_identity(
        identity, payload=payload, headers=headers,
        body_field="prompt_cache_key",
    )
    assert payload["prompt_cache_key"] == identity.key
    assert body_receipt["application"] == "body.prompt_cache_key"
    assert body_receipt["native_key_sent"] is True

    payload2 = {"model": "test"}
    headers2: dict[str, str] = {}
    header_receipt = apply_prompt_cache_identity(
        identity, payload=payload2, headers=headers2,
        header_name="x-grok-conv-id",
    )
    assert headers2["x-grok-conv-id"] == identity.key
    assert header_receipt["application"] == "header.x-grok-conv-id"


def test_provider_without_native_key_keeps_identity_for_prefix_cache():
    identity = resolve_prompt_cache_identity("durable-owner")
    receipt = apply_prompt_cache_identity(
        identity,
        payload={},
        headers={},
        fallback_application="local_server_prefix_cache",
        cache_enabled=True,
    )
    assert receipt == {
        "identity_available": True,
        "key_id": identity.key,
        "scope": "explicit",
        "source": "explicit",
        "application": "local_server_prefix_cache",
        "native_key_sent": False,
        "cache_enabled": True,
    }


def test_declared_body_and_header_receive_the_same_opaque_identity():
    identity = resolve_prompt_cache_identity('private-local-chat')
    payload, headers = {}, {}
    receipt = apply_prompt_cache_identity(
        identity, payload=payload, headers=headers,
        body_field='prompt_cache_key', header_name='x-client-request-id',
    )
    assert payload['prompt_cache_key'] == headers['x-client-request-id'] == identity.key
    assert len(identity.key) <= 64 and 'private-local-chat' not in identity.key
    assert receipt['application'] == 'body.prompt_cache_key+header.x-client-request-id'
    assert receipt['native_key_sent'] is True
    assert 'session_id' not in headers
    assert 'prompt_cache_retention' not in payload


def test_invalid_second_cache_projection_does_not_partially_modify_request():
    identity = resolve_prompt_cache_identity('owner')
    payload, headers = {}, {}
    with pytest.raises(ValueError, match='header declaration'):
        apply_prompt_cache_identity(identity,payload=payload,headers=headers,
                                    body_field='prompt_cache_key',header_name='invalid\nheader')
    assert payload == {} and headers == {}
