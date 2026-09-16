"""Free in-process metasearch for VARIANT-1 web tools.

The provider fans out to a small fixed set of public engines, applies bounded
retries and per-engine circuit breakers, fuses ranked results, and publishes a
compact non-secret health snapshot. It remains intentionally smaller than
SearXNG and never falls back to another provider or browser transport.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import html as html_lib
import random
import re
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from html.parser import HTMLParser
from typing import Any
from urllib.parse import (
    parse_qs,
    parse_qsl,
    unquote,
    urlencode,
    urlsplit,
    urlunsplit,
)

import httpx

# Public provider row: (title, url, snippet). Metadata stays internal so the
# model-facing web_search schema and provider adapter remain small.
Row = tuple[str, str, str]

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_ENGINES = ("ddg", "bing")
KNOWN_ENGINES = frozenset(DEFAULT_ENGINES)

MAX_QUERY_CHARS = 500
ENGINE_ATTEMPTS = 2
ENGINE_ATTEMPT_TIMEOUT_SECONDS = 6.0
CIRCUIT_FAILURE_THRESHOLD = 2
CIRCUIT_COOLDOWN_SECONDS = 180.0
CACHE_TTL_SECONDS = 90.0
CACHE_MAX_ENTRIES = 128
RRF_K = 60.0
MAX_RESULTS_PER_HOST = 2
_TRANSIENT_HTTP = frozenset({429, 500, 502, 503, 504})
_ENGINE_WEIGHTS = {"ddg": 1.0, "bing": 0.95}
_TRACKING_KEYS = frozenset({
    "fbclid", "gclid", "msclkid", "dclid", "yclid", "mc_cid", "mc_eid",
    "ved", "ei", "sa", "sourceid",
})


class EngineError(RuntimeError):
    """Typed engine failure used by retry, health, and error reporting."""

    def __init__(self, kind: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.kind = str(kind or "engine_error")
        self.retryable = bool(retryable)


def _blank_health() -> dict[str, Any]:
    return {
        "status": "unknown",
        "last_latency_ms": 0,
        "last_error": "",
        "last_checked_at": 0.0,
        "last_success_at": 0.0,
        "consecutive_failures": 0,
        "circuit_open_until": 0.0,
    }


_ENGINE_HEALTH: dict[str, dict[str, Any]] = {
    name: _blank_health() for name in DEFAULT_ENGINES
}
_LAST_STATUS: dict[str, Any] = {
    "checked": False,
    "healthy_engines": 0,
    "engine_count": len(DEFAULT_ENGINES),
    "degraded": False,
    "last_latency_ms": 0,
    "last_run_at": 0.0,
    "cache_hit": False,
    "coalesced": False,
    "last_error": "",
    "failed_engines": [],
    "result_count": 0,
}
_CACHE: OrderedDict[tuple, tuple[float, list[Row], dict[str, Any]]] = OrderedDict()
_INFLIGHT: dict[tuple, asyncio.Task] = {}


def _headers() -> dict[str, str]:
    return {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


def _clip(s: str, n: int = 400) -> str:
    text = " ".join((s or "").split())
    return text if len(text) <= n else text[: n - 1] + "..."


def _clean_html(s: str) -> str:
    text = re.sub(r"<[^>]+>", " ", s or "")
    return _clip(html_lib.unescape(text), 500)


def _attrs(attrs) -> dict[str, str]:
    return {str(key or "").lower(): str(value or "") for key, value in attrs}


def _classes(attrs: dict[str, str]) -> set[str]:
    return {part for part in attrs.get("class", "").split() if part}


class _DdgResultParser(HTMLParser):
    """Tolerant DDG result parser independent of attribute order/quote style."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._title_depth = 0
        self._snippet_depth = 0
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []
        self._href = ""
        self._snippet_row = -1

    def handle_starttag(self, tag: str, attrs) -> None:
        values = _attrs(attrs)
        classes = _classes(values)
        if self._title_depth:
            self._title_depth += 1
        elif tag.lower() == "a" and "result__a" in classes:
            self._title_depth = 1
            self._title_parts = []
            self._href = values.get("href", "")

        if self._snippet_depth:
            self._snippet_depth += 1
        elif "result__snippet" in classes:
            self._snippet_depth = 1
            self._snippet_parts = []
            self._snippet_row = len(self.rows) - 1

    def handle_endtag(self, tag: str) -> None:
        if self._title_depth:
            self._title_depth -= 1
            if self._title_depth == 0:
                title = _clean_html(" ".join(self._title_parts))
                link = normalize_url(self._href)
                if title and link:
                    self.rows.append([title, link, ""])
        if self._snippet_depth:
            self._snippet_depth -= 1
            if self._snippet_depth == 0 and 0 <= self._snippet_row < len(self.rows):
                self.rows[self._snippet_row][2] = _clean_html(
                    " ".join(self._snippet_parts)
                )

    def handle_data(self, data: str) -> None:
        if self._title_depth:
            self._title_parts.append(data)
        if self._snippet_depth:
            self._snippet_parts.append(data)


