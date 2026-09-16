"""Regression tests for the backend code-review fixes.

Each test locks in a behavior change described in docs/BACKEND_CODE_REVIEW.md so
it can't silently regress. Kept dependency-light (no live engine / COM / network).
"""

import json
from pathlib import Path

import pytest

from tests.support.model_config import with_test_support


# ---- X1: the former desktop run registry was deleted -------------------------
def test_desktop_driver_has_no_parallel_session_registry():
    import desktop.session as ds
    assert not hasattr(ds, "_SESSION_REGISTRY")
    assert not hasattr(ds, "_DEFAULT_SESSION")
    assert not hasattr(ds, "register_desktop_session")


def test_browser_has_no_parallel_session_registry_or_backend():
    backend_root = Path(__file__).resolve().parents[1]
    assert not (backend_root / "browser" / "session.py").exists()
    assert not (backend_root / "browser" / "service.py").exists()
    assert not (backend_root / "browser" / "backends.py").exists()


# ---- L1: route failures must not replay or switch streams --------------------
def _router(**cfg):
    from llm_router import LLMRouter
    base = {"mode": "local"}
    base.update(cfg)
    return LLMRouter(with_test_support(base), app_root=".")


@pytest.mark.asyncio
async def test_stream_re_raises_after_a_token_was_yielded():
    from llm_router import LocalEngineError
    r = _router()

    async def local_gen(*a, **k):
        yield "partial"
        raise LocalEngineError("mid-stream boom")

    async def cloud_gen(*a, **k):
        yield "CLOUD"

    r._call_local, r._call_cloud = local_gen, cloud_gen

    out = []
    with pytest.raises(LocalEngineError):
        async for tok in r.stream(
            [{"role": "user", "content": "hi"}],
            internal_projection=True,
        ):
            out.append(tok)
    # got the partial local token and re-raised -- did NOT append the full cloud reply
    assert out == ["partial"]


@pytest.mark.asyncio
async def test_stream_does_not_switch_routes_when_local_fails_before_output():
    from llm_router import LocalEngineError
    r = _router()

    async def local_gen(*a, **k):
        raise LocalEngineError("down before any token")
        yield  # unreachable; makes this an async generator

    async def cloud_gen(*a, **k):
        yield "CLOUD"

    r._call_local, r._call_cloud = local_gen, cloud_gen

    with pytest.raises(LocalEngineError):
        _ = [tok async for tok in r.stream(
            [{"role": "user", "content": "hi"}],
            internal_projection=True,
        )]


@pytest.mark.asyncio
async def test_stream_does_not_switch_routes_when_cloud_fails_before_output():
    from llm_router import LocalEngineError
    r = _router(mode="cloud")

    async def local_gen(*a, **k):
        yield "LOCAL"

    async def cloud_gen(*a, **k):
        raise LocalEngineError("cloud down before any token")
        yield

    r._call_local, r._call_cloud = local_gen, cloud_gen

    with pytest.raises(LocalEngineError, match="cloud down"):
        _ = [tok async for tok in r.stream(
            [{"role": "user", "content": "hi"}],
            internal_projection=True,
        )]


# ---- L2: save_config is atomic (temp + os.replace) ---------------------------
def test_save_config_is_atomic(tmp_path):
    cfg_path = tmp_path / "config.json"
    r = _router()
    r.config_path = str(cfg_path)
    r.set_mode("cloud")  # triggers save_config
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["mode"] == "cloud"
    assert "vision" not in saved
    assert not (tmp_path / "config.json.tmp").exists()


def test_strict_save_surfaces_replace_failure_and_preserves_committed_file(
        tmp_path, monkeypatch):
    import llm_router_config

    cfg_path = tmp_path / "config.json"
    original = {"mode": "local", "local": {"model": "old.gguf"}}
    cfg_path.write_text(json.dumps(original), encoding="utf-8")

    def fail_replace(_src, _dst):
        raise OSError("replace denied")

    monkeypatch.setattr(llm_router_config.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace denied"):
        llm_router_config.save_config(
            str(cfg_path),
            {"mode": "local", "local": {"model": "new.gguf"}},
            strict=True,
        )

    assert json.loads(cfg_path.read_text(encoding="utf-8")) == original


def test_gemma_e4b_model_pairs_only_with_e4b_projector(tmp_path):
    from model_runtime.engine_manager import find_mmproj

    model = "gemma-4-E4B-it-Q4_K_M.gguf"
    (tmp_path / model).touch()
    (tmp_path / "mmproj-gemma-4-12B-it-bf16.gguf").touch()
    (tmp_path / "mmproj-gemma-4-E4B-it-bf16.gguf").touch()

    assert find_mmproj(str(tmp_path), model) == "mmproj-gemma-4-E4B-it-bf16.gguf"


# ---- MCP2: stdio subprocess env is scrubbed of ambient secrets ---------------
def test_mcp_child_env_scrubs_ambient_secrets(monkeypatch):
    from extensions.mcp_v2 import child_environment
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("MY_API_KEY", "sekret")
    monkeypatch.setenv("SESSION_TOKEN", "tok")
    monkeypatch.setenv("DB_PASSWORD", "pw")
    env = child_environment({"SERVER_SPECIFIC": "x"})
    assert env.get("PATH") == "/usr/bin"          # normal vars pass through
    assert "MY_API_KEY" not in env
    assert "SESSION_TOKEN" not in env
    assert "DB_PASSWORD" not in env
    assert env["SERVER_SPECIFIC"] == "x"          # server-configured env preserved
