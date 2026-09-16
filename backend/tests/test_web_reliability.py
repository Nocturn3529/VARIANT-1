"""P1 web reliability: DDG URL unwrap, shared static search, browser fallbacks."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import tools_web as tools


_DDG_HTML = """
<div class="result">
  <a class="result__a" href="https://example.com/a">Example Result A</a>
  <a class="result__snippet" href="https://example.com/a">First snippet text.</a>
</div>
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fb&amp;rut=x">Wrapped B</a>
  <a class="result__snippet" href="https://example.com/b">Second snippet text.</a>
</div>
"""


async def _web_search(args):
    return await tools.web_search(args, config={"provider": "variant1"})


class _Response:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


def _make_httpx_client(responses):
    """Return a fake AsyncClient class that yields responses in order for POST."""
    queue = list(responses)

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            if not queue:
                return _Response(status_code=500, text="")
            return queue.pop(0)

    return _Client


def test_normalize_result_url_unwraps_ddg_redirect():
    wrapped = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&rut=abc"
    assert tools.normalize_result_url(wrapped) == "https://example.com/page"
    assert tools.normalize_result_url("https://example.com/direct") == "https://example.com/direct"


def test_parse_ddg_unwraps_uddg_and_keeps_direct_links():
    rows = tools.parse_ddg(_DDG_HTML, 5)
    assert rows == [
        ("Example Result A", "https://example.com/a", "First snippet text."),
        ("Wrapped B", "https://example.com/b", "Second snippet text."),
    ]


@pytest.mark.asyncio
async def test_ddg_static_search_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(
        "httpx.AsyncClient",
        _make_httpx_client([
            _Response(status_code=429, text=""),
            _Response(status_code=200, text=_DDG_HTML),
        ]),
    )
    sleeps = []

    async def _fake_sleep(_):
        sleeps.append(True)

    monkeypatch.setattr(tools, "_sleep_backoff", _fake_sleep)
    rows = await tools.ddg_static_search("query", 5)
    assert len(rows) == 2
    assert rows[1][1] == "https://example.com/b"
    assert sleeps  # at least one backoff after 429


@pytest.mark.asyncio
async def test_web_search_uses_configured_provider(monkeypatch):
    async def _fake_search(query, n, cfg=None):
        assert query == "normal"
        return [("Example Result A", "https://example.com/a", "snip")]

    monkeypatch.setattr("web_search.providers.search", _fake_search)
    monkeypatch.setattr("web_search.providers.provider_name", lambda cfg=None: "variant1")
    out = await _web_search({"query": "normal"})
    assert "Example Result A" in out
    assert "provider=variant1" in out
    assert "BEGIN UNTRUSTED WEB_SEARCH" in out


@pytest.mark.asyncio
async def test_web_search_surfaces_provider_config_errors(monkeypatch):
    async def _boom(query, n, cfg=None):
        raise RuntimeError("SearXNG base_url is not set")

    monkeypatch.setattr("web_search.providers.search", _boom)
    with pytest.raises(tools.ToolError, match="SearXNG"):
        await _web_search({"query": "x"})


@pytest.mark.asyncio
async def test_web_search_reads_complete_url_when_static_is_rich():
    rich = "A" * 500

    async def _rich(_url, _max):
        return rich, ""

    with patch.object(tools, "_static_fetch_text", new=AsyncMock(side_effect=_rich)):
        out = await _web_search({"query": "https://example.com/"})

    assert rich in out


@pytest.mark.asyncio
async def test_web_search_complete_http_url_matches_schema_contract(monkeypatch):
    rich = "HTTP content " * 40

    async def _rich(_url, _max):
        return rich, ""

    monkeypatch.setattr(tools, "_static_fetch_text", _rich)

    out = await _web_search({"query": "http://example.com/article"})

    assert rich in out
    assert "BEGIN UNTRUSTED WEB_PAGE" in out


@pytest.mark.asyncio
async def test_web_search_accepts_explicit_url_alias_and_rejects_ambiguity(monkeypatch):
    async def _rich(_url, _max):
        return "explicit URL content", ""

    monkeypatch.setattr(tools, "_static_fetch_text", _rich)

    out = await _web_search({"url": "http://example.com/article"})

    assert "explicit URL content" in out
    assert "BEGIN UNTRUSTED WEB_PAGE" in out
    with pytest.raises(tools.ToolError, match="either 'query' or 'url'"):
        await _web_search({
            "query": "search terms",
            "url": "http://example.com/article",
        })


@pytest.mark.asyncio
async def test_web_search_query_length_is_bounded():
    with pytest.raises(tools.ToolError, match="too long"):
        await _web_search({"query": "q" * (tools.MAX_SEARCH_QUERY_CHARS + 1)})


@pytest.mark.asyncio
async def test_web_search_url_read_is_honest_when_static_is_empty():
    async def _empty(_url, _max):
        return "", ""

    with patch.object(tools, "_static_fetch_text", new=AsyncMock(side_effect=_empty)):
        out = await _web_search({"query": "https://example.com/"})

    assert "empty body" in out.lower() or "browser.navigate" in out
    assert "BEGIN UNTRUSTED WEB_PAGE" in out


@pytest.mark.asyncio
async def test_web_search_url_read_accepts_mocked_loopback_http(monkeypatch):
    async def _loopback(_url, _max):
        return "local service response", ""

    monkeypatch.setattr(tools, "_static_fetch_text", _loopback)
    out = await _web_search({"query": "http://127.0.0.1/secret"})

    assert "local service response" in out
    assert "BEGIN UNTRUSTED WEB_PAGE" in out
