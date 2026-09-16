"""Tests for free in-process VARIANT-1 Search metasearch."""

from __future__ import annotations

import pytest

from web_search import providers as wsp
from web_search import search as ms


_DDG_HTML = """
<div class="result">
  <a class="result__a" href="https://example.com/a">Example Result A</a>
  <a class="result__snippet" href="https://example.com/a">First snippet text.</a>
</div>
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fb&amp;rut=x">Wrapped B</a>
  <a class="result__snippet" href="https://example.com/b">Second snippet.</a>
</div>
"""

_BING_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
  <item><title>Bing One</title><link>https://example.com/bing1</link>
    <description>Bing snippet one.</description></item>
  <item><title>Example Result A</title><link>https://example.com/a</link>
    <description>Duplicate of DDG A.</description></item>
</channel></rss>
"""


@pytest.fixture(autouse=True)
def _reset_search_runtime_state():
    """Keep cache/circuit state from leaking across behavioral tests."""
    ms._CACHE.clear()
    ms._INFLIGHT.clear()
    ms._ENGINE_HEALTH.clear()
    ms._ENGINE_HEALTH.update({name: ms._blank_health() for name in ms.DEFAULT_ENGINES})
    ms._LAST_STATUS.update({
        "checked": False,
        "healthy_engines": 0,
        "engine_count": len(ms.DEFAULT_ENGINES),
        "degraded": False,
        "last_latency_ms": 0,
        "last_run_at": 0.0,
        "cache_hit": False,
        "coalesced": False,
        "last_error": "",
        "failed_engines": [],
        "result_count": 0,
    })
    yield
    ms._CACHE.clear()
    ms._INFLIGHT.clear()


def test_parse_ddg_unwraps_and_titles():
    rows = ms.parse_ddg_html(_DDG_HTML, 5)
    assert rows[0][0] == "Example Result A"
    assert rows[0][1] == "https://example.com/a"
    assert rows[1][1] == "https://example.com/b"


def test_parse_bing_rss():
    rows = ms.parse_bing_rss(_BING_RSS, 5)
    assert len(rows) >= 2
    assert rows[0][1] == "https://example.com/bing1"
    assert "Bing snippet" in rows[0][2]


def test_parser_tolerates_attribute_order_and_single_quotes():
    html = """
    <a data-id='1' href='https://example.com/tolerant' class='x result__a'>Tolerant</a>
    <div data-id='2' class='result__snippet x'>Useful snippet.</div>
    """
    assert ms.parse_ddg_html(html, 1) == [
        ("Tolerant", "https://example.com/tolerant", "Useful snippet."),
    ]


def test_url_identity_preserves_meaningful_query_and_drops_tracking():
    first = ms.url_key("https://youtube.com/watch?v=first&utm_source=test#comments")
    second = ms.url_key("https://youtube.com/watch?v=second")
    assert first == "youtube.com/watch?v=first"
    assert second == "youtube.com/watch?v=second"
    assert first != second


def test_rank_fusion_dedupes_and_records_sources():
    a = [("A", "https://example.com/a", "da"), ("B", "https://example.com/b", "db")]
    b = [("A2", "https://example.com/a", "dup"), ("C", "https://example.com/c", "dc")]
    merged, provenance = ms._fuse_rows({"ddg": a, "bing": b}, 5)
    urls = [r[1] for r in merged]
    assert urls == [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    ]
    assert urls.count("https://example.com/a") == 1
    assert provenance[0] == {
        "url": "https://example.com/a",
        "sources": ["ddg", "bing"],
    }


def test_resolve_engines_defaults():
    assert ms.resolve_engines(None) == ["ddg", "bing"]
    assert ms.resolve_engines({"variant1": {"engines": "bing,ddg"}}) == ["bing", "ddg"]
    with pytest.raises(ValueError, match="unknown VARIANT-1 Search engine"):
        ms.resolve_engines({"variant1": {"engines": ["nope", "bing"]}})


def test_default_provider_is_variant1():
    assert wsp.provider_name({}) == "variant1"
    assert wsp.provider_name({"provider": "unknown"}) == "variant1"


def test_public_status_includes_variant1_block():
    st = wsp.public_status({"provider": "variant1", "variant1": {"engines": ["ddg"]}})
    assert st["provider"] == "variant1"
    assert st["variant1"]["docker_required"] is False
    assert "ddg" in st["variant1"]["engines"]


@pytest.mark.asyncio
async def test_variant1_search_merges_engines(monkeypatch):
    async def fake_ddg(query, n, client):
        return [("D1", "https://example.com/d1", "from ddg")]

    async def fake_bing(query, n, client):
        return [("B1", "https://example.com/b1", "from bing"),
                ("D1b", "https://example.com/d1", "dup")]

    monkeypatch.setattr(ms, "engine_ddg", fake_ddg)
    monkeypatch.setattr(ms, "engine_bing", fake_bing)
    # rebind map used inside search
    monkeypatch.setitem(ms._ENGINE_FNS, "ddg", fake_ddg)
    monkeypatch.setitem(ms._ENGINE_FNS, "bing", fake_bing)

    rows, metadata = await ms.search_detailed("hello", 5)
    urls = [r[1] for r in rows]
    assert "https://example.com/d1" in urls
    assert "https://example.com/b1" in urls
    assert len(urls) == 2
    source_map = {item["url"]: item["sources"] for item in metadata["result_sources"]}
    assert source_map["https://example.com/d1"] == ["ddg", "bing"]


@pytest.mark.asyncio
async def test_variant1_search_all_fail_raises(monkeypatch):
    async def boom(query, n, client):
        raise RuntimeError("blocked")

    for name in ("ddg", "bing"):
        monkeypatch.setitem(ms._ENGINE_FNS, name, boom)

    with pytest.raises(RuntimeError, match="all engines failed"):
        await ms.search("hello", 3)


@pytest.mark.asyncio
async def test_partial_failure_returns_results_with_truthful_metadata(monkeypatch):
    async def fake_ddg(query, n, client):
        return [("Shared", "https://example.com/shared?utm_source=ddg", "short")]

    async def fake_bing(query, n, client):
        raise ms.EngineError("blocked", "bing challenge page")

    monkeypatch.setitem(ms._ENGINE_FNS, "ddg", fake_ddg)
    monkeypatch.setitem(ms._ENGINE_FNS, "bing", fake_bing)

    rows, metadata = await ms.search_detailed("partial result", 5)

    assert rows == [("Shared", "https://example.com/shared?utm_source=ddg", "short")]
    assert metadata["degraded"] is True
    assert metadata["healthy_engines"] == 1
    assert metadata["failed_engines"] == ["bing"]
    assert metadata["result_sources"][0]["sources"] == ["ddg"]


@pytest.mark.asyncio
async def test_empty_results_and_partial_failure_are_not_reported_as_clean(monkeypatch):
    async def empty(query, n, client):
        return []

    async def blocked(query, n, client):
        raise ms.EngineError("blocked", "challenge page")

    monkeypatch.setitem(ms._ENGINE_FNS, "ddg", empty)
    monkeypatch.setitem(ms._ENGINE_FNS, "bing", blocked)

    with pytest.raises(RuntimeError, match="no results and some engines failed"):
        await ms.search("nothing useful", 5)


@pytest.mark.asyncio
async def test_transient_engine_error_retries_once(monkeypatch):
    calls = 0

    async def flaky(query, n, client):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ms.EngineError("rate_limited", "HTTP 429", retryable=True)
        return [("Recovered", "https://example.com/recovered", "ok")]

    async def no_sleep(_delay):
        return None

    monkeypatch.setitem(ms._ENGINE_FNS, "ddg", flaky)
    monkeypatch.setattr(ms.asyncio, "sleep", no_sleep)

    rows, metadata = await ms.search_detailed(
        "retry", 3, cfg={"variant1": {"engines": ["ddg"]}},
    )

    assert rows[0][0] == "Recovered"
    assert calls == 2
    assert metadata["outcomes"][0]["attempts"] == 2


@pytest.mark.asyncio
async def test_positive_results_are_cached(monkeypatch):
    calls = 0

    async def fake_ddg(query, n, client):
        nonlocal calls
        calls += 1
        return [("Cached", "https://example.com/cached", "ok")]

    monkeypatch.setitem(ms._ENGINE_FNS, "ddg", fake_ddg)
    cfg = {"variant1": {"engines": ["ddg"]}}

    first_rows, first_meta = await ms.search_detailed("cache me", 3, cfg=cfg)
    second_rows, second_meta = await ms.search_detailed("  CACHE   me ", 3, cfg=cfg)

    assert first_rows == second_rows
    assert first_meta["cache_hit"] is False
    assert second_meta["cache_hit"] is True
    assert second_meta["last_latency_ms"] == 0
    assert calls == 1


def test_query_length_is_bounded_before_network_use():
    with pytest.raises(ValueError, match="too long"):
        ms._validated_query("x" * (ms.MAX_QUERY_CHARS + 1))


@pytest.mark.asyncio
async def test_web_search_provider_routes_to_variant1(monkeypatch):
    async def fake(query, n, cfg=None):
        return [("T", "https://example.com/t", "s")]

    monkeypatch.setattr(wsp, "_variant1", fake)
    monkeypatch.setattr(wsp, "provider_name", lambda cfg=None: "variant1")
    rows = await wsp.search("q", 3, cfg={"provider": "variant1"})
    assert rows[0][1] == "https://example.com/t"


def test_tools_config_defaults_to_variant1(tmp_path):
    import tools

    path = tmp_path / "tools.json"
    path.write_text("{}", encoding="utf-8")
    cfg = tools.ToolsConfig(str(path))
    assert cfg.web_search["provider"] == "variant1"
    assert cfg.web_search["searxng"]["autostart"] is False
    assert "ddg" in cfg.web_search["variant1"]["engines"]


@pytest.mark.asyncio
async def test_tools_web_search_labels_variant1(monkeypatch):
    import tools

    async def fake(query, n, cfg=None):
        return [("Example Result A", "https://example.com/a", "snip")]

    monkeypatch.setattr("web_search.providers.search", fake)
    monkeypatch.setattr("web_search.providers.provider_name", lambda cfg=None: "variant1")
    out = await tools.web_search(
        {"query": "normal"}, config={"provider": "variant1"}
    )
    assert "provider=variant1" in out
    assert "Example Result A" in out
