"""Explicit route recovery policy, scoped request budgets and cooldown evidence."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
import math
import threading
import time

from model_providers import ProviderRequestError


PROFILES = frozenset({"internal_json", "internal_prose", "vision"})


def normalize_recovery_config(raw=None) -> dict:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("provider_recovery must be an object")
    if not isinstance(raw.get("enabled", False), bool):
        raise ValueError("provider_recovery.enabled must be boolean")
    attempts = raw.get("max_attempts", 4)
    wait = raw.get("max_wait_seconds", 60)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 12:
        raise ValueError("provider recovery max_attempts must be 1..12")
    if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not math.isfinite(wait) or not 0 <= wait <= 600:
        raise ValueError("provider recovery max_wait_seconds must be 0..600")

    def routes(value):
        if not isinstance(value, list) or len(value) > 4:
            raise ValueError("recovery chains must contain at most four explicit routes")
        result = []
        for entry in value:
            if not isinstance(entry, dict) or set(entry) - {"mode", "provider", "model", "reasoning_effort"}:
                raise ValueError("recovery routes accept mode/provider/model/reasoning_effort only")
            mode = entry.get("mode", "cloud")
            provider = entry.get("provider", "local" if mode == "local" else "")
            model = entry.get("model", "")
            if (mode not in {"local", "cloud"} or not isinstance(provider, str) or not provider.strip()
                    or not isinstance(model, str) or not model.strip() or len(provider) > 200 or len(model) > 512
                    or (mode == "local" and provider != "local")):
                raise ValueError("recovery routes require explicit mode/provider/model")
            route = {"mode": mode, "provider": provider.strip(), "model": model.strip()}
            if "reasoning_effort" in entry:
                effort = entry["reasoning_effort"]
                if not isinstance(effort, str) or len(effort) > 32:
                    raise ValueError("recovery route effort must be a bounded string")
                route["reasoning_effort"] = effort
            if route not in result:
                result.append(route)
        return result

    auxiliary = raw.get("auxiliary_routes", {})
    if not isinstance(auxiliary, dict) or set(auxiliary) - PROFILES:
        raise ValueError("auxiliary_routes accepts named internal profiles only")
    return {"enabled": raw.get("enabled", False), "max_attempts": attempts,
            "max_wait_seconds": float(wait), "fallback_routes": routes(raw.get("fallback_routes", [])),
            "auxiliary_routes": {name: routes(chain) for name, chain in auxiliary.items()}}


def _absolute_reset(value):
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, str) and not value.replace(".", "", 1).isdigit():
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            value = parsed.timestamp()
        value = float(value)
        return value if math.isfinite(value) and value > time.time() else None
    except (TypeError, ValueError, OverflowError):
        return None


def annotate_provider_error(error, payload) -> None:
    """Keep only structured classification/reset metadata, never the error body."""
    if not isinstance(payload, dict):
        return
    body = payload.get("error") if isinstance(payload.get("error"), dict) else payload
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    error.provider_error_type = str(body.get("type") or metadata.get("error_type") or body.get("code") or "")[:100]
    error.provider_error_code = str(body.get("code") or "")[:100]
    # OpenRouter's shared upstream capacity is not this API key's quota.
    # BYOK and unclassified throttling retain conservative credential scope.
    provider = str(getattr(error, 'provider', '')).casefold()
    message = str(body.get('message') or '').casefold()
    nous_capacity = (
        provider == 'hermes' and getattr(error, 'status_code', 0) == 429
        and message.startswith('the requested model is temporarily at capacity upstream.')
        and "this is not your api key's rate limit" in message
    )
    error.model_specific_rate_limit = nous_capacity or (
        str(getattr(error,'provider','')).casefold() == 'openrouter'
        and str(body.get('code') or getattr(error,'status_code',0)) == '429'
        and str(body.get('message') or '').casefold() == 'provider returned error'
        and isinstance(metadata.get('provider_name'),str) and bool(metadata['provider_name'])
        and metadata.get('is_byok') is False
    )
    for source in (body, metadata):
        reset = _absolute_reset(source.get("reset_at") or source.get("resets_at"))
        if reset is not None:
            error.provider_reset_at = reset
            break
        seconds = source.get("resets_in_seconds")
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and math.isfinite(seconds) and seconds > 0:
            error.provider_reset_at = time.time() + seconds
            break


@dataclass(frozen=True)
class Failure:
    kind: str
    retryable: bool = False
    fallback: bool = False
    reset_at: float | None = None
    model_specific: bool = False


def classify_provider_error(error) -> Failure:
    codes = {str(getattr(error, name, "") or "").casefold().replace("-", "_")
             for name in ("provider_error_type", "provider_error_code")}
    codes.discard("")
    kind = str(getattr(error, "failure_kind", "") or "").casefold()
    reset = _absolute_reset(getattr(error, "provider_reset_at", None))
    wait = getattr(error, "retry_after_seconds", None)
    if reset is None and isinstance(wait, (int, float)) and math.isfinite(wait) and wait > 0:
        reset = time.time() + wait
    try:
        status = int(getattr(error, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    if kind in {"request_budget_exhausted", "unsupported_modality"}:
        return Failure(kind)
    if any(part in code for code in codes for part in ("content_filter", "safety", "refusal", "content_policy", "responsibleai")):
        return Failure("content_policy")
    if codes & {"context_length_exceeded", "context_window_exceeded", "input_too_long"} or status == 413:
        return Failure("context")
    quota_codes = {"insufficient_quota", "usage_limit_reached", "quota_exceeded", "billing_error", "payment_required"}
    detail = str(error).casefold() if not codes else ""
    if codes & quota_codes or status == 402 or any(marker in detail for marker in (
        "insufficient_quota", "usage_limit_reached", "monthly usage limit reached", "available balance", "out of budget", "quota exceeded",
        "gousagelimiterror", "freeusagelimiterror", "billing limit", "billing error")):
        return Failure("quota", fallback=True, reset_at=reset)
    if status == 429 and getattr(error,'model_specific_rate_limit',False):
        return Failure('rate_limit',retryable=True,fallback=True,reset_at=reset,model_specific=True)
    if codes & {"upstream_rate_limit", "upstream_blocked", "provider_policy_blocked"}:
        return Failure("upstream", fallback=True, reset_at=reset)
    if status in {401, 403}:
        return Failure("authentication", fallback=True)
    if status == 404 or codes & {"model_not_found", "model_not_available"}:
        return Failure("model_unavailable", fallback=True)
    if status == 429 or codes & {"rate_limit_error", "rate_limit_exceeded", "too_many_requests", "resource_exhausted"}:
        return Failure("rate_limit", retryable=True, fallback=True, reset_at=reset)
    if status in {408, 409} or status >= 500 or codes & {"overloaded_error", "server_error", "api_error", "timeout_error"}:
        return Failure("transient", retryable=True, fallback=True, reset_at=reset)
    return Failure("request")


@dataclass
class RequestBudget:
    max_attempts: int = 4
    max_wait_seconds: float = 60
    attempts: int = 0
    waited: float = 0
    candidate_attempts: int = 0
    candidate_limit: int = 12

    def begin_candidate(self, reserved_candidates: int = 0):
        self.candidate_attempts = 0
        self.candidate_limit = min(4, max(1, self.max_attempts - self.attempts - reserved_candidates))

    @property
    def has_capacity(self):
        return self.attempts < self.max_attempts and self.candidate_attempts < self.candidate_limit

    def consume(self, provider: str) -> None:
        if not self.has_capacity:
            error = ProviderRequestError(provider, "Provider recovery attempt budget exhausted")
            error.failure_kind = "request_budget_exhausted"
            raise error
        self.attempts += 1
        self.candidate_attempts += 1

    def admit_wait(self, seconds: float) -> bool:
        if not math.isfinite(seconds) or seconds < 0 or self.waited + seconds > self.max_wait_seconds:
            return False
        self.waited += seconds
        return True


REQUEST_BUDGET: ContextVar[RequestBudget | None] = ContextVar("variant1_provider_request_budget", default=None)


@dataclass
class RouteScope:
    primary: dict
    policy: dict
    allow_recovery: bool = True
    router: object = None
    effective: dict | None = None
    primary_identity: dict | None = None
    effective_identity: dict | None = None
    force_portable: bool = False
    restored_run_id: str = ""


ROUTE_SCOPE: ContextVar[RouteScope | None] = ContextVar("variant1_provider_route_scope", default=None)


class RecoveryCooldowns:
    def __init__(self):
        self._lock = threading.RLock()
        self._entries: dict[tuple, tuple[float, int]] = {}

    @staticmethod
    def key(router, route) -> tuple:
        base = ""
        if route.get("mode") == "cloud":
            base = router.provider_base_url(route.get("provider", ""))
        generation = getattr(router, "_recovery_credential_revisions", {}).get(route.get("provider"), 0)
        return (route.get("mode"), route.get("provider"), route.get("model"), base, generation)

    def remaining(self, key) -> float:
        with self._lock:
            return max(0.0, self._entries.get(key, (0, 0))[0] - time.monotonic())

    def failed(self, key, failure: Failure) -> None:
        if failure.kind not in {"rate_limit", "quota"} and failure.reset_at is None:
            return
        with self._lock:
            _, count = self._entries.get(key, (0, 0))
            delay = min(60 * 2 ** min(count, 8), 14400)
            if failure.reset_at is not None:
                delay = max(0.0, failure.reset_at - time.time())
            expiry = time.monotonic() + delay
            self._entries[key] = (expiry, count + 1)

    def succeeded(self, key) -> None:
        with self._lock:
            self._entries.pop(key, None)
    def clear_provider(self, provider):
        with self._lock:
            self._entries = {key: value for key, value in self._entries.items() if key[1] != provider}