def normalize_url(link: str) -> str:
    """Unwrap known redirects and accept only absolute HTTP(S) result URLs."""
    link = html_lib.unescape((link or "").strip())
    if not link:
        return ""
    if link.startswith("//"):
        link = "https:" + link
    try:
        parts = urlsplit(link)
        host = (parts.hostname or "").lower()
        if host.endswith("duckduckgo.com") and (
            "/l/" in (parts.path or "") or parts.path in {"/l", "/l/"}
        ):
            query = parse_qs(parts.query or "")
            for key in ("uddg", "u"):
                values = query.get(key) or []
                if values and values[0]:
                    link = unquote(values[0]).strip()
                    parts = urlsplit(link)
                    host = (parts.hostname or "").lower()
                    break
        if host.endswith("bing.com") and "/ck/a" in (parts.path or ""):
            query = parse_qs(parts.query or "")
            for key in ("u", "r"):
                values = query.get(key) or []
                if not values:
                    continue
                raw = unquote(values[0]).strip()
                if raw.startswith("a1"):
                    try:
                        encoded = raw[2:] + "=" * (-len(raw[2:]) % 4)
                        decoded = base64.urlsafe_b64decode(encoded).decode(
                            "utf-8", errors="ignore"
                        )
                        if decoded.startswith(("http://", "https://")):
                            raw = decoded
                    except Exception:
                        pass
                if raw.startswith(("http://", "https://")):
                    link = raw
                    parts = urlsplit(link)
                    break
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return ""
        return urlunsplit((
            parts.scheme.lower(), parts.netloc, parts.path or "/", parts.query, "",
        ))
    except Exception:
        return ""


def _is_tracking_key(key: str) -> bool:
    low = str(key or "").lower()
    return low.startswith("utm_") or low in _TRACKING_KEYS


def url_key(link: str) -> str:
    """Canonical identity preserving meaningful query parameters.

    Tracking-only parameters and fragments are removed, but identity-bearing
    values such as YouTube's ``v`` parameter remain part of the key.
    """
    normalized = normalize_url(link)
    if not normalized:
        return ""
    try:
        parts = urlsplit(normalized)
        host = (parts.hostname or "").lower()
        port = parts.port
        if port and not (
            (parts.scheme == "https" and port == 443)
            or (parts.scheme == "http" and port == 80)
        ):
            host = f"{host}:{port}"
        path = (parts.path or "/").rstrip("/") or "/"
        query_items = sorted(
            (key, value) for key, value in parse_qsl(
                parts.query or "", keep_blank_values=True
            ) if not _is_tracking_key(key)
        )
        query = urlencode(query_items, doseq=True)
        return f"{host}{path}" + (f"?{query}" if query else "")
    except Exception:
        return normalized.lower()


def parse_ddg_html(body: str, n: int) -> list[Row]:
    parser = _DdgResultParser()
    try:
        parser.feed(body or "")
        parser.close()
    except Exception:
        return []
    return [tuple(row) for row in parser.rows[: max(1, int(n or 5))]]


