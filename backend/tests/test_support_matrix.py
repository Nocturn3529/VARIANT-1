from types import SimpleNamespace

import pytest
import llm_cloud_stream

from session_catalog.profiles import (
    ACTION_SURFACE,
    IPYTHON_SCHEMA_REVISION,
)
from session_catalog.support import (
    SupportMatrix,
    UnsupportedModelRoute,
    validate_tool_projection,
)
from run_context import Variant1RunContext, bind_run_context
from llm_router import LLMRouter
from model_runtime.context import model_route_support_coordinates
from ws_chat_sessions import _mutation_route


IPYTHON = [{"name": "ipython", "params": {"code": {"type": "string"}}}]


def _matrix():
    return SupportMatrix.from_config({
        "astb": {
            "support_matrix": [
                {
                    "profile": ACTION_SURFACE,
                    "provider": "local",
                    "model": "*qwen3.5-4b*",
                    "adapter": "llamacpp.*",
                    "status": "canary",
                    "evidence": "Prime static board",
                },
                {
                    "profile": ACTION_SURFACE,
                    "provider": "xai",
                    "model": "grok-4.6",
                    "adapter": "*",
                    "status": "canary",
                },
                {
                    "profile": ACTION_SURFACE,
                    "provider": "local",
                    "model": "bad-model",
                    "adapter": "*",
                    "status": "unqualified",
                },
            ]
        }
    })


def test_missing_support_matrix_is_fail_closed():
    matrix = SupportMatrix.from_config({"action_surface": {}})
    assert matrix.public_snapshot()["rules"] == []
    with pytest.raises(UnsupportedModelRoute, match="no support-matrix"):
        matrix.validate(
            profile=ACTION_SURFACE,
            provider="xai",
            model="grok-4.6",
            adapter="xai.responses",
        )


def test_support_matrix_keeps_one_profile_but_rejects_unqualified_models():
    matrix = _matrix()
    assert matrix.validate(
        profile=ACTION_SURFACE,
        provider="local",
        model="Qwen3.5-4B-BF16.gguf",
        adapter="llamacpp.chat_completions",
    ).status == "canary"
    assert matrix.validate(
        profile=ACTION_SURFACE,
        provider="xai",
        model="grok-4.6",
        adapter="openai.responses",
    ).status == "canary"
    with pytest.raises(UnsupportedModelRoute, match="unqualified"):
        matrix.validate(
            profile=ACTION_SURFACE,
            provider="local",
            model="bad-model",
            adapter="llamacpp.chat_completions",
        )
    with pytest.raises(UnsupportedModelRoute, match="no support-matrix"):
        matrix.validate(
            profile=ACTION_SURFACE,
            provider="anthropic",
            model="claude-unknown",
            adapter="anthropic.messages",
        )


def test_xai_oauth_support_coordinate_matches_responses_transport():
    router = SimpleNamespace(
        cfg={"cloud": {"xai_credential_policy": "subscription_first"}},
        mode="cloud",
        cloud_provider="xai",
        _kn=lambda provider: provider,
        get_cloud_model=lambda _provider: "grok-4.6",
        provider_profile=lambda _provider: SimpleNamespace(api_style="openai"),
        has_oauth=lambda provider: provider == "xai",
    )

    oauth = model_route_support_coordinates(
        router,
        {"mode": "cloud", "provider": "xai", "model": "grok-4.6"},
    )
    router.cfg["cloud"]["xai_credential_policy"] = "api_key_only"
    api_key = model_route_support_coordinates(
        router,
        {"mode": "cloud", "provider": "xai", "model": "grok-4.6"},
    )

    assert oauth["adapter"] == "xai.responses"
    assert api_key["adapter"] == "openai.*"

    router.cfg["cloud"]["xai_credential_policy"] = "subscription_first"
    sessions = SimpleNamespace(get_model_route=lambda _sid: {
            "mode": "cloud", "provider": "xai", "model": "grok-4.6",
        })
    server = SimpleNamespace(
        router=router,
        require_runtime=lambda: SimpleNamespace(
            sessions=sessions
        ),
    )
    assert _mutation_route(server, "chat")["adapter"] == "xai.responses"


def test_projection_fails_closed_on_schema_mixing():
    validate_tool_projection(
        ACTION_SURFACE,
        IPYTHON_SCHEMA_REVISION,
        IPYTHON,
    )
    with pytest.raises(UnsupportedModelRoute, match="exactly one ipython"):
        validate_tool_projection(
            ACTION_SURFACE,
            IPYTHON_SCHEMA_REVISION,
            [*IPYTHON, {"name": "read_file"}],
        )
    with pytest.raises(UnsupportedModelRoute, match="unsupported action surface"):
        validate_tool_projection(
            "native-tools.v1",
            "native.provider-schema.v1",
            IPYTHON,
        )


