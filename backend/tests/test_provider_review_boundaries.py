from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from llm_router import LLMRouter
from llm_stream_diagnostics import StreamDiagnostics
from tool_calling import ToolCallAccumulator
from test_cloud_stream_reliability import _StreamResponse, _client_for


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_kind", ["close", "cancel", "return"])
async def test_local_request_closes_before_gate_release(monkeypatch, exit_kind):
    import llm_local_stream as local
    import llm_profiles
    events = []
    entered = asyncio.Event()
    @asynccontextmanager
    async def gate():
        events.append("gate acquired")
        try:
            yield
        finally:
            events.append("gate released")
    async def inner(*args, **kwargs):
        events.append("request opened")
        try:
            yield "first"
            entered.set()
            await asyncio.Event().wait()
        finally:
            events.append("request closed")
    monkeypatch.setattr(local, "call_local_inner", inner)
    router = SimpleNamespace(_local_gate=gate)
    retained = local.call_local(router, [], {})
    if exit_kind == "return":
        router.stream = lambda *a, **k: retained
        # Exercise return after the stream opens. An already-stopped caller
        # correctly never acquires a gate or starts a request.
        await llm_profiles.complete(router, [], profile="internal_prose", should_stop=lambda: bool(events))
    else:
        assert await anext(retained) == "first"
        if exit_kind == "cancel":
            task = asyncio.create_task(anext(retained))
            await entered.wait()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        else:
            await retained.aclose()
    assert events == ["gate acquired", "request opened", "request closed", "gate released"]


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["delta", "item", "response", "mixed", "reasoning"])
async def test_codex_resource_limit_includes_complete_and_streamed_tool_arguments(tmp_path, monkeypatch, form):
    import llm_openai_codex_responses as codex
    monkeypatch.setattr(codex, 'MAX_RESPONSE_OUTPUT_BYTES', 48)
    item = {"type": "function_call", "id": "fc1", "call_id": "call1", "name": "ipython",
            "arguments": '{"code":"' + "x" * 200 + '"}'}
    if form == "delta":
        events = [{"type": "response.function_call_arguments.delta", "item_id": "fc1", "delta": item["arguments"]}]
    elif form == "response":
        events = []
    else:
        events = []
        if form == "mixed":
            events.append({"type": "response.output_text.delta", "delta": "hello"})
        if form == "reasoning":
            events.append({"type": "response.reasoning_summary_text.delta", "delta": "r" * 45})
            item["arguments"] = '{"code":"ok"}'
        events.append({"type": "response.output_item.done", "item": item})
    events.append({"type": "response.completed", "response": {"output": [item]}})
    response = _StreamResponse(["data: " + json.dumps(event) for event in events])
    monkeypatch.setattr(codex.httpx, "AsyncClient", _client_for(response))
    router = LLMRouter({"cloud": {"provider": "openai-codex"}}, str(tmp_path))
    tools, diagnostics = ToolCallAccumulator(), StreamDiagnostics()
    output = []
    with pytest.raises(codex.ProviderRequestError, match='resource limit'):
        async for token in codex.call_openai_codex_responses(
            router, [{"role": "user", "content": "test"}], {"max_tokens": 16}, "fake",
            model="gpt-5.3-codex-spark", tool_call_sink=tools, stream_diagnostics=diagnostics):
            output.append(token)
    assert tools.actions() == []
    assert diagnostics.finish_reason == "error:response_resource_limit"
    assert output == (["hello"] if form == "mixed" else [])