def parse_bing_rss(body: str, n: int) -> list[Row]:
    """Parse Bing's public RSS result surface without HTML-layout coupling."""
    try:
        root = ET.fromstring(body or "")
    except ET.ParseError as exc:
        raise EngineError("invalid_response", "bing returned invalid RSS XML") from exc
    if root.tag.rsplit("}", 1)[-1].lower() != "rss":
        raise EngineError("invalid_response", "bing returned a non-RSS response")
    rows: list[Row] = []
    limit = max(1, int(n or 5))
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1].lower() != "item":
            continue
        fields: dict[str, str] = {}
        for child in item:
            key = child.tag.rsplit("}", 1)[-1].lower()
            fields[key] = "".join(child.itertext())
        title = _clean_html(fields.get("title", ""))
        link = normalize_url(fields.get("link", ""))
        snippet = _clean_html(fields.get("description", ""))
        if title and link:
            rows.append((title, link, snippet))
        if len(rows) >= limit:
            break
    return rows


def _validate_html_result_page(engine: str, body: str, rows: list[Row]) -> None:
    if rows:
        return
    low = (body or "").lower()
    blocked_markers = (
        "captcha", "unusual traffic", "verify you are human", "access denied",
        "automated queries", "challenge-form", "cf-chl-",
    )
    if any(marker in low for marker in blocked_markers) or len(body or "") < 200:
        raise EngineError("blocked", f"{engine} returned a blocked/challenge page")
    no_result_markers = (
        "no results found", "did not match any results", "b_no",
        "result--no-result",
    )
    if any(marker in low for marker in no_result_markers):
        return
    raise EngineError(
        "parse_drift",
        f"{engine} returned HTML but no recognized result markup",
    )


async def _fetch(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    data: dict | None = None,
    params: dict | None = None,
) -> httpx.Response:
    if method.upper() == "POST":
        return await client.post(url, data=data or {}, headers=_headers())
    return await client.get(url, params=params, headers=_headers())


def _http_error(engine: str, status: int) -> EngineError:
    return EngineError(
        "rate_limited" if status == 429 else "http_error",
        f"{engine} HTTP {status}",
        retryable=status in _TRANSIENT_HTTP,
    )


async def engine_ddg(query: str, n: int, client: httpx.AsyncClient) -> list[Row]:
    response = await _fetch(
        client, "POST", "https://html.duckduckgo.com/html/", data={"q": query},
    )
    if response.status_code != 200:
        raise _http_error("ddg", response.status_code)
    rows = parse_ddg_html(response.text, n)
    _validate_html_result_page("ddg", response.text, rows)
    return rows


async def engine_bing(query: str, n: int, client: httpx.AsyncClient) -> list[Row]:
    response = await _fetch(
        client,
        "GET",
        "https://www.bing.com/search",
        params={"q": query, "format": "rss", "setlang": "en", "cc": "US"},
    )
    if response.status_code != 200:
        raise _http_error("bing", response.status_code)
    return parse_bing_rss(response.text, n)


_ENGINE_FNS = {
    "ddg": engine_ddg,
    "bing": engine_bing,
}


def resolve_engines(cfg: dict | None = None) -> list[str]:
    block: dict[str, Any] = {}
    configured = False
    if isinstance(cfg, dict):
        raw = cfg.get("variant1") if isinstance(cfg.get("variant1"), dict) else cfg
        if isinstance(raw, dict) and raw.get("engines") is not None:
            block = raw
            configured = True
    engines_raw = block.get("engines") if configured else None
    if isinstance(engines_raw, str):
        names = [part.strip().lower() for part in engines_raw.split(",") if part.strip()]
    elif isinstance(engines_raw, (list, tuple)):
        names = [str(part).strip().lower() for part in engines_raw if str(part).strip()]
    elif configured:
        raise ValueError("VARIANT-1 Search engines must be a list or comma-separated string")
    else:
        names = list(DEFAULT_ENGINES)
    unknown = sorted({name for name in names if name not in KNOWN_ENGINES})
    if unknown:
        raise ValueError("unknown VARIANT-1 Search engine(s): " + ", ".join(unknown))
    if not names:
        raise ValueError("VARIANT-1 Search needs at least one configured engine")
    # Stable dedupe without changing configured priority.
    return list(dict.fromkeys(names))


