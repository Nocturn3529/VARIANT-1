"""Registry-driven web-search providers for VARIANT-1.

Every backend projects into the same bounded ``(title, url, snippet)`` rows,
so adding provider richness never expands the model-facing tool surface. A
provider is selected explicitly; there is no silent cross-provider fallback.
Secrets come from the shared encrypted service credential store.
"""

from __future__ import annotations

import copy
import json
import os
import re
import uuid
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlparse

import httpx

from service_credentials import configured as credential_configured
from service_credentials import secret as credential_secret

Row = tuple[str, str, str]

_DEFAULT_PROVIDER = "variant1"
_ALIASES = {"brave": "brave-free", "ddg": "ddgs"}
_PROVIDERS: tuple[dict[str, Any], ...] = (
    {"id": "variant1", "name": "VARIANT-1 Search", "auth": "none",
     "description": "Free in-process metasearch across public engines.",
     "signup_url": "", "env_vars": (), "keyless": True},
    {"id": "ddgs", "name": "DuckDuckGo", "auth": "none",
     "description": "Free DuckDuckGo HTML search.",
     "signup_url": "", "env_vars": (), "keyless": True},
    {"id": "brave-free", "name": "Brave Search", "auth": "api_key",
     "description": "Brave's independent web index and free API tier.",
     "signup_url": "https://brave.com/search/api/",
     "env_vars": ("BRAVE_SEARCH_API_KEY", "BRAVE_API_KEY"), "keyless": False},
    {"id": "exa", "name": "Exa", "auth": "optional",
     "description": "Semantic web search; anonymous MCP or paid API key.",
     "signup_url": "https://exa.ai", "env_vars": ("EXA_API_KEY",), "keyless": True},
    {"id": "firecrawl", "name": "Firecrawl", "auth": "optional",
     "description": "Web search through Firecrawl's cloud or self-hosted API.",
     "signup_url": "https://firecrawl.dev", "env_vars": ("FIRECRAWL_API_KEY",),
     "keyless": True},
    {"id": "keenable", "name": "Keenable", "auth": "optional",
     "description": "AI-oriented search with a public anonymous tier.",
     "signup_url": "https://keenable.ai", "env_vars": ("KEENABLE_API_KEY",),
     "keyless": True},
    {"id": "parallel", "name": "Parallel", "auth": "optional",
     "description": "Agentic web search; anonymous MCP or paid API key.",
     "signup_url": "https://parallel.ai", "env_vars": ("PARALLEL_API_KEY",),
     "keyless": True},
    {"id": "searxng", "name": "SearXNG", "auth": "endpoint",
     "description": "Self-hosted or VARIANT-1-managed metasearch.",
     "signup_url": "https://docs.searxng.org", "env_vars": (), "keyless": True},
    {"id": "tavily", "name": "Tavily", "auth": "optional",
     "description": "Search API for agents with keyed and explicit keyless access.",
     "signup_url": "https://app.tavily.com/home", "env_vars": ("TAVILY_API_KEY",),
     "keyless": True},
    {"id": "xai", "name": "xAI Web Search", "auth": "shared",
     "description": "Grok's server-side agentic web search.",
     "signup_url": "https://console.x.ai", "env_vars": ("XAI_API_KEY",),
     "keyless": False, "shared_provider": "xai"},
)
_BY_ID = {item["id"]: item for item in _PROVIDERS}
_KNOWN = frozenset(_BY_ID)

_LAST_SEARCH_METADATA: ContextVar[dict[str, Any]] = ContextVar(
    "variant1_last_search_metadata", default={}
)
_KEYLESS_SESSION_ID = uuid.uuid4().hex
_EXA_MCP_URL = "https://mcp.exa.ai/mcp"
_PARALLEL_MCP_URL = "https://search.parallel.ai/mcp"


def provider_name(cfg: dict) -> str:
    if not isinstance(cfg, dict):
        raise TypeError("web search configuration is required")
    raw = str(cfg.get("provider") or _DEFAULT_PROVIDER).strip().lower()
    name = _ALIASES.get(raw, raw)
    return name if name in _KNOWN else _DEFAULT_PROVIDER


