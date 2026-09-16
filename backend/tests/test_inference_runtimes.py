from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from llm_local_stream import _build_local_payload
from llm_router import LLMRouter
from model_runtime import engine_manager
from model_runtime.external_runtime import ExternalOpenAIRuntime
from model_runtime.runtime_catalog import (
    configure_runtime,
    platform_id,
    runtime_catalog,
    runtime_config,
)


def _cfg():
    return {
        "mode": "local",
        "local": {"model": "model.gguf"},
        "inference": {
            "runtime": "llamacpp",
            "runtimes": {
                "vllm": {
                    "endpoint": "http://127.0.0.1:8000",
                    "model": "org/model",
                    "context_size": 16384,
                },
            },
        },
    }


def test_runtime_platform_identity_has_one_catalog_contract():
    expected = "windows" if sys.platform.startswith("win") else (
        "macos" if sys.platform == "darwin" else "linux"
    )
    assert platform_id() == expected


def test_runtime_catalog_keeps_llamacpp_bundled_and_optional_engines_guided():
    catalog = runtime_catalog(_cfg(), SimpleNamespace(runtime_id="llamacpp", ready=True))
    by_id = {item["id"]: item for item in catalog["items"]}

    assert catalog["selected"] == "llamacpp"
    assert by_id["llamacpp"]["bundled"] is True
    assert by_id["llamacpp"]["managed"] is True
    assert by_id["vllm"]["bundled"] is False
    assert by_id["vllm"]["configured"] is True
    assert by_id["sglang"]["setup"]["kind"] == "guided"
    assert by_id["openai_compatible"]["setup"]["kind"] == "configure"


def test_runtime_configuration_normalizes_endpoint_and_validates_env_name():
    cfg = _cfg()
    configured = configure_runtime(cfg, "vllm", {
        "endpoint": "http://localhost:9000/v1/",
        "model": "new/model",
        "context_size": 65536,
        "api_key_env": "VLLM_API_KEY",
    })

    assert configured["endpoint"] == "http://localhost:9000"
    assert runtime_config(cfg, "vllm")["model"] == "new/model"
    assert runtime_config(cfg, "vllm")["context_size"] == 65536
    with pytest.raises(ValueError, match="environment variable"):
        configure_runtime(cfg, "vllm", {"api_key_env": "not-valid!"})


def test_external_runtime_payload_uses_model_without_llamacpp_extensions(tmp_path):
    router = LLMRouter(_cfg(), str(tmp_path))
    router.engine = ExternalOpenAIRuntime("vllm", router.cfg)
    payload = _build_local_payload(
        router,
        [{"role": "user", "content": "hello"}],
        {"max_tokens": 20},
        False,
        0,
        None,
    )

    assert payload["model"] == "org/model"
    assert "reasoning_budget" not in payload
    assert "chat_template_kwargs" not in payload
    assert router.engine.request_adapter == "vllm.openai_chat_completions"


@pytest.mark.asyncio
async def test_switch_runtime_commits_only_after_candidate_is_ready():
    events = []

    class Engine:
        def __init__(self, runtime_id, ready=False, fail=False):
            self.runtime_id = runtime_id
            self.display_name = runtime_id
            self.ready = ready
            self.fail = fail
            self.proc = None

        async def start(self):
            events.append(f"start:{self.runtime_id}")
            if self.fail:
                raise RuntimeError("candidate unavailable")
            self.ready = True

        async def stop(self):
            events.append(f"stop:{self.runtime_id}")
            self.ready = False

    class Router:
        def __init__(self):
            self.engine = Engine("llamacpp", ready=True)
            self.committed = ""

        def wants_local_engine(self):
            return True

        def build_inference_runtime(self, runtime_id):
            return Engine(runtime_id, fail=runtime_id == "broken")

        def commit_inference_runtime(self, runtime_id, engine):
            self.engine = engine
            self.committed = runtime_id

    router = Router()
    assert await engine_manager.switch_inference_runtime(router, "vllm") == "switched"
    assert events == ["start:vllm", "stop:llamacpp"]
    assert router.committed == "vllm"

    previous = router.engine
    with pytest.raises(RuntimeError, match="candidate unavailable"):
        await engine_manager.switch_inference_runtime(router, "broken")
    assert router.engine is previous
    assert router.committed == "vllm"


@pytest.mark.asyncio
async def test_external_probe_discovers_models(monkeypatch):
    runtime = ExternalOpenAIRuntime("vllm", _cfg())

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "org/model"}, {"id": "org/other"}]}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr("model_runtime.external_runtime.httpx.AsyncClient", Client)
    status = await runtime.probe()

    assert status["ready"] is True
    assert status["models"] == ["org/model", "org/other"]


@pytest.mark.asyncio
async def test_external_runtime_uses_available_tokenizer_endpoint(monkeypatch):
    runtime = ExternalOpenAIRuntime("vllm", _cfg())
    runtime.ready = True
    requests = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"tokens": [1, 2, 3, 4]}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            requests.append((url, kwargs["json"]))
            return Response()

    monkeypatch.setattr(
        "model_runtime.external_runtime.httpx.AsyncClient", Client,
    )

    assert await runtime.count_tokens("hello") == 4
    assert requests[0][0].endswith("/tokenize")