def _fuse_rows(
    engine_batches: dict[str, list[Row]], n: int,
) -> tuple[list[Row], list[dict[str, Any]]]:
    """Weighted reciprocal-rank fusion with URL and host diversity."""
    records: dict[str, dict[str, Any]] = {}
    sequence = 0
    for engine_index, (engine, batch) in enumerate(engine_batches.items()):
        weight = float(_ENGINE_WEIGHTS.get(engine, 1.0))
        for rank, row in enumerate(batch or (), 1):
            if not isinstance(row, (list, tuple)) or len(row) < 3:
                continue
            title, link, snippet = str(row[0]), normalize_url(str(row[1])), str(row[2])
            key = url_key(link)
            if not key:
                continue
            record = records.get(key)
            if record is None:
                record = {
                    "title": title or link,
                    "url": link,
                    "snippet": _clip(snippet),
                    "score": 0.0,
                    "best_rank": rank,
                    "engine_index": engine_index,
                    "sequence": sequence,
                    "sources": [],
                    "host": (urlsplit(link).hostname or "").lower(),
                }
                records[key] = record
                sequence += 1
            record["score"] += weight / (RRF_K + rank)
            record["best_rank"] = min(record["best_rank"], rank)
            if engine not in record["sources"]:
                record["sources"].append(engine)
            # Prefer a more informative snippet from corroborating engines.
            if len(_clip(snippet)) > len(record["snippet"]):
                record["snippet"] = _clip(snippet)

    ranked = sorted(
        records.values(),
        key=lambda row: (
            -row["score"], row["best_rank"], row["engine_index"], row["sequence"],
        ),
    )
    selected: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    host_counts: dict[str, int] = {}
    for record in ranked:
        host = record["host"]
        if host and host_counts.get(host, 0) >= MAX_RESULTS_PER_HOST:
            deferred.append(record)
            continue
        selected.append(record)
        if host:
            host_counts[host] = host_counts.get(host, 0) + 1
        if len(selected) >= n:
            break
    if len(selected) < n:
        for record in deferred:
            selected.append(record)
            if len(selected) >= n:
                break
    rows = [
        (record["title"], record["url"], record["snippet"])
        for record in selected[:n]
    ]
    provenance = [
        {"url": record["url"], "sources": list(record["sources"])}
        for record in selected[:n]
    ]
    return rows, provenance


def _health_for(name: str) -> dict[str, Any]:
    return _ENGINE_HEALTH.setdefault(name, _blank_health())


def _mark_engine_success(name: str, *, rows: int, latency_ms: int) -> None:
    now = time.time()
    health = _health_for(name)
    health.update({
        "status": "healthy" if rows else "empty",
        "last_latency_ms": int(latency_ms),
        "last_error": "",
        "last_checked_at": now,
        "last_success_at": now,
        "consecutive_failures": 0,
        "circuit_open_until": 0.0,
    })


def _mark_engine_failure(name: str, *, error: str, latency_ms: int) -> None:
    now = time.time()
    health = _health_for(name)
    failures = int(health.get("consecutive_failures") or 0) + 1
    open_until = 0.0
    if failures >= CIRCUIT_FAILURE_THRESHOLD:
        open_until = now + CIRCUIT_COOLDOWN_SECONDS
    health.update({
        "status": "circuit_open" if open_until else "failed",
        "last_latency_ms": int(latency_ms),
        "last_error": _clip(error, 240),
        "last_checked_at": now,
        "consecutive_failures": failures,
        "circuit_open_until": open_until,
    })


def _circuit_is_open(name: str) -> bool:
    health = _health_for(name)
    until = float(health.get("circuit_open_until") or 0.0)
    if until <= time.time():
        if until:
            health["circuit_open_until"] = 0.0
            health["status"] = "retrying"
        return False
    return True


def _classify_exception(exc: BaseException) -> tuple[str, str, bool]:
    if isinstance(exc, EngineError):
        return exc.kind, str(exc), exc.retryable
    if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)):
        return "timeout", "engine request timed out", True
    if isinstance(exc, (httpx.TransportError, OSError)):
        return "transport_error", f"{type(exc).__name__}: {exc}", True
    return "engine_error", f"{type(exc).__name__}: {exc}", False


