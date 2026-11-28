import json
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from llm_cloud_stream import call_gemini
from llm_router import LLMRouter
from model_providers import CredentialLease, ProviderProfile, ProviderRequestError
from model_runtime.llama_server import LocalEngineError
from reasoning_summaries import ReasoningBuffer
from speech import providers as speech


_KEY = "synthetic-gemini-header-credential"


@pytest_asyncio.fixture
async def transport(monkeypatch, tmp_path):
    router = LLMRouter({}, str(tmp_path), data_dir=str(tmp_path))
    client = httpx.AsyncClient

    def install(send):
        monkeypatch.setattr(
            httpx, "AsyncClient",
            lambda *args, **kwargs: client(*args, transport=httpx.MockTransport(send), **kwargs),
        )

    try:
        yield router, install
    finally:
        await router._manifest_bus.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["ok", "saved_query", "plugin_query", "http", "embedded", "transport", "subscription"])
async def test_gemini_chat_api_key_never_enters_request_url(transport, monkeypatch, kind):
    router, install = transport
    subscription = kind == "subscription"
    profile = router.provider_profile("google-antigravity" if subscription else "gemini")
    if kind == "saved_query":
        profile = profile.with_overrides({"auth_style": "query"})
    if kind == "plugin_query":
        router.provider_registry.register(ProviderProfile(
            name="fixture-gemini", display_name="Fixture Gemini",
            api_style="gemini", base_url=profile.base_url, auth_style="query",
        ), origin={"kind": "plugin"})
        profile = router.provider_profile("fixture-gemini")
    if subscription:
        monkeypatch.setattr(router, "_oauth_rec", lambda _name: {"project_id": "fixture-project"})

    def mock_request(request):
        assert request.url.params.get("alt") == "sse"
        assert "key" not in request.url.params and _KEY not in str(request.url)
        if subscription:
            assert request.url.path == "/v1internal:streamGenerateContent"
            assert request.headers["Authorization"] == f"Bearer {_KEY}"
            assert "x-goog-api-key" not in request.headers
            assert json.loads(request.content)["project"] == "fixture-project"
        else:
            assert request.url.path.endswith("/models/fixture:streamGenerateContent")
            assert request.headers["x-goog-api-key"] == _KEY
        if kind == "http":
            return httpx.Response(400, json={"error": {"message": f"rejected {_KEY}"}})
        if kind == "transport":
            raise httpx.ConnectError(f"fixture failure with {_KEY}")
        if kind == "embedded":
            event = {"error": {"message": f"rejected {_KEY}", "code": 429}}
        else:
            event = {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]}
        if subscription:
            event = {"response": event}
        return httpx.Response(200, text=f"data: {json.dumps(event)}\n\n")

    install(mock_request)
    stream = call_gemini(
        router, [{"role": "user", "content": "fixture"}], {"max_tokens": 32},
        _KEY, profile=profile, model="fixture",
    )
    if kind in {"ok", "saved_query", "plugin_query", "subscription"}:
        assert [part async for part in stream] == ["ok"]
    else:
        with pytest.raises(ProviderRequestError) as caught:
            _ = [part async for part in stream]
        assert _KEY not in str(caught.value)
        assert caught.value.status_code == {"http": 400, "embedded": 429, "transport": 503}[kind]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["direct", "profile", "subscription"])
@pytest.mark.parametrize("enabled", [True, False])
async def test_gemini_thoughts_and_credentials_follow_the_selected_route(transport, monkeypatch, route, enabled):
    router, install = transport
    label = "google-antigravity" if route == "subscription" else "gemini"
    profile = None if route == "direct" else router.provider_profile(label)
    monkeypatch.setattr(router, "_oauth_rec", lambda _name: {"project_id": "fixture-project"})

    def configure(payload, **_kwargs):
        payload["generationConfig"]["thinkingConfig"] = {"includeThoughts": enabled}
        return payload

    monkeypatch.setattr(router, "prepare_cloud_payload", configure)

    def mock_request(request):
        assert dict(request.url.params) == {"alt": "sse"}
        assert _KEY not in str(request.url)
        body = json.loads(request.content)
        event = {"candidates": [{"content": {"parts": [
            {"thought": True, "text": "Check the payload.", "thoughtSignature": "OPAQUE_FIXTURE"},
            {"text": "Answer"},
        ]}, "finishReason": "STOP"}]}
        if route == "subscription":
            assert request.headers["Authorization"] == f"Bearer {_KEY}"
            assert "x-goog-api-key" not in request.headers and body["project"] == "fixture-project"
            body = body["request"]
            event = {"response": event}
        else:
            assert request.headers["x-goog-api-key"] == _KEY
        assert body["generationConfig"]["thinkingConfig"]["includeThoughts"] == enabled
        return httpx.Response(200, text=f"data: {json.dumps(event)}\n\n")

    install(mock_request)
    frames = []
    sink = ReasoningBuffer(summary_sink=SimpleNamespace(summary_event=frames.append))
    stream = call_gemini(router, [], {"max_tokens": 32}, _KEY, profile=profile,
                         model="fixture", reasoning_sink=sink)
    assert [part async for part in stream] == ["Answer"]
    await sink.finish("done")
    assert [row["text"] for row in frames if row["status"] == "done"] == (["Check the payload."] if enabled else [])
    assert "OPAQUE_FIXTURE" not in json.dumps(frames)


@pytest.mark.asyncio
@pytest.mark.parametrize("reject", [False, True])
async def test_gemini_discovery_uses_a_header(transport, monkeypatch, reject):
    router, install = transport

    async def fresh(_name):
        return True

    monkeypatch.setattr(router, "ensure_oauth_fresh", fresh)
    monkeypatch.setattr(router, "_credential_leases", lambda name: [CredentialLease(name, "id", "fixture", _KEY)])

    def mock_request(request):
        assert request.url.path.endswith("/models")
        assert dict(request.url.params) == {"pageSize": "100"}
        assert request.headers["x-goog-api-key"] == _KEY
        if reject:
            return httpx.Response(403, text=f"rejected {_KEY}")
        return httpx.Response(200, json={
            "models": [{"name": "models/fixture", "supportedGenerationMethods": ["generateContent"]}],
        })

    install(mock_request)
    if reject:
        with pytest.raises(LocalEngineError, match="403") as caught:
            await router.list_cloud_models("gemini")
        assert _KEY not in str(caught.value)
    else:
        assert await router.list_cloud_models("gemini") == ["fixture"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reject", [False, True])
async def test_gemini_speech_uses_a_header(transport, monkeypatch, reject):
    router, install = transport

    async def key(*_args):
        return _KEY

    monkeypatch.setattr(speech, "_key", key)

    def mock_request(request):
        assert request.url.path.endswith("/models/fixture:generateContent")
        assert not request.url.query
        assert request.headers["x-goog-api-key"] == _KEY
        if reject:
            return httpx.Response(403, text=f"rejected {_KEY}")
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"inlineData": {"data": "AAA="}}]}}],
        })

    install(mock_request)
    if reject:
        with pytest.raises(speech.SpeechProviderError, match="HTTP 403") as caught:
            await speech._gemini_tts(router, {}, {}, "fixture", "Kore", "fixture")
        assert _KEY not in str(caught.value)
    else:
        result = await speech._gemini_tts(router, {}, {}, "fixture", "Kore", "fixture")
        assert result.mime_type == "audio/wav" and result.data.startswith(b"RIFF")
