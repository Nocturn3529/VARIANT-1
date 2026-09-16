from __future__ import annotations

import sys

import pytest

from model_runtime.llama_server import LlamaServer, resolve_user_model_path


def test_relative_model_pins_use_writable_user_model_root(tmp_path):
    app_root = tmp_path / "app"
    data_dir = tmp_path / "data"
    engine = LlamaServer({
        "model": "qwen.gguf",
        "mmproj": "models/user/qwen-mmproj.gguf",
    }, str(app_root), str(data_dir))

    expected = "llama-server.exe" if sys.platform.startswith("win") else "llama-server"
    assert engine.binary == str(app_root / "bin" / expected)
    assert engine.model == str(data_dir / "models" / "user" / "qwen.gguf")
    assert engine.mmproj == str(
        data_dir / "models" / "user" / "qwen-mmproj.gguf"
    )


def test_absolute_model_pin_is_preserved_and_relative_escape_is_rejected(tmp_path):
    absolute = tmp_path / "elsewhere" / "model.gguf"
    assert resolve_user_model_path(str(tmp_path / "data"), str(absolute)) == str(
        absolute
    )
    with pytest.raises(ValueError, match="escapes models/user"):
        resolve_user_model_path(str(tmp_path / "data"), "../escape.gguf")


def test_runtime_args_enable_bounded_cache_idle_sleep_and_auto_fit(tmp_path):
    engine = LlamaServer({
        "model": "model.gguf",
        "ctx_size": "auto",
        "backend": "cpu",
    }, str(tmp_path))

    args = engine._build_args()

    assert "-c" not in args
    assert args[args.index("--parallel") + 1] == "1"
    assert args[args.index("--device") + 1] == "none"
    assert args[args.index("--cache-reuse") + 1] == "256"
    assert args[args.index("--cache-ram") + 1] == "512"
    assert args[args.index("--sleep-idle-seconds") + 1] == "1800"
    assert "--fit" in args
    assert "--metrics" in args


def test_auto_context_uses_known_loaded_slot_path_or_conservative_floor(tmp_path):
    engine = LlamaServer({
        "model": "model.gguf", "ctx_size": "auto", "fit_min_ctx": 4096,
    }, str(tmp_path))

    assert engine._loaded_context_size({
        "metadata": {"n_ctx": 999999},
        "default_generation_settings": {"n_ctx": 32768},
    }) == 32768
    assert engine._loaded_context_size({
        "metadata": {"n_ctx": 999999},
    }) == 0


@pytest.mark.asyncio
async def test_auto_context_falls_back_to_fit_floor_when_props_is_ambiguous(
    tmp_path, monkeypatch,
):
    engine = LlamaServer({
        "model": "model.gguf", "ctx_size": "auto", "fit_min_ctx": 4096,
    }, str(tmp_path))

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"metadata": {"n_ctx": 131072}}

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _url):
            return Response()

    monkeypatch.setattr(
        "model_runtime.llama_server.httpx.AsyncClient", Client,
    )

    await engine._detect_reasoning()

    assert engine.ctx_size == 4096
    assert engine.context_size_source == "conservative_fit_floor"


def test_manual_context_and_backend_override_are_preserved(tmp_path):
    engine = LlamaServer({
        "model": "model.gguf", "ctx_size": 12288,
        "backend": "cuda", "sleep_idle_seconds": 0,
    }, str(tmp_path))

    args = engine._build_args()

    assert args[args.index("-c") + 1] == "12288"
    assert args[args.index("--device") + 1] == "CUDA0"
    assert "--sleep-idle-seconds" not in args


def test_parallel_is_pinned_to_scheduler_capacity(tmp_path):
    engine = LlamaServer({
        "model": "model.gguf", "parallel": 8,
    }, str(tmp_path))

    args = engine._build_args()

    assert engine.requested_parallel == 8
    assert engine.parallel == 1
    assert args[args.index("--parallel") + 1] == "1"


