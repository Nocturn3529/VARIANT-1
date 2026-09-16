from types import SimpleNamespace

import pytest

from speech import xai as xai_speech


class FakePools:
    def __init__(self):
        self.successes = []
        self.failures = []

    def mark_success(self, lease):
        self.successes.append(lease)

    def mark_failure(self, lease, **fields):
        self.failures.append((lease, fields))


class FakeRouter:
    def __init__(self):
        self.lease = SimpleNamespace(secret="test-secret", source="oauth", label="Subscription")
        self.credential_pools = FakePools()
        self.refreshed = []
        self.base_calls = []

    def has_cloud_key(self, provider):
        return provider == "xai"

    async def ensure_oauth_fresh(self, provider):
        self.refreshed.append(provider)
        return True

    def _credential_leases(self, provider):
        return [self.lease] if provider == "xai" else []

    def provider_base_url(self, provider, lease):
        self.base_calls.append((provider, lease))
        return "https://api.x.ai/v1"


class FakeResponse:
    def __init__(self, status=200, *, body=None, content=b""):
        self.status_code = status
        self._body = body if body is not None else {}
        self.content = content
        self.text = str(self._body)

    def json(self):
        return self._body


class FakeClient:
    responses = []
    calls = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def fake_http(monkeypatch):
    FakeClient.responses = []
    FakeClient.calls = []
    monkeypatch.setattr(xai_speech.httpx, "AsyncClient", FakeClient)


@pytest.mark.asyncio
async def test_cloud_tts_reuses_router_lease_and_requests_wav():
    router = FakeRouter()
    FakeClient.responses = [FakeResponse(content=b"RIFF-test")]

    audio = await xai_speech.synthesize(
        router, "hello", voice="eve", language="auto", speed=1.2)

    assert audio == b"RIFF-test"
    method, url, kwargs = FakeClient.calls[0]
    assert (method, url) == ("POST", "https://api.x.ai/v1/tts")
    assert kwargs["headers"]["Authorization"] == "Bearer test-secret"
    assert kwargs["json"]["output_format"] == {"codec": "wav", "sample_rate": 24000}
    assert kwargs["json"]["speed"] == 1.2
    assert router.refreshed == ["xai"]
    assert router.base_calls == [("xai", router.lease)]
    assert router.credential_pools.successes == [router.lease]


@pytest.mark.asyncio
async def test_cloud_stt_uploads_audio_only_on_explicit_call():
    router = FakeRouter()
    FakeClient.responses = [FakeResponse(body={"text": "hello VARIANT-1"})]

    text = await xai_speech.transcribe(router, b"wav-data", language="en")

    assert text == "hello VARIANT-1"
    method, url, kwargs = FakeClient.calls[0]
    assert (method, url) == ("POST", "https://api.x.ai/v1/stt")
    assert kwargs["files"]["file"] == ("audio.wav", b"wav-data", "audio/wav")
    assert kwargs["data"] == {"format": "true", "language": "en"}


@pytest.mark.asyncio
async def test_cloud_stt_omits_format_without_concrete_language():
    """xAI returns 400 for format=true without language; mic omits language."""
    router = FakeRouter()
    FakeClient.responses = [FakeResponse(body={"text": "plain transcript"})]

    text = await xai_speech.transcribe(router, b"wav-data", language=None)

    assert text == "plain transcript"
    _, _, kwargs = FakeClient.calls[0]
    assert kwargs["files"]["file"] == ("audio.wav", b"wav-data", "audio/wav")
    assert kwargs.get("data") in (None, {})


@pytest.mark.asyncio
async def test_cloud_stt_auto_language_does_not_send_format():
    router = FakeRouter()
    FakeClient.responses = [FakeResponse(body={"text": "auto path"})]

    text = await xai_speech.transcribe(router, b"wav-data", language="auto")

    assert text == "auto path"
    _, _, kwargs = FakeClient.calls[0]
    assert kwargs.get("data") in (None, {})


@pytest.mark.asyncio
async def test_cloud_voice_roster_uses_current_provider_endpoint():
    router = FakeRouter()
    FakeClient.responses = [FakeResponse(body={"voices": [
        {"voice_id": "eve", "name": "Eve", "language": "multilingual"},
        {"voice_id": "ara", "name": "Ara", "language": "multilingual"},
    ]})]

    voices = await xai_speech.list_voices(router)

    assert voices == [
        {"id": "eve", "name": "Eve", "language": "multilingual"},
        {"id": "ara", "name": "Ara", "language": "multilingual"},
    ]
    assert FakeClient.calls[0][0:2] == ("GET", "https://api.x.ai/v1/tts/voices")


@pytest.mark.asyncio
async def test_cloud_speech_fails_closed_without_credentials():
    router = FakeRouter()
    router._credential_leases = lambda provider: []

    with pytest.raises(xai_speech.SpeechUnavailable, match="API key or connected subscription"):
        await xai_speech.transcribe(router, b"wav")
    assert FakeClient.calls == []
