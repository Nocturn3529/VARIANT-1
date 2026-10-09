from unittest.mock import AsyncMock, Mock
import asyncio
import json

import pytest
import os

from llm_router import LLMRouter
from model_providers import ProviderRegistry
from model_runtime import hermes_proxy


def _router(cfg=None):
    return LLMRouter(
        cfg or {
            "mode": "cloud",
            "cloud": {
                "provider": "hermes",
                "hermes_model": "upstage/solar-pro4:free",
            },
        },
        ".",
        config_path=None,
    )


def test_hermes_is_one_fixed_oauth_proxy_provider():
    registry = ProviderRegistry()
    profile = registry.get("hermes-agent")
    assert profile is not None
    assert profile.name == "hermes"
    assert profile.display_name == "Hermes Agent (Nous OAuth)"
    assert profile.base_url == "http://127.0.0.1:8645/v1"
    assert profile.default_model == "meituan/longcat-2.5-preview:free"
    assert profile.auth_style == "optional"
    assert profile.env_vars == ()
    assert profile.supports_reasoning is True
    assert profile.supports_vision is True

    router = _router({
        "mode": "cloud",
        "cloud": {
            "provider": "hermes",
            "hermes_model": "upstage/solar-pro4:free",
            "provider_options": {
                "hermes": {"base_url": "https://example.invalid/v1"}
            },
        },
    })
    assert router.provider_base_url("hermes") == hermes_proxy.HERMES_PROXY_OPENAI_BASE
    with pytest.raises(ValueError, match="fixed loopback"):
        router.set_provider_options("hermes", {"base_url": "https://example.invalid"})


@pytest.mark.asyncio
async def test_proxy_start_and_authentication_are_host_owned(monkeypatch):
    healthy = {"status": "ok", "upstream": "Nous Portal", "authenticated": True}
    health = AsyncMock(side_effect=[None, healthy])
    start = Mock()
    monkeypatch.setattr(hermes_proxy, "_health", health)
    monkeypatch.setattr(hermes_proxy, "_start_proxy", start)
    monkeypatch.setattr(hermes_proxy.asyncio, "sleep", AsyncMock())

    result = await hermes_proxy.ensure_proxy("upstage/solar-pro4:free")

    start.assert_called_once_with()
    assert result["endpoint"] == hermes_proxy.HERMES_PROXY_OPENAI_BASE
    assert result["model"] == "upstage/solar-pro4:free"


@pytest.mark.asyncio
async def test_proxy_rejects_wrong_upstream_or_logged_out_state(monkeypatch):
    monkeypatch.setattr(
        hermes_proxy,
        "_health",
        AsyncMock(return_value={
            "status": "ok", "upstream": "xAI Grok OAuth", "authenticated": True,
        }),
    )
    with pytest.raises(hermes_proxy.HermesProxyError, match="not Nous Portal"):
        await hermes_proxy.ensure_proxy()

    monkeypatch.setattr(
        hermes_proxy,
        "_health",
        AsyncMock(return_value={
            "status": "ok", "upstream": "Nous Portal", "authenticated": False,
        }),
    )
    with pytest.raises(hermes_proxy.HermesProxyError, match="not authenticated"):
        await hermes_proxy.ensure_proxy()


@pytest.mark.asyncio
async def test_router_lists_models_through_the_proxy_without_credentials(monkeypatch):
    listing = AsyncMock(return_value=[{"id": "upstage/solar-pro4:free", "context_length": 524288}])
    monkeypatch.setattr(hermes_proxy, "available_model_catalog", listing)

    router = _router()
    assert router.has_cloud_key("hermes") is True
    lease = router._credential_leases("hermes")[0]
    assert lease.source == "anonymous"
    assert lease.secret == ""
    assert await router.list_cloud_models("hermes") == ["upstage/solar-pro4:free"]
    assert router.context_limit_tokens({"mode": "cloud", "provider": "hermes", "model": "upstage/solar-pro4:free"}) == 524288
    listing.assert_awaited_once_with(start_if_needed=True)
    listing.return_value = [{"id": "upstage/solar-pro4:free"}]
    await router.list_cloud_models("hermes")
    assert router.context_limit_tokens({"mode": "cloud", "provider": "hermes", "model": "upstage/solar-pro4:free"}) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("url,allowed", [("https://portal.nousresearch.com/device?user_code=TEST", True), ("https://example.invalid/device", False)])