def provider_definitions() -> list[dict[str, Any]]:
    return [copy.deepcopy(item) for item in _PROVIDERS]


def _block(cfg: dict, name: str) -> dict:
    value = cfg.get(name)
    if not isinstance(value, dict):
        for old, new in _ALIASES.items():
            if new == name and isinstance(cfg.get(old), dict):
                value = cfg.get(old)
                break
    return dict(value) if isinstance(value, dict) else {}


def public_status(cfg: dict, *, runtime: dict | None = None, router=None) -> dict[str, Any]:
    """Return a safe Settings snapshot containing no credentials."""
    if not isinstance(cfg, dict):
        raise TypeError("web search configuration is required")
    name = provider_name(cfg)
    searx = _block(cfg, "searxng")
    variant1_block = _block(cfg, "variant1")
    base = str(searx.get("base_url") or os.environ.get("VARIANT1_SEARXNG_URL")
               or os.environ.get("SEARXNG_URL") or "http://127.0.0.1:8888").strip()
    try:
        port = int(searx.get("port", 8888))
    except (TypeError, ValueError):
        port = 8888
    out_searx: dict[str, Any] = {
        "base_url": base, "autostart": bool(searx.get("autostart", False)),
        "managed": bool(searx.get("managed", True)),
        "host": str(searx.get("host") or "127.0.0.1"), "port": port,
        "container_name": str(searx.get("container_name") or "variant1-searxng"),
        "docker_image": str(searx.get("docker_image") or "docker.io/searxng/searxng:latest"),
    }
    if isinstance(runtime, dict):
        out_searx.update({key: runtime[key] for key in (
            "ready", "owned", "running", "docker_available", "runtime", "error",
            "base_url", "host", "port", "container_name", "docker_image",
            "autostart", "managed",
        ) if key in runtime})
    try:
        from web_search import search as metasearch
        out_variant1 = metasearch.public_status({"variant1": variant1_block})
    except Exception:
        out_variant1 = {"engines": ["ddg", "bing"], "docker_required": False,
                        "api_key_required": False, "mode": "in_process"}
    rows = []
    for definition in _PROVIDERS:
        provider_id = str(definition["id"])
        env_vars = tuple(definition.get("env_vars") or ())
        shared = str(definition.get("shared_provider") or "")
        has_credential = bool(str(_block(cfg, provider_id).get("api_key") or "").strip()) or credential_configured(
            router, "web", provider_id, env_vars=env_vars, shared_provider=shared)
        available = bool(definition.get("keyless") or has_credential)
        if provider_id == "searxng":
            available = bool(out_searx.get("ready") or out_searx.get("base_url"))
        rows.append({
            **{key: copy.deepcopy(value) for key, value in definition.items()
               if key != "env_vars"},
            "env_vars": list(env_vars), "configured": has_credential,
            "available": available, "active": provider_id == name,
            "config": {key: value for key, value in _block(cfg, provider_id).items()
                       if key not in {"api_key", "token", "secret"}},
        })
    return {
        "provider": name, "providers": rows, "variant1": out_variant1,
        "searxng": out_searx,
        "brave": {"has_api_key": next(
            (row["configured"] for row in rows if row["id"] == "brave-free"), False)},
        "tavily": {"has_api_key": next(
            (row["configured"] for row in rows if row["id"] == "tavily"), False)},
    }


def _headers() -> dict[str, str]:
    return {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9"}


def _clip(value: str, limit: int = 400) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _api_key(cfg: dict, provider: str, router=None) -> str:
    definition = _BY_ID[provider]
    legacy = str(_block(cfg, provider).get("api_key") or "").strip()
    if legacy:
        return legacy
    return await credential_secret(
        router, "web", provider, env_vars=tuple(definition.get("env_vars") or ()),
        shared_provider=str(definition.get("shared_provider") or ""))


def _json_rows(items: Any, n: int) -> list[Row]:
    out: list[Row] = []
    for item in items if isinstance(items, list) else ():
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or item.get("link") or "").strip()
        if not url:
            continue
        title = str(item.get("title") or item.get("name") or url).strip()
        snippet = str(item.get("description") or item.get("snippet") or item.get("content")
                      or " ".join(str(part) for part in (item.get("highlights") or ()) if part)
                      or " ".join(str(part) for part in (item.get("excerpts") or ()) if part)).strip()
        out.append((title or url, url, _clip(snippet)))
        if len(out) >= n:
            break
    return out


