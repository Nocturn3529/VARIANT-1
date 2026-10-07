import asyncio
from types import SimpleNamespace

import pytest

import background_tasks
import ws_config
import xai_oauth
from model_runtime import hermes_proxy


class Socket:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(dict(payload))


class Router:
    def __init__(self):
        self.cfg = {"cloud": {}}
        self.saved = []

    @staticmethod
    def _kn(provider):
        return str(provider or "").strip().lower()

    def set_oauth_tokens(self, provider, **fields):
        self.saved.append((provider, dict(fields)))

    def oauth_status(self, provider):
        return {"provider": provider, "connected": bool(self.saved)}

    def clear_oauth(self, _provider):
        return None

class Server:
    def __init__(self):
        self.router = Router()
        self.broadcasts = []
        self.hub = SimpleNamespace(broadcast=self._broadcast)
        self._runtime = SimpleNamespace(
            models=SimpleNamespace(config_status=lambda: {"type": "config"})
        )

    async def _broadcast(self, payload):
        self.broadcasts.append(payload)

    def require_runtime(self):
        return self._runtime

    @staticmethod
    def engine_status_message():
        return {"type": "engine"}


def handlers():
    found = {}

    def on(name):
        def register(handler):
            found[name] = handler
            return handler
        return register

    ws_config.register(on)
    return found


@pytest.mark.asyncio
async def test_nous_reconnect_keeps_credentials_in_hermes(monkeypatch):
    from model_runtime import provider_accounts
    monkeypatch.setattr(background_tasks, "spawn", lambda coro, **_: asyncio.create_task(coro))
    async def login(*, on_verification, can_commit):
        await on_verification("https://portal.nousresearch.com/device", "CODE")
        assert can_commit()
    monkeypatch.setattr(hermes_proxy, "manage_account", login)
    monkeypatch.setattr(provider_accounts, "check_account", lambda *_: asyncio.sleep(0, result={"state": "ready"}))
    srv, socket = Server(), Socket()
    await handlers()["cloud:oauth:start"](srv, socket, None, {"provider": "hermes", "open_browser": False, "request_id": "nous-test"})
    task = srv._oauth_attempts["hermes"]["task"]
    await task
    assert srv.router.saved == []
    assert any(row["type"] == "cloud:oauth:pending" and row["user_code"] == "CODE" for row in socket.sent)
    assert any(row["type"] == "cloud:oauth:complete" for row in socket.sent)


@pytest.mark.asyncio
async def test_google_disconnect_is_available():
    srv, socket = Server(), Socket()
    await handlers()["cloud:oauth:disconnect"](srv, socket, None, {"provider": "google-antigravity"})
    assert socket.sent[-1]["type"] == "cloud:oauth:disconnected"


@pytest.mark.asyncio
async def test_first_class_xai_oauth_persists_only_owned_attempt(monkeypatch):
    monkeypatch.setattr(
        background_tasks, "spawn",
        lambda coro, name="", **_kwargs: asyncio.create_task(coro, name=name),
    )

    async def login(_config, *, open_browser, on_authorize_url):
        assert open_browser is True
        on_authorize_url(
            "https://auth.x.ai/authorize", "http://127.0.0.1:56121/callback"
        )
        return xai_oauth.TokenSet(
            access_token="AT", refresh_token="RT", expires_at=9_999_999_999,
            scope="openid api:access",
        )

    monkeypatch.setattr(xai_oauth, "login_pkce", login)
    srv, socket = Server(), Socket()
    await handlers()["cloud:oauth:start"](
        srv, socket, None,
        {"provider": "xai", "open_browser": True},
    )
    for _ in range(20):
        if srv.router.saved:
            break
        await asyncio.sleep(0)
    assert srv.router.saved[0][0] == "xai"
    assert srv.router.saved[0][1]["replace"] is True
    assert srv.router.saved[0][1]["auth_flow"] == "pkce"
    assert any(row["type"] == "cloud:oauth:pending" for row in socket.sent)
    assert any(row["type"] == "cloud:oauth:complete" for row in socket.sent)


@pytest.mark.asyncio
async def test_oauth_cancel_waits_for_attempt_and_prevents_late_persist(monkeypatch):
    monkeypatch.setattr(
        background_tasks, "spawn",
        lambda coro, name="", **_kwargs: asyncio.create_task(coro, name=name),
    )
    started = asyncio.Event()

    async def login(_config, *, open_browser, on_authorize_url):
        del open_browser
        on_authorize_url(
            "https://auth.x.ai/authorize", "http://127.0.0.1:56121/callback"
        )
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(xai_oauth, "login_pkce", login)
    srv, socket = Server(), Socket()
    registered = handlers()
    await registered["cloud:oauth:start"](
        srv, socket, None, {"provider": "xai"}
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    await registered["cloud:oauth:cancel"](
        srv, socket, None, {"provider": "xai"}
    )
    assert srv.router.saved == []
    assert getattr(srv, "_oauth_attempts", {}) == {}
    assert socket.sent[-1]["type"] == "cloud:oauth:cancelled"