async def test_managed_login_allowlists_verification_and_grant_commit(tmp_path, monkeypatch, url, allowed):
    executable = tmp_path / "venv" / ("Scripts" if os.name == "nt" else "bin") / ("hermes.exe" if os.name == "nt" else "hermes")
    executable.parent.mkdir(parents=True)
    executable.touch()
    executable.with_name("python.exe" if os.name == "nt" else "python").touch()
    monkeypatch.setattr(hermes_proxy, "hermes_executable_path", lambda: executable)
    reader = asyncio.StreamReader()
    for row in ({"event": "pending", "verification_url": url, "user_code": "TEST"}, {"event": "ready"}, {"event": "complete"}):
        reader.feed_data((json.dumps(row) + "\n").encode())
    reader.feed_eof()
    process = Mock(returncode=0, stdout=reader, stdin=Mock(drain=AsyncMock()), wait=AsyncMock(return_value=0))
    monkeypatch.setattr(hermes_proxy.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    monkeypatch.setattr(hermes_proxy, "resume_owned_process_and_reap", AsyncMock(return_value=None))
    verification = AsyncMock()
    if allowed:
        await hermes_proxy.manage_account(on_verification=verification)
        verification.assert_awaited_once_with(url, "TEST")
        process.stdin.write.assert_called_once_with(b"commit\n")
    else:
        with pytest.raises(hermes_proxy.HermesProxyError, match="unexpected verification"):
            await hermes_proxy.manage_account(on_verification=verification)
        process.stdin.write.assert_not_called()


def test_longcat_context_and_reasoning_policy_do_not_invent_effort_levels():
    from model_runtime.context import normalize_model_route, context_limit_tokens
    from model_runtime.request_policy import project_reasoning_policy
    r = _router()
    model = "meituan/longcat-2.5-preview:free"
    route = normalize_model_route(r, {"mode": "cloud", "provider": "hermes", "model": model})
    assert not route.get("reasoning_efforts")
    assert context_limit_tokens(r, route) == 1048576
    payload = {}
    assert project_reasoning_policy(r, r.provider_profile("hermes"), model, payload, None) == ""
    assert "reasoning_effort" not in payload
    project_reasoning_policy(r, r.provider_profile("hermes"), model, payload, 0)
    assert payload["reasoning"]["enabled"] is False


@pytest.mark.asyncio
async def test_managed_login_cancellation_reaps_real_interpreter_tree(tmp_path, monkeypatch):
    import sys
    import os
    import psutil
    from pathlib import Path
    installed = Path(sys.executable).with_name("hermes.exe" if os.name == "nt" else "hermes")
    worker = tmp_path / "hermes_auth_worker.py"
    pid_file = tmp_path / "pid.txt"
    worker.write_text("import os,time,json\nfrom pathlib import Path\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "print(json.dumps({'event':'pending','verification_url':'https://portal.nousresearch.com/device','user_code':'TEST'}),flush=True)\n"
        "time.sleep(120)\n")
    monkeypatch.setattr(hermes_proxy, "__file__", str(tmp_path / "hermes_proxy.py"))
    monkeypatch.setattr(hermes_proxy, "hermes_executable_path", lambda: installed)
    pending = asyncio.Event()
    async def verification(*_):
        pending.set()
    task = asyncio.create_task(hermes_proxy.manage_account(on_verification=verification))
    try:
        await asyncio.wait_for(pending.wait(), 15)
        pid = int(pid_file.read_text())
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    for _ in range(50):
        if not psutil.pid_exists(pid):
            break
        await asyncio.sleep(.02)
    assert not psutil.pid_exists(pid), "Cancelled OAuth must not leave the real venv interpreter alive"


@pytest.mark.parametrize("acknowledgment", ["commit\n", ""])
def test_auth_worker_never_emits_tokens_or_commits_without_acknowledgment(monkeypatch, capsys, acknowledgment):
    import io
    import sys
    from types import ModuleType
    from model_runtime import hermes_auth_worker
    auth = ModuleType("hermes_cli.auth")
    tokens = {"access_token": "secret-access", "refresh_token": "secret-refresh"}
    def login(**fields):
        print(tokens)  # Even unexpected SDK output must stay inside the helper.
        fields["on_verification"]("https://portal.nousresearch.com/device", "CODE")
        return tokens
    stored = []
    auth._nous_device_code_login = login
    auth.persist_nous_credentials = stored.append
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", auth)
    monkeypatch.setattr(sys, "argv", ["worker", "login"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(acknowledgment))
    hermes_auth_worker.main()
    output = capsys.readouterr().out
    assert "secret-" not in output
    assert bool(stored) == bool(acknowledgment)
