"""Native llama.cpp chat-input token counting and compatibility fallbacks."""

from types import SimpleNamespace

import pytest

from model_runtime import llama_server
from model_runtime.llama_server import LlamaServer
from llm_local_stream import _build_local_payload
from llm_router import LLMRouter


class _Response:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


@pytest.mark.asyncio
async def test_llama_server_prefers_native_chat_input_token_endpoint(monkeypatch):
    calls = []

    class _Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, *, json):
            calls.append((url, json))
            return _Response(200, {
                "object": "response.input_tokens",
                "input_tokens": 264,
            })

    monkeypatch.setattr(llama_server.httpx, "AsyncClient", _Client)
    engine = LlamaServer({"autostart": False}, app_root=".")
    engine.ready = True
    engine.host = "127.0.0.1"
    engine.port = 8080
    template_payload = {
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "read_file",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
        "tool_choice": "auto",
        "reasoning_budget": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }

    count = await engine.count_prompt_tokens(template_payload)

    assert count == 264
    assert calls == [(
        "http://127.0.0.1:8080/v1/chat/completions/input_tokens",
        template_payload,
    )]


@pytest.mark.asyncio
async def test_llama_server_404_falls_back_to_apply_template_and_tokenize(monkeypatch):
    calls = []

    class _Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, *, json):
            calls.append((url, json))
            if url.endswith("/v1/chat/completions/input_tokens"):
                return _Response(404, {"error": "not found"})
            if url.endswith("/apply-template"):
                return _Response(200, {"prompt": "<bos>templated prompt<assistant>"})
            if url.endswith("/tokenize"):
                return _Response(200, {"tokens": [1, 2, 3, 4, 5]})
            raise AssertionError(url)

    monkeypatch.setattr(llama_server.httpx, "AsyncClient", _Client)
    engine = LlamaServer({"autostart": False}, app_root=".")
    engine.ready = True

    count = await engine.count_prompt_tokens({
        "messages": [{"role": "user", "content": "hello"}],
    })

    assert count == 5
    assert [url.rsplit("/", 1)[-1] for url, _payload in calls] == [
        "input_tokens", "apply-template", "tokenize",
    ]


@pytest.mark.asyncio
async def test_router_count_uses_generation_projection_and_native_tool_schema():
    class _Engine:
        reasoning_budget = 0

        def __init__(self):
            self.payload = None

        async def count_prompt_tokens(self, payload):
            self.payload = payload
            return 77

    router = object.__new__(LLMRouter)
    router.mode = "local"
    router.engine = _Engine()
    tools = [{
        "name": "read_file",
        "description": "Read one file",
        "params": {
            "path": {"type": "string", "required": True},
        },
    }]
    source_messages = [
        {"role": "developer", "content": "Use tools carefully."},
        {"role": "user", "content": "Open the file."},
    ]

    count = await LLMRouter.count_prompt_tokens(
        router,
        source_messages,
        tools=tools,
    )

    assert count == 77
    template_payload = router.engine.payload
    assert [message["role"] for message in template_payload["messages"]] == [
        "system", "user",
    ]
    function = template_payload["tools"][0]["function"]
    assert function["name"] == "read_file"
    assert function["parameters"]["required"] == ["path"]
    assert template_payload["chat_template_kwargs"] == {"enable_thinking": False}

    generation_payload = _build_local_payload(
        SimpleNamespace(engine=SimpleNamespace(reasoning_budget=0)),
        template_payload["messages"],
        {"temperature": 0.7, "top_p": 0.95, "max_tokens": 128},
        False,
        None,
        tools,
    )
    for key in (
        "messages", "tools", "tool_choice", "reasoning_budget",
        "chat_template_kwargs",
    ):
        assert template_payload[key] == generation_payload[key]


@pytest.mark.asyncio
async def test_router_prompt_count_is_local_only():
    router = object.__new__(LLMRouter)
    router.mode = "cloud"
    router.engine = SimpleNamespace()

    assert await LLMRouter.count_prompt_tokens(
        router, [{"role": "user", "content": "hello"}]
    ) is None


@pytest.mark.asyncio
async def test_token_counters_follow_local_route_pinned_over_cloud_default():
    class _Engine:
        model = ""
        reasoning_budget = 0

        def __init__(self):
            self.text_calls = []
            self.prompt_calls = []

        async def count_tokens(self, text):
            self.text_calls.append(text)
            return 3

        async def count_prompt_tokens(self, payload):
            self.prompt_calls.append(payload)
            return 9

    router = object.__new__(LLMRouter)
    router._mode = "cloud"
    router.cfg = {
        "mode": "cloud",
        "cloud": {"provider": "anthropic", "anthropic_model": "claude"},
    }
    router.provider_registry = SimpleNamespace(
        canonical_name=lambda value: value,
        get=lambda _value: None,
    )
    router.engine = _Engine()

    with router.bind_model_route({
        "mode": "local", "provider": "local", "model": "",
    }):
        assert router.mode == "local"
        assert await router.count_tokens("hello") == 3
        assert await router.count_prompt_tokens(
            [{"role": "user", "content": "hello"}]
        ) == 9

    assert router.mode == "cloud"
    assert router.engine.text_calls == ["hello"]
    assert len(router.engine.prompt_calls) == 1


@pytest.mark.asyncio
async def test_token_counters_skip_cloud_route_pinned_over_local_default():
    class _Engine:
        model = "local.gguf"
        reasoning_budget = 0

        async def count_tokens(self, _text):
            raise AssertionError("cloud-pinned turn must not use local tokenizer")

        async def count_prompt_tokens(self, _payload):
            raise AssertionError("cloud-pinned turn must not use local template")

    router = object.__new__(LLMRouter)
    router._mode = "local"
    router.cfg = {
        "mode": "local",
        "cloud": {"provider": "openai", "openai_model": "gpt-4o"},
    }
    router.provider_registry = SimpleNamespace(
        canonical_name=lambda value: value,
        get=lambda _value: None,
    )
    router.engine = _Engine()

    with router.bind_model_route({
        "mode": "cloud", "provider": "openai", "model": "gpt-4o",
    }):
        assert router.mode == "cloud"
        assert await router.count_tokens("hello") is None
        assert await router.count_prompt_tokens(
            [{"role": "user", "content": "hello"}]
        ) is None

    assert router.mode == "local"


@pytest.mark.asyncio
async def test_multimodal_count_sends_generation_image_to_native_counter_unchanged():
    class _Engine:
        reasoning_budget = 0

        def __init__(self):
            self.payload = None

        async def count_prompt_tokens(self, payload):
            self.payload = payload
            # Native llama.cpp owns multimodal accounting. The adapter must not
            # guess a surcharge or replace the image before token preflight.
            return 19

    router = object.__new__(LLMRouter)
    router.mode = "local"
    router.engine = _Engine()
    image_data = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
        "AAAADUlEQVR42mNk+M/wHwAF/gL+3i8AAAAASUVORK5CYII="
    )
    image = {
        "data_b64": image_data,
        "media_type": "image/png",
        "origin": "current_user",
    }

    count = await router.count_prompt_tokens(
        [{"role": "user", "content": "What is shown?"}],
        image_b64=image,
    )

    assert count == 19
    content = router.engine.payload["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "What is shown?"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == f"data:image/png;base64,{image_data}"
