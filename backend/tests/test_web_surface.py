"""The direct web seed shares one configured search and fetch path."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import tools_web


@pytest.mark.asyncio
async def test_structured_search_and_seed_use_the_same_provider(monkeypatch):
    search = AsyncMock(return_value=[
        ("A", "https://example.com/a", "snip"),
        ("B", "https://example.com/b", "more"),
    ])
    monkeypatch.setattr("web_search.providers.search", search)
    monkeypatch.setattr(
        "web_search.providers.last_search_metadata",
        lambda: {"provider": "variant1"},
    )
    monkeypatch.setattr(
        "web_search.providers.provider_name", lambda _config: "variant1"
    )

    config = {"provider": "variant1"}
    structured = await tools_web.search_web("query", 5, config=config)
    rendered = await tools_web.web_search({"query": "query"}, config=config)

    assert structured["results"][0] == {
        "title": "A", "url": "https://example.com/a",
        "snippet": "snip", "engines": [],
    }
    assert "https://example.com/a" in rendered
    assert search.await_count == 2


@pytest.mark.asyncio
async def test_structured_fetch_and_seed_use_the_same_static_reader(monkeypatch):
    reader = AsyncMock(return_value=("Readable page text", ""))
    monkeypatch.setattr(tools_web, "_static_fetch_text", reader)

    document = await tools_web.fetch_document("https://example.com/", 4000)
    rendered = await tools_web.web_search(
        {"query": "https://example.com/"}, config={"provider": "variant1"}
    )

    assert document["text"] == "Readable page text"
    assert "Readable page text" in rendered
    assert reader.await_count == 2


def test_static_html_prefers_article_and_keeps_noscript_fallback():
    body = """
    <html><body><nav>navigation noise</nav><main><article>
      <h1>Useful title</h1><noscript>Static fallback body</noscript>
    </article></main><footer>footer noise</footer></body></html>
    """
    text = tools_web.html_to_text(body)
    assert "Useful title" in text
    assert "Static fallback body" in text
    assert "navigation noise" not in text
    assert "footer noise" not in text


@pytest.mark.asyncio
async def test_non_text_fetch_diagnostic_is_preserved(monkeypatch):
    reader = AsyncMock(return_value=("", "(skipped non-text content: application/pdf)"))
    monkeypatch.setattr(tools_web, "_static_fetch_text", reader)
    assert await tools_web.fetch_text("https://example.com/report.pdf") == (
        "(skipped non-text content: application/pdf)"
    )


@pytest.mark.asyncio
async def test_url_seed_reports_final_redirect_destination(monkeypatch):
    monkeypatch.setattr(
        tools_web,
        "_static_fetch_result",
        AsyncMock(return_value={
            "text": "destination body",
            "note": "",
            "final_url": "https://cdn.example/final",
            "redirects": [
                "https://example.com/start", "https://cdn.example/final",
            ],
        }),
    )
    rendered = await tools_web.web_search(
        {"url": "https://example.com/start"}, config={"provider": "variant1"}
    )
    assert "Content of https://cdn.example/final" in rendered
    assert "https://example.com/start -> https://cdn.example/final" in rendered