@pytest.mark.asyncio
async def test_codex_argument_snapshots_are_charged_once_and_emitted_once(tmp_path, monkeypatch):
    import llm_openai_codex_responses as codex
    args = '{"code":"ok"}'
    item = {"type": "function_call", "id": "fc1", "call_id": "call1", "name": "ipython", "arguments": args}
    events = [
        {"type": "response.output_item.added", "output_index": 0, "item": {**item, "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "item_id": "fc1", "delta": args},
        {"type": "response.function_call_arguments.done", "item_id": "fc1", "arguments": args},
        {"type": "response.output_item.done", "item": item},
        {"type": "response.completed", "response": {"output": [item]}},
    ]
    monkeypatch.setattr(codex.httpx, "AsyncClient", _client_for(_StreamResponse([
        "data: " + json.dumps(event) for event in events])))
    router = LLMRouter({"cloud": {"provider": "openai-codex"}}, str(tmp_path))
    tools, diagnostics = ToolCallAccumulator(), StreamDiagnostics()
    _ = [token async for token in codex.call_openai_codex_responses(router, [{"role": "user", "content": "test"}], {"max_tokens": 5}, "fake",
        model="gpt-5.3-codex-spark", tool_call_sink=tools, stream_diagnostics=diagnostics)]
    assert len(tools.actions()) == 1
    assert diagnostics.finish_reason == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [False, True])
async def test_late_oauth_refresh_cannot_publish_over_a_new_account(tmp_path, monkeypatch, replacement):
    import openai_codex_oauth
    router = LLMRouter({}, str(tmp_path))
    monkeypatch.setattr(router, "save_config", lambda **kwargs: True)
    router.set_oauth_tokens("openai-codex", access_token="old", refresh_token="refresh-old", expires_at=1)
    entered, release = asyncio.Event(), asyncio.Event()
    async def refresh(*args):
        entered.set()
        await release.wait()
        return SimpleNamespace(access_token="late", refresh_token="late-refresh", token_type="Bearer",
                               scope="", expires_at=9999999999)
    monkeypatch.setattr(openai_codex_oauth, "refresh", refresh)
    task = asyncio.create_task(router.ensure_oauth_fresh("openai-codex"))
    await entered.wait()
    router.clear_oauth("openai-codex")
    if replacement:
        router.set_oauth_tokens("openai-codex", access_token="new", refresh_token="new-refresh", replace=True)
    expected = dict(router._oauth_rec("openai-codex"))
    release.set()
    assert await task is False
    assert router._oauth_rec("openai-codex") == expected


@pytest.mark.asyncio
async def test_manual_endpoint_skips_discovery_and_clears_obsolete_context(tmp_path, monkeypatch):
    router = LLMRouter({}, str(tmp_path))
    monkeypatch.setattr(router, "save_config", lambda **kwargs: True)
    value = {"name": "manual", "base_url": "https://manual.invalid/v1", "model": "one",
             "context_length": 16384, "discover_models": False, "models": ["one", "two"]}
    row = router.save_custom_endpoint(value)
    def forbidden(*args, **kwargs):
        raise AssertionError("discovery must not make a network request")
    monkeypatch.setattr("llm_router.httpx.AsyncClient", forbidden)
    assert (await router.validate_custom_endpoint(value))["discovery_skipped"] is True
    assert await router.list_cloud_models(row["id"]) == ["one", "two"]
    router.save_custom_endpoint({**row, "model": "two", "context_length": 0})
    assert not any(key.startswith(row["id"] + "/") for key in router.cfg["cloud"]["context_windows"])


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["service", "inference", "legacy"])
async def test_search_uses_the_endpoint_belonging_to_its_credential(monkeypatch, source):
    from web_search import providers
    from service_credentials import credential_key
    dedicated = SimpleNamespace(secret="dedicated", base_url="")
    shared = SimpleNamespace(secret="shared", base_url="https://shared.invalid/v1")
    router = SimpleNamespace(
        credential_pools=SimpleNamespace(leases=lambda name: [dedicated] if source == "service" else []),
        ensure_oauth_fresh=AsyncMock(), _credential_leases=lambda name: [shared],
        provider_base_url=lambda name, lease: lease.base_url)
    captured = {}
    async def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return {"citations": ["https://example.test"], "output_text": "result"}
    monkeypatch.setattr(providers, "_post_json", post)
    await providers._xai("test", 1, {"xai": {"api_key": "legacy"}} if source == "legacy" else {}, router)
    expected_key = {"service": "dedicated", "inference": "shared", "legacy": "legacy"}[source]
    expected_base = "https://shared.invalid/v1" if source == "inference" else "https://api.x.ai/v1"
    assert captured["url"] == expected_base + "/responses"
    assert captured["headers"]["Authorization"] == "Bearer " + expected_key


@pytest.mark.asyncio
async def test_openrouter_high_uses_existing_session_route_and_native_reasoning_object(monkeypatch):
    import llm_cloud_stream as cloud
    from test_cloud_stream_reliability import _router, _capturing_client_for
    router, captured = _router(), {}
    response = _StreamResponse(["data: " + json.dumps({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})])
    monkeypatch.setattr(cloud.httpx, "AsyncClient", _capturing_client_for(response, captured))
    with router.bind_model_route({"mode": "cloud", "provider": "openrouter", "model": "minimax/minimax-m3:free", "reasoning_effort": "high"}):
        _ = [token async for token in cloud.call_openai(router, [{"role": "user", "content": "test"}],
            {"max_tokens": 16}, "fake", label="openrouter", model="minimax/minimax-m3:free",
            profile=router.provider_profile("openrouter"))]
    assert captured["payload"]["reasoning"] == {"effort": "high"}
    assert "reasoning_effort" not in captured["payload"]


@pytest.mark.asyncio
async def test_image_rejection_does_not_cool_down_or_rotate_valid_credentials(monkeypatch):
    import llm_cloud_stream as cloud
    from model_providers import CredentialLease, ProviderRequestError
    from test_cloud_stream_reliability import _router
    router = _router()
    lease = CredentialLease("openai", "one", "one", "fake", source="environment")
    router._credential_leases = lambda provider: [lease, lease]
    failure = ProviderRequestError("openai", "This model does not support image input", status_code=400)
    calls = []
    async def reject(*args, **kwargs):
        calls.append(True)
        raise failure
        yield ""
    monkeypatch.setattr(cloud, "call_cloud_once", reject)
    monkeypatch.setattr(router.credential_pools, "mark_failure", lambda *a, **k: pytest.fail("healthy credential was penalized"))
    with pytest.raises(ProviderRequestError) as caught:
        from session_catalog.service import IPYTHON_PROVIDER_SPEC
        _ = [token async for token in cloud.call_cloud(router, [{"role": "user", "content": "test"}], {"max_tokens": 10}, image_b64="fake", tools=[IPYTHON_PROVIDER_SPEC])]
    assert caught.value is failure and failure.failure_kind == "unsupported_modality"
    assert len(calls) == 1


def test_image_words_in_a_quota_error_are_not_a_modality_rejection():
    from model_providers import ProviderRequestError
    from model_runtime.image_fallback import looks_like_image_rejection
    assert not looks_like_image_rejection(ProviderRequestError("test", "image input quota rejected", status_code=429))
