"""Usage observation helpers for LLM routing (metadata-only receipts).

Separated from ``LLMRouter`` so inference dispatch does not own token-path
normalization or the ContextVar usage observer.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar


_USAGE_OBSERVER = ContextVar("variant1_llm_usage_observer", default=None)
_USAGE_CATEGORY = ContextVar("variant1_llm_usage_category", default="agent")
_MAX_RECEIPT_TOKEN_COUNT = 1_000_000_000_000


def usage_token(value):
    """Return one bounded non-negative token count, never arbitrary content."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return min(_MAX_RECEIPT_TOKEN_COUNT, max(0, number))


def usage_path(raw_usage: dict, *path: str):
    current = raw_usage
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None, False
        current = current[key]
    number = usage_token(current)
    return number, number is not None


def first_usage_path(raw_usage: dict, paths):
    for path in paths:
        value, present = usage_path(raw_usage, *path)
        if present:
            return value, True
    return None, False


def normalize_manifest_usage(
    provider: str,
    prompt_tokens=0,
    completion_tokens=0,
    total_tokens=None,
    *,
    raw_usage=None,
) -> dict:
    """Reduce provider usage to an allowlisted, metadata-only receipt block."""
    provider_key = str(provider or "").strip().lower()
    raw = raw_usage if isinstance(raw_usage, dict) else {}
    input_tokens, input_reported = first_usage_path(raw, (
        ("input_tokens",),
        ("prompt_tokens",),
        ("promptTokenCount",),
    ))
    output_tokens, output_reported = first_usage_path(raw, (
        ("output_tokens",),
        ("completion_tokens",),
        ("candidatesTokenCount",),
    ))
    reported_total, total_reported = first_usage_path(raw, (
        ("total_tokens",),
        ("totalTokenCount",),
    ))
    cached_tokens, cached_reported = first_usage_path(raw, (
        ("input_tokens_details", "cached_tokens"),
        ("prompt_tokens_details", "cached_tokens"),
        ("cache_read_input_tokens",),
        ("cachedContentTokenCount",),
        ("cached_input_tokens",),
        ("cached_prompt_tokens",),
        ("cached_tokens",),
    ))
    reasoning_tokens, reasoning_reported = first_usage_path(raw, (
        ("output_tokens_details", "reasoning_tokens"),
        ("completion_tokens_details", "reasoning_tokens"),
        ("reasoning_tokens",),
        ("thoughtsTokenCount",),
    ))
    cache_write_tokens, cache_write_reported = first_usage_path(raw, (
        ("input_tokens_details", "cache_write_tokens"),
        ("prompt_tokens_details", "cache_write_tokens"),
        ("cache_creation_input_tokens",),
        ("cache_write_input_tokens",),
        ("cache_write_tokens",),
    ))
    tool_prompt_tokens, tool_prompt_reported = first_usage_path(raw, (
        ("toolUsePromptTokenCount",),
        ("tool_prompt_tokens",),
        ("tool_use_prompt_tokens",),
    ))

    fallback_used = False
    if input_tokens is None:
        input_tokens = usage_token(prompt_tokens)
        if input_tokens is None:
            input_tokens = 0
        fallback_used = True
    if output_tokens is None:
        output_tokens = usage_token(completion_tokens)
        if output_tokens is None:
            output_tokens = 0
        fallback_used = True
    if reported_total is None:
        fallback_total = usage_token(total_tokens)
        if fallback_total is not None:
            reported_total = fallback_total
            fallback_used = True
        else:
            reported_total = min(
                _MAX_RECEIPT_TOKEN_COUNT, input_tokens + output_tokens)
            # Deriving total from two provider-reported components is exact;
            # deriving it from either fallback component remains estimated.
            fallback_used = fallback_used or not (
                input_reported and output_reported)

    provider_reported = any((
        input_reported, output_reported, total_reported, cached_reported,
        reasoning_reported, cache_write_reported, tool_prompt_reported,
    ))
    measurement = (
        "mixed" if provider_reported and fallback_used
        else "provider_reported" if provider_reported
        else "estimated"
    )
    cached = int(cached_tokens or 0)
    cache_write = int(cache_write_tokens or 0)
    # Anthropic reports ordinary input, cache reads, and cache writes as
    # disjoint buckets. Responses/OpenAI-compatible and Gemini usage reports
    # include cached tokens in their input total. Preserve the provider's raw
    # input field above, but also expose one provider-neutral prompt split for
    # session accounting and cache-share comparisons.
    split_cache_buckets = (
        provider_key in {"anthropic", "claude"}
        or "cache_read_input_tokens" in raw
        or "cache_creation_input_tokens" in raw
    )
    if split_cache_buckets:
        prompt_token_volume = min(
            _MAX_RECEIPT_TOKEN_COUNT,
            input_tokens + cached + cache_write,
        )
        uncached_input_tokens = min(
            _MAX_RECEIPT_TOKEN_COUNT,
            input_tokens + cache_write,
        )
    else:
        prompt_token_volume = input_tokens
        # Cache creation is still uncached work on inclusive-input providers;
        # subtract cache reads only.
        uncached_input_tokens = max(0, input_tokens - cached)
    cache_share = (
        round(cached / prompt_token_volume, 8)
        if prompt_token_volume > 0 else 0.0
    )
    token_volume = min(
        _MAX_RECEIPT_TOKEN_COUNT,
        max(reported_total, prompt_token_volume + output_tokens),
    )
    return {
        "measurement": measurement,
        "provider_reported": provider_reported,
        "estimated": fallback_used or not provider_reported,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": reported_total,
        "cached_input_tokens": cached_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cache_write_input_tokens": cache_write_tokens,
        "tool_prompt_tokens": tool_prompt_tokens,
        "prompt_token_volume": prompt_token_volume,
        "uncached_input_tokens": uncached_input_tokens,
        "cache_share": cache_share,
        "token_volume": token_volume,
    }


@contextmanager
def observe_usage(callback):
    """Bind a run-scoped usage sink; contextvars isolate concurrent tasks."""
    token = _USAGE_OBSERVER.set(callback)
    try:
        yield
    finally:
        _USAGE_OBSERVER.reset(token)


def current_usage_observer():
    return _USAGE_OBSERVER.get()


@contextmanager
def observe_usage_category(category: str):
    """Label LLM usage by runtime role without changing model behavior."""
    value = str(category or "agent").strip().lower() or "agent"
    token = _USAGE_CATEGORY.set(value)
    try:
        yield
    finally:
        _USAGE_CATEGORY.reset(token)


def current_usage_category() -> str:
    return str(_USAGE_CATEGORY.get() or "agent")
