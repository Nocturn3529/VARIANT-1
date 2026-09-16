"""Single-path web_search providers (SearXNG default, optional APIs)."""

from __future__ import annotations

import pytest

from web_search import providers as wsp


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.content = b"{}" if payload is not None else text.encode("utf-8")

    def json(self):
        return self._payload


def _client(response: _Response):
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, *a, **k):
            return response

        async def post(self, *a, **k):
            return response

    return _Client


@pytest.mark.asyncio
async def test_searxng_parses_json_results(monkeypatch):
    payload = {
        "results": [
            {"title": "Paris", "url": "https://example.com/paris", "content": "Capital of France"},
            {"title": "Other", "url": "https://example.com/x", "content": "x"},
        ]
    }
    monkeypatch.setattr("httpx.AsyncClient", _client(_Response(200, payload)))
    rows = await wsp.search(
        "capital of France",
        5,
        cfg={"provider": "searxng", "searxng": {"base_url": "http://127.0.0.1:8888"}},
    )
    assert rows[0][0] == "Paris"
    assert rows[0][1] == "https://example.com/paris"
    assert "Capital" in rows[0][2]


@pytest.mark.asyncio
async def test_searxng_missing_url_raises():
    with pytest.raises(RuntimeError, match="base_url"):
        await wsp.search("q", 3, cfg={"provider": "searxng", "searxng": {"base_url": ""}})


@pytest.mark.asyncio
async def test_searxng_unparsed_json_is_not_reported_as_empty_results(monkeypatch):
    monkeypatch.setattr("httpx.AsyncClient", _client(_Response(200, {"query": "q"})))
    with pytest.raises(RuntimeError, match="results list"):
        await wsp.search(
            "q", 3,
            cfg={"provider": "searxng", "searxng": {"base_url": "http://127.0.0.1:8888"}},
        )


@pytest.mark.asyncio
async def test_optional_ddg_uses_validated_engine_and_surfaces_failure(monkeypatch):
    from web_search import search as search_engine

    async def blocked(*_args, **_kwargs):
        raise search_engine.EngineError("blocked", "ddg challenge")

    monkeypatch.setattr(search_engine, "engine_ddg", blocked)
    with pytest.raises(RuntimeError, match="ddg challenge"):
        await wsp.search("q", 3, cfg={"provider": "ddg"})


@pytest.mark.asyncio
async def test_brave_requires_key():
    with pytest.raises(RuntimeError, match="API key"):
        await wsp.search("q", 3, cfg={"provider": "brave", "brave": {"api_key": ""}})


@pytest.mark.asyncio
async def test_brave_parses_results(monkeypatch):
    payload = {
        "web": {
            "results": [
                {"title": "A", "url": "https://a.example", "description": "desc"},
            ]
        }
    }
    monkeypatch.setattr("httpx.AsyncClient", _client(_Response(200, payload)))
    rows = await wsp.search(
        "q", 5, cfg={"provider": "brave", "brave": {"api_key": "test-key"}},
    )
    assert rows == [("A", "https://a.example", "desc")]


@pytest.mark.asyncio
async def test_tavily_parses_results(monkeypatch):
    payload = {
        "results": [
            {"title": "T", "url": "https://t.example", "content": "snippet"},
        ]
    }
    monkeypatch.setattr("httpx.AsyncClient", _client(_Response(200, payload)))
    rows = await wsp.search(
        "q", 5, cfg={"provider": "tavily", "tavily": {"api_key": "tvly-test"}},
    )
    assert rows == [("T", "https://t.example", "snippet")]


def test_public_status_hides_secrets():
    st = wsp.public_status({
        "provider": "brave",
        "brave": {"api_key": "secret"},
        "searxng": {"base_url": "http://127.0.0.1:8888"},
    })
    assert st["provider"] == "brave-free"
    assert st["brave"]["has_api_key"] is True
    assert "secret" not in str(st)