async def _post_json(url: str, *, payload: dict, headers: dict | None = None,
                     timeout: float = 45.0) -> dict:
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=False) as client:
            response = await client.post(url, json=payload, headers=headers or {})
    except (httpx.TimeoutException, httpx.TransportError, OSError) as exc:
        raise RuntimeError(f"request failed ({type(exc).__name__})") from exc
    if response.status_code >= 400:
        detail = _clip(response.text, 300)
        raise RuntimeError(f"HTTP {response.status_code}" + (f": {detail}" if detail else ""))
    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError("response was not JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("response JSON must be an object")
    return data


async def search(query: str, n: int = 5, *, cfg: dict, router=None) -> list[Row]:
    query = str(query or "").strip()
    if not query:
        raise ValueError("search needs a non-empty query")
    n = max(1, min(10, int(n or 5)))
    if not isinstance(cfg, dict):
        raise TypeError("web search configuration is required")
    name = provider_name(cfg)
    _LAST_SEARCH_METADATA.set({"provider": name, "result_count": 0})
    dispatch = {"variant1": _variant1, "searxng": _searxng,
                "brave-free": _brave, "tavily": _tavily, "ddgs": _ddg,
                "exa": _exa, "firecrawl": _firecrawl, "keenable": _keenable,
                "parallel": _parallel, "xai": _xai}
    rows = (await dispatch[name](query, n, cfg) if router is None
            else await dispatch[name](query, n, cfg, router))
    metadata = dict(_LAST_SEARCH_METADATA.get() or {})
    metadata.setdefault("provider", name)
    metadata["result_count"] = len(rows or [])
    _LAST_SEARCH_METADATA.set(metadata)
    return rows


def last_search_metadata() -> dict[str, Any]:
    return copy.deepcopy(_LAST_SEARCH_METADATA.get() or {})


async def _variant1(query: str, n: int, cfg: dict, _router=None) -> list[Row]:
    from web_search import search as metasearch
    rows, metadata = await metasearch.search_detailed(query, n, cfg=cfg)
    _LAST_SEARCH_METADATA.set(dict(metadata or {}))
    return rows


async def _searxng(query: str, n: int, cfg: dict, _router=None) -> list[Row]:
    block = _block(cfg, "searxng")
    base = str(block.get("base_url") or os.environ.get("VARIANT1_SEARXNG_URL")
               or os.environ.get("SEARXNG_URL") or "").strip().rstrip("/")
    parsed = urlparse(base)
    if not base or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("SearXNG base_url must be a complete http(s) URL")
    params = {"q": query, "format": "json", "language": str(block.get("language") or "en")}
    if str(block.get("engines") or "").strip():
        params["engines"] = str(block["engines"])
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True, trust_env=False) as client:
            response = await client.get(f"{base}/search", params=params, headers=_headers())
    except (httpx.TimeoutException, httpx.TransportError, OSError) as exc:
        raise RuntimeError(f"SearXNG is unreachable ({type(exc).__name__})") from exc
    if response.status_code != 200:
        raise RuntimeError(f"SearXNG returned HTTP {response.status_code}")
    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError("SearXNG response was not JSON") from exc
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise RuntimeError("SearXNG returned JSON without a results list")
    return _json_rows(results, n)


async def _brave(query: str, n: int, cfg: dict, router=None) -> list[Row]:
    key = await _api_key(cfg, "brave-free", router)
    if not key:
        raise RuntimeError("Brave Search needs an API key")
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True, trust_env=False) as client:
            response = await client.get("https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": n},
                headers={**_headers(), "X-Subscription-Token": key})
    except (httpx.TimeoutException, httpx.TransportError, OSError) as exc:
        raise RuntimeError(f"Brave Search request failed ({type(exc).__name__})") from exc
    if response.status_code != 200:
        raise RuntimeError(f"Brave Search returned HTTP {response.status_code}")
    data = response.json() if response.content else {}
    return _json_rows(((data.get("web") or {}).get("results") if isinstance(data, dict) else None), n)