async def _run_engine(
    name: str, query: str, n: int, client: httpx.AsyncClient,
) -> dict[str, Any]:
    if _circuit_is_open(name):
        health = _health_for(name)
        return {
            "name": name,
            "status": "circuit_open",
            "rows": [],
            "attempts": 0,
            "latency_ms": 0,
            "error": health.get("last_error") or "circuit is cooling down",
        }
    function = _ENGINE_FNS.get(name)
    if function is None:
        return {
            "name": name,
            "status": "config_error",
            "rows": [],
            "attempts": 0,
            "latency_ms": 0,
            "error": "unknown engine",
        }
    started = time.perf_counter()
    last_kind = "engine_error"
    last_error = "engine failed"
    for attempt in range(1, ENGINE_ATTEMPTS + 1):
        try:
            rows = await asyncio.wait_for(
                function(query, n, client),
                timeout=ENGINE_ATTEMPT_TIMEOUT_SECONDS,
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            rows = list(rows or [])
            _mark_engine_success(name, rows=len(rows), latency_ms=latency_ms)
            return {
                "name": name,
                "status": "healthy" if rows else "empty",
                "rows": rows,
                "attempts": attempt,
                "latency_ms": latency_ms,
                "error": "",
            }
        except Exception as exc:
            last_kind, last_error, retryable = _classify_exception(exc)
            if retryable and attempt < ENGINE_ATTEMPTS:
                await asyncio.sleep(0.2 * attempt + random.uniform(0.0, 0.08))
                continue
            break
    latency_ms = int((time.perf_counter() - started) * 1000)
    message = f"{last_kind}: {last_error}"
    _mark_engine_failure(name, error=message, latency_ms=latency_ms)
    return {
        "name": name,
        "status": last_kind,
        "rows": [],
        "attempts": min(ENGINE_ATTEMPTS, attempt),
        "latency_ms": latency_ms,
        "error": _clip(message, 240),
    }


def _cache_key(query: str, n: int, engines: list[str]) -> tuple:
    normalized_query = " ".join(query.split()).casefold()
    return normalized_query, int(n), tuple(engines)


def _cache_get(key: tuple) -> tuple[list[Row], dict[str, Any]] | None:
    item = _CACHE.get(key)
    if item is None:
        return None
    expires_at, rows, metadata = item
    if expires_at <= time.time():
        _CACHE.pop(key, None)
        return None
    _CACHE.move_to_end(key)
    out_meta = copy.deepcopy(metadata)
    out_meta["cache_hit"] = True
    out_meta["coalesced"] = False
    # Report this lookup, not the latency/timestamp of the request that
    # originally populated the cache. Engine health retains the live request
    # timings separately.
    out_meta["last_latency_ms"] = 0
    out_meta["last_run_at"] = time.time()
    return list(rows), out_meta


def _cache_put(key: tuple, rows: list[Row], metadata: dict[str, Any]) -> None:
    if not rows:
        return
    _CACHE[key] = (
        time.time() + CACHE_TTL_SECONDS,
        list(rows),
        copy.deepcopy(metadata),
    )
    _CACHE.move_to_end(key)
    while len(_CACHE) > CACHE_MAX_ENTRIES:
        _CACHE.popitem(last=False)


def _set_last_status(metadata: dict[str, Any]) -> None:
    for key in tuple(_LAST_STATUS):
        if key in metadata:
            _LAST_STATUS[key] = copy.deepcopy(metadata[key])


async def _search_uncached(
    query: str, n: int, engines: list[str], key: tuple,
) -> tuple[list[Row], dict[str, Any]]:
    started = time.perf_counter()
    timeout = httpx.Timeout(ENGINE_ATTEMPT_TIMEOUT_SECONDS, connect=4.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=False) as client:
        outcomes = await asyncio.gather(*[
            _run_engine(name, query, min(10, n + 3), client) for name in engines
        ])
    batches = {
        outcome["name"]: list(outcome.get("rows") or [])
        for outcome in outcomes if outcome.get("rows") is not None
    }
    rows, provenance = _fuse_rows(batches, n)
    healthy_statuses = {"healthy", "empty"}
    healthy = [
        outcome["name"] for outcome in outcomes
        if outcome.get("status") in healthy_statuses
    ]
    failed = [
        outcome["name"] for outcome in outcomes
        if outcome.get("status") not in healthy_statuses
    ]
    errors = [
        f"{outcome['name']}: {outcome.get('error')}"
        for outcome in outcomes if outcome.get("error")
    ]
    metadata = {
        "checked": True,
        "provider": "variant1",
        "healthy_engines": len(healthy),
        "engine_count": len(engines),
        "degraded": bool(failed),
        "last_latency_ms": int((time.perf_counter() - started) * 1000),
        "last_run_at": time.time(),
        "cache_hit": False,
        "coalesced": False,
        "last_error": _clip("; ".join(errors), 500),
        "failed_engines": failed,
        "result_count": len(rows),
        "result_sources": provenance,
        "outcomes": [
            {
                "name": outcome["name"],
                "status": outcome["status"],
                "attempts": outcome["attempts"],
                "latency_ms": outcome["latency_ms"],
                "error": outcome["error"],
            }
            for outcome in outcomes
        ],
    }
    _set_last_status(metadata)
    if rows:
        _cache_put(key, rows, metadata)
        return rows, metadata
    if not healthy:
        raise RuntimeError(
            "VARIANT-1 Search: all engines failed ("
            + ("; ".join(errors[:4]) or "no engine completed")
            + "). Try again after the engine cooldown, or select another provider."
        )
    if failed:
        raise RuntimeError(
            "VARIANT-1 Search: no results and some engines failed ("
            + "; ".join(errors[:4])
            + ")."
        )
    return [], metadata


def _validated_query(query: str) -> str:
    value = " ".join((query or "").strip().split())
    if not value:
        raise ValueError("search needs a non-empty query")
    if len(value) > MAX_QUERY_CHARS:
        raise ValueError(
            f"search query is too long ({len(value)} characters; maximum {MAX_QUERY_CHARS})"
        )
    return value


async def search_detailed(
    query: str, n: int = 5, *, cfg: dict | None = None,
) -> tuple[list[Row], dict[str, Any]]:
    """Search with compact runtime metadata; coalesce identical live requests."""
    value = _validated_query(query)
    n = max(1, min(10, int(n or 5)))
    try:
        engines = resolve_engines(cfg)
    except ValueError as exc:
        metadata = {
            "checked": True,
            "provider": "variant1",
            "healthy_engines": 0,
            "engine_count": 0,
            "degraded": True,
            "last_latency_ms": 0,
            "last_run_at": time.time(),
            "cache_hit": False,
            "coalesced": False,
            "last_error": str(exc),
            "failed_engines": [],
            "result_count": 0,
        }
        _set_last_status(metadata)
        raise
    key = _cache_key(value, n, engines)
    cached = _cache_get(key)
    if cached is not None:
        rows, metadata = cached
        _set_last_status(metadata)
        return rows, metadata

    task = _INFLIGHT.get(key)
    coalesced = task is not None
    if task is None:
        task = asyncio.create_task(_search_uncached(value, n, engines, key))
        _INFLIGHT[key] = task

        def _clear(done: asyncio.Task, *, cache_key=key) -> None:
            if _INFLIGHT.get(cache_key) is done:
                _INFLIGHT.pop(cache_key, None)

        task.add_done_callback(_clear)
    rows, metadata = await asyncio.shield(task)
    metadata = copy.deepcopy(metadata)
    metadata["coalesced"] = coalesced
    _set_last_status(metadata)
    return list(rows), metadata


async def search(query: str, n: int = 5, *, cfg: dict | None = None) -> list[Row]:
    rows, _ = await search_detailed(query, n, cfg=cfg)
    return rows


def public_status(cfg: dict | None = None) -> dict[str, Any]:
    config_error = ""
    try:
        engines = resolve_engines(cfg)
    except ValueError as exc:
        engines = []
        config_error = str(exc)
    now = time.time()
    engine_status: dict[str, dict[str, Any]] = {}
    for name in engines or DEFAULT_ENGINES:
        health = copy.deepcopy(_health_for(name))
        if float(health.get("circuit_open_until") or 0.0) > now:
            health["status"] = "circuit_open"
        health["last_error"] = _clip(str(health.get("last_error") or ""), 240)
        engine_status[name] = health
    last = copy.deepcopy(_LAST_STATUS)
    if config_error:
        last.update({"degraded": True, "last_error": config_error})
    return {
        "engines": engines,
        "docker_required": False,
        "api_key_required": False,
        "mode": "in_process",
        **last,
        "engine_status": engine_status,
        "cache_ttl_seconds": int(CACHE_TTL_SECONDS),
    }