def test_flash_attention_uses_explicit_tristate_semantics(tmp_path):
    disabled = LlamaServer({
        "model": "model.gguf", "flash_attn": False,
        "cache_type_k": "q8_0", "cache_type_v": "q8_0",
    }, str(tmp_path))
    automatic = LlamaServer({
        "model": "model.gguf", "flash_attn": "auto",
        "cache_type_k": "q8_0", "cache_type_v": "q8_0",
    }, str(tmp_path))

    disabled_args = disabled._build_args()
    automatic_args = automatic._build_args()

    assert disabled_args[disabled_args.index("--flash-attn") + 1] == "off"
    assert "--cache-type-k" not in disabled_args
    assert "--cache-type-v" not in disabled_args
    assert automatic_args[automatic_args.index("--flash-attn") + 1] == "auto"
    assert automatic_args[automatic_args.index("--cache-type-k") + 1] == "q8_0"
    assert automatic_args[automatic_args.index("--cache-type-v") + 1] == "q8_0"


def test_runtime_probe_omits_only_unsupported_options(tmp_path):
    engine = LlamaServer({
        "model": "model.gguf", "ctx_size": "auto", "flash_attn": "auto",
    }, str(tmp_path))
    engine._runtime_probe_complete = True
    engine._runtime_flags = frozenset({
        "--parallel", "--flash-attn", "--cache-prompt", "--fit",
    })

    args = engine._build_args()

    assert "--parallel" in args
    assert "--flash-attn" in args
    assert "--cache-prompt" in args
    assert "--fit" in args
    assert "--cache-type-k" not in args
    assert "--cache-type-v" not in args
    assert "--cache-reuse" not in args
    assert "--cache-ram" not in args
    assert "--ctx-checkpoints" not in args
    assert "--sleep-idle-seconds" not in args
    assert "--metrics" not in args
    assert set(engine._unsupported_requested_options()) >= {
        "cache_type_k", "cache_type_v", "cache_reuse", "cache_ram",
        "ctx_checkpoints", "sleep_idle_seconds", "metrics",
    }


def test_core_compatibility_retry_omits_capability_dependent_flags(tmp_path):
    engine = LlamaServer({
        "model": "model.gguf", "ctx_size": "auto",
        "backend": "cpu",
        "extra_args": ["--developer-only-tuning"],
    }, str(tmp_path))

    args = engine._build_args(core_only=True)

    assert "--parallel" not in args
    assert "--device" not in args
    assert "-dev" not in args
    assert args[args.index("-ngl") + 1] == "0"
    assert "--flash-attn" not in args
    assert "--cache-prompt" not in args
    assert "--fit" not in args
    assert "--metrics" not in args
    assert "--developer-only-tuning" not in args


def test_runtime_probe_emits_the_alias_that_was_actually_advertised(tmp_path):
    engine = LlamaServer({
        "model": "model.gguf",
        "ctx_size": "auto",
        "flash_attn": "on",
        "ubatch": 64,
        "backend": "cuda",
    }, str(tmp_path))
    engine._runtime_probe_complete = True
    engine._runtime_flags = frozenset({
        "-np", "-dev", "-fa", "-ctk", "-ctv", "-ub", "-cram", "-ctxcp",
        "-fit", "-fitt", "-fitc", "--cache-prompt", "--cache-reuse",
        "--sleep-idle-seconds", "--reasoning-budget", "--metrics",
    })

    args = engine._build_args()

    for advertised in (
        "-np", "-dev", "-fa", "-ctk", "-ctv", "-ub", "-cram", "-ctxcp",
        "-fit", "-fitt", "-fitc",
    ):
        assert advertised in args
    for unadvertised in (
        "--parallel", "--device", "--flash-attn", "--cache-type-k", "--cache-type-v",
        "--ubatch-size", "--cache-ram", "--ctx-checkpoints", "--fit",
        "--fit-target", "--fit-ctx",
    ):
        assert unadvertised not in args