async def _tavily(query: str, n: int, cfg: dict, router=None) -> list[Row]:
    block = _block(cfg, "tavily")
    key = await _api_key(cfg, "tavily", router)
    base = str(block.get("base_url") or os.environ.get("TAVILY_BASE_URL")
               or "https://api.tavily.com").rstrip("/")
    headers = {"X-Client-Name": "variant-1"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    else:
        headers["X-Tavily-Access-Mode"] = "keyless"
    data = await _post_json(f"{base}/search",
        payload={"query": query, "max_results": n, "include_raw_content": False,
                 "include_images": False}, headers=headers)
    return _json_rows(data.get("results"), n)


async def _ddg(query: str, n: int, _cfg: dict, _router=None) -> list[Row]:
    from web_search import search as metasearch
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True, trust_env=False) as client:
            return list(await metasearch.engine_ddg(query, n, client))
    except metasearch.EngineError as exc:
        raise RuntimeError(str(exc)) from exc


def _parse_mcp_text(body: str) -> str:
    payloads = [body.strip(), *[line[6:].strip() for line in body.splitlines()
                                if line.startswith("data: ")]]
    for payload in payloads:
        if not payload.startswith("{"):
            continue
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if data.get("error"):
            error = data["error"]
            raise RuntimeError(str(error.get("message") if isinstance(error, dict) else error))
        result = data.get("result") or {}
        content = result.get("content") or []
        texts = [str(item.get("text")) for item in content
                 if isinstance(item, dict) and item.get("text")]
        if result.get("isError"):
            raise RuntimeError(" ".join(texts) or "MCP tool call failed")
        if texts:
            return texts[0]
    raise RuntimeError("unrecognized MCP response")


async def _mcp_call(url: str, tool: str, arguments: dict) -> str:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": tool, "arguments": arguments}}
    try:
        async with httpx.AsyncClient(timeout=35, follow_redirects=True, trust_env=False) as client:
            response = await client.post(url, json=payload,
                headers={"Accept": "application/json, text/event-stream",
                         "User-Agent": "variant-1"})
    except (httpx.TimeoutException, httpx.TransportError, OSError) as exc:
        raise RuntimeError(f"keyless MCP request failed ({type(exc).__name__})") from exc
    if response.status_code >= 400:
        raise RuntimeError(f"keyless MCP returned HTTP {response.status_code}")
    return _parse_mcp_text(response.text)


def _parse_exa_text(text: str, n: int) -> list[Row]:
    rows: list[Row] = []
    for block in re.split(r"\n---\n", text):
        title, url, highlights, collecting = "", "", [], False
        for raw in block.splitlines():
            line = raw.strip()
            if line.startswith("Title:"):
                title, collecting = line[6:].strip(), False
            elif line.startswith("URL:"):
                url, collecting = line[4:].strip(), False
            elif line.startswith("Highlights:"):
                collecting = True
            elif line.startswith(("Published:", "Author:")):
                collecting = False
            elif collecting and line:
                highlights.append(line)
        if url:
            rows.append((title or url, url, _clip(" ".join(highlights))))
        if len(rows) >= n:
            break
    return rows


async def _exa(query: str, n: int, cfg: dict, router=None) -> list[Row]:
    key = await _api_key(cfg, "exa", router)
    if not key:
        text = await _mcp_call(_EXA_MCP_URL, "web_search_exa",
                               {"query": query, "numResults": n})
        _LAST_SEARCH_METADATA.set({"provider": "exa", "tier": "keyless"})
        return _parse_exa_text(text, n)
    data = await _post_json("https://api.exa.ai/search",
        payload={"query": query, "numResults": n,
                 "contents": {"highlights": {"maxCharacters": 1200}}},
        headers={"x-api-key": key, "x-exa-integration": "variant-1"})
    return _json_rows(data.get("results"), n)