def test_router_context_surface_is_explicit_for_request_validation():
    config = SimpleNamespace(
        action_surface=ACTION_SURFACE,
        provider_tool_schema_revision=IPYTHON_SCHEMA_REVISION,
    )
    ctx = Variant1RunContext.create(source="chat", run_config=config)
    from session_catalog.support import current_action_surface

    with bind_run_context(ctx):
        assert current_action_surface() == (
            ACTION_SURFACE,
            IPYTHON_SCHEMA_REVISION,
        )


def test_mutation_authority_does_not_change_the_provider_profile():
    config = SimpleNamespace(
        action_surface=ACTION_SURFACE,
        provider_tool_schema_revision=IPYTHON_SCHEMA_REVISION,
    )
    ctx = Variant1RunContext.create(
        source="chat",
        run_config=config,
        metadata={
            "session_capabilities": {
                "mutation_write_enabled": True,
                "mutation_authority_revision": 7,
            },
        },
    )
    from session_catalog.support import current_action_surface

    with bind_run_context(ctx):
        assert current_action_surface() == (
            ACTION_SURFACE,
            IPYTHON_SCHEMA_REVISION,
        )


def test_mutation_off_uses_the_same_provider_profile():
    config = SimpleNamespace(
        action_surface=ACTION_SURFACE,
        provider_tool_schema_revision=IPYTHON_SCHEMA_REVISION,
    )
    ctx = Variant1RunContext.create(
        source="chat",
        run_config=config,
        metadata={
            "session_capabilities": {
                "mutation_write_enabled": False,
                "mutation_authority_revision": 8,
            },
        },
    )
    from session_catalog.support import current_action_surface

    with bind_run_context(ctx):
        assert current_action_surface() == (
            ACTION_SURFACE,
            IPYTHON_SCHEMA_REVISION,
        )


def test_internal_projection_skips_only_the_agent_tool_shape_gate():
    config = SimpleNamespace(
        action_surface=ACTION_SURFACE,
        provider_tool_schema_revision=IPYTHON_SCHEMA_REVISION,
    )
    ctx = Variant1RunContext.create(source="chat", run_config=config)
    router = object.__new__(LLMRouter)
    router._support_matrix = _matrix()

    with bind_run_context(ctx):
        qualified = router.validate_model_request(
            provider="local",
            model="Qwen3.5-4B-BF16.gguf",
            adapter="llamacpp.chat_completions",
            tools=None,
            internal_projection=True,
        )
        assert qualified["status"] == "canary"
        with pytest.raises(UnsupportedModelRoute, match="exactly one ipython"):
            router.validate_model_request(
                provider="local",
                model="Qwen3.5-4B-BF16.gguf",
                adapter="llamacpp.chat_completions",
                tools=None,
            )
        with pytest.raises(TypeError, match="cannot expose action tools"):
            router.validate_model_request(
                provider="local",
                model="Qwen3.5-4B-BF16.gguf",
                adapter="llamacpp.chat_completions",
                tools=IPYTHON,
                internal_projection=True,
            )


@pytest.mark.asyncio
async def test_unqualified_cloud_route_fails_before_oauth_provider_io():
    calls = []

    class Router:
        cloud_provider = "xai"

        def get_fallback_chain(self):
            return []

        def provider_profile(self, _provider):
            return SimpleNamespace(
                supports_vision=True, default_model="grok-4.6", api_style="openai"
            )

        def get_cloud_model(self, _provider):
            return "grok-4.6"

        def validate_model_request(self, **_kwargs):
            calls.append("validate")
            raise UnsupportedModelRoute(
                profile=ACTION_SURFACE, provider="xai", model="grok-4.6",
                adapter="openai.*", reason="test deny",
            )

        async def ensure_oauth_fresh(self, _provider):
            calls.append("oauth")

    with pytest.raises(UnsupportedModelRoute):
        _ = [token async for token in llm_cloud_stream.call_cloud(
            Router(), [], {}, tools=IPYTHON
        )]
    assert calls == ["validate"]


def test_operator_support_matrix_persists_revocation_and_one_global_profile(tmp_path):
    path = tmp_path / "llm.json"
    router = LLMRouter({"astb": {}}, str(tmp_path), config_path=str(path))
    snapshot = router.replace_support_matrix([{
        "profile": ACTION_SURFACE,
        "provider": "xai", "model": "grok-4.6", "adapter": "*",
        "status": "qualified", "evidence": "release gate 1",
    }, {
        "profile": ACTION_SURFACE,
        "provider": "local", "model": "bad-model", "adapter": "*",
        "status": "revoked", "evidence": "rollback",
    }])
    assert len(snapshot["rules"]) == 2 and path.is_file()
    with pytest.raises(UnsupportedModelRoute, match="revoked"):
        router._support_matrix.validate(
            profile=ACTION_SURFACE, provider="local", model="bad-model",
            adapter="llamacpp.chat",
        )
    with pytest.raises(ValueError, match="invalid profile"):
        router.replace_support_matrix([{
            "profile": ACTION_SURFACE, "status": "qualified",
        }, {
            "profile": "native-tools.v1", "status": "qualified",
        }])