async def _firecrawl(query: str, n: int, cfg: dict, router=None) -> list[Row]:
    block = _block(cfg, "firecrawl")
    key = await _api_key(cfg, "firecrawl", router)
    base = str(block.get("base_url") or os.environ.get("FIRECRAWL_API_URL")
               or "https://api.firecrawl.dev").rstrip("/")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = await _post_json(f"{base}/v2/search", payload={"query": query, "limit": n},
                            headers=headers, timeout=60)
    source = data.get("data")
    if isinstance(source, dict):
        source = source.get("web") or source.get("results")
    return _json_rows(source if isinstance(source, list) else data.get("results"), n)


async def _keenable(query: str, n: int, cfg: dict, router=None) -> list[Row]:
    key = await _api_key(cfg, "keenable", router)
    endpoint = "/v1/search" if key else "/v1/search/public"
    headers = {"X-Keenable-Title": "variant-1"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = await _post_json(f"https://api.keenable.ai{endpoint}",
        payload={"query": query, "max_results": n}, headers=headers)
    return _json_rows(data.get("results"), n)


async def _parallel(query: str, n: int, cfg: dict, router=None) -> list[Row]:
    key = await _api_key(cfg, "parallel", router)
    if not key:
        text = await _mcp_call(_PARALLEL_MCP_URL, "web_search",
            {"objective": query, "search_queries": [query],
             "session_id": _KEYLESS_SESSION_ID})
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Parallel MCP returned invalid JSON") from exc
        _LAST_SEARCH_METADATA.set({"provider": "parallel", "tier": "keyless"})
        return _json_rows(data.get("results"), n)
    data = await _post_json("https://api.parallel.ai/v1beta/search",
        payload={"objective": query, "search_queries": [query], "mode": "fast",
                 "max_results": n, "excerpts": {"max_chars_per_result": 1200}},
        headers={"x-api-key": key, "Content-Type": "application/json"}, timeout=60)
    return _json_rows(data.get("results"), n)


def _response_output_text(data: dict) -> str:
    parts: list[str] = []
    for item in data.get("output") or ():
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or ():
            if isinstance(content, dict) and content.get("type") == "output_text":
                parts.append(str(content.get("text") or ""))
    return "\n".join(parts).strip()


def _xai_rows(data: dict, n: int) -> list[Row]:
    text = _response_output_text(data)
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            rows = _json_rows(json.loads(match.group(0)).get("results"), n)
            if rows:
                return rows
        except Exception:
            pass
    urls = [str(value) for value in (data.get("citations") or ()) if value]
    for item in data.get("output") or ():
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or ():
            if isinstance(content, dict):
                urls.extend(str(annotation["url"]) for annotation in
                            (content.get("annotations") or ())
                            if isinstance(annotation, dict) and annotation.get("url"))
    seen: set[str] = set()
    rows: list[Row] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        rows.append((url, url, _clip(text)))
        if len(rows) >= n:
            break
    return rows


async def _xai(query: str, n: int, cfg: dict, router=None) -> list[Row]:
    from service_credentials import ServiceCredential, resolve
    default_base = "https://api.x.ai/v1"
    legacy = str(_block(cfg, "xai").get("api_key") or "").strip()
    credential = ServiceCredential(legacy, default_base, "legacy") if legacy else await resolve(
        router, "web", "xai", default_base_url=default_base, shared_provider="xai",
        env_vars=tuple(_BY_ID["xai"].get("env_vars") or ()),
    )
    key, base = credential.secret, credential.base_url
    if not key:
        raise RuntimeError("xAI Web Search needs a connected xAI account or API key")
    block = _block(cfg, "xai")
    model = str(block.get("model") or "grok-4.6")
    prompt = (f"Search the web for: {query}\nReturn JSON only with this shape: "
              f'{{"results":[{{"title":"...","url":"https://...","description":"..."}}]}}. '
              f"Return at most {n} results.")
    data = await _post_json(f"{base.rstrip('/')}/responses",
        payload={"model": model, "input": [{"role": "user", "content": prompt}],
                 "tools": [{"type": "web_search"}], "max_output_tokens": 2000},
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout=float(block.get("timeout") or 90))
    rows = _xai_rows(data, n)
    if not rows:
        raise RuntimeError("xAI Web Search returned no usable citations")
    return rows
