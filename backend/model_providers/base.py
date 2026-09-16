"""Model-provider profile contracts for VARIANT-1.

Profiles describe provider identity, endpoints, authentication, model defaults,
and small request quirks.  They deliberately do not own credential rotation or
HTTP streaming; those remain shared infrastructure in ``LLMRouter``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from copy import deepcopy
from typing import Any, Mapping


@dataclass(frozen=True)
class ProviderProfile:
    """Declarative description of one inference provider."""

    name: str
    display_name: str
    api_style: str = "openai"  # openai | anthropic | gemini
    aliases: tuple[str, ...] = ()
    base_url: str = ""
    models_url: str = ""
    env_vars: tuple[str, ...] = ()
    auth_style: str = "bearer"  # bearer | x-api-key | query | optional
    default_model: str = ""
    fallback_models: tuple[str, ...] = ()
    # Fail closed. Provider/model profiles must explicitly opt into image input;
    # arbitrary compatible endpoints and text-only routes must not receive it.
    supports_vision: bool = False
    supports_reasoning: bool = False
    reasoning_efforts: tuple[str, ...] = ()
    # Optional top-level field used by compatible chat-completions providers
    # that accept an effort string (for example ``reasoning_effort``). Empty
    # means the adapter must not invent a wire field.
    reasoning_effort_field: str = ""
    # Ordered, first-match model capabilities for per-call minimum reasoning.
    # Each rule declares patterns, optional supported efforts, and optional
    # minimum_fields (dotted native payload paths). None removes a conflicting
    # native field. Unknown models retain the profile's declared effort floor.
    reasoning_model_rules: tuple[Mapping[str, Any], ...] = ()
    signup_url: str = ""
    description: str = ""
    default_headers: Mapping[str, str] = field(default_factory=dict)
    request_defaults: Mapping[str, Any] = field(default_factory=dict)
    omit_temperature: bool = False
    # Declarative request-field compatibility. Transports resolve these once
    # before dispatch, mirroring Hermes's profile-driven provider layer.
    sampling_fields: tuple[str, ...] = ("temperature",)
    sampling_forbidden_model_patterns: tuple[str, ...] = ()
    completion_token_field: str = "max_tokens"  # max_tokens|max_completion_tokens|auto
    max_completion_token_model_patterns: tuple[str, ...] = ()
    structured_output_style: str = ""
    # One provider-neutral cache identity is resolved by the router. Profiles
    # only declare a native wire projection when their protocol supports one;
    # empty values mean the provider/runtime uses automatic prefix caching.
    prompt_cache_body_field: str = ""
    prompt_cache_header: str = ""

    def with_overrides(self, values: Mapping[str, Any] | None) -> "ProviderProfile":
        """Apply safe, declarative per-install overrides from config.

        Code hooks are intentionally not configurable here.  A user plugin that
        needs code registers its own profile through ``register(registry)``.
        """
        if not isinstance(values, Mapping):
            return self
        allowed = {
            "display_name", "api_style", "aliases", "base_url", "models_url",
            "env_vars", "auth_style", "default_model", "fallback_models",
            "supports_vision", "supports_reasoning", "reasoning_efforts",
            "reasoning_effort_field", "reasoning_model_rules",
            "signup_url", "description",
            "default_headers", "request_defaults", "omit_temperature",
            "sampling_fields", "sampling_forbidden_model_patterns",
            "completion_token_field", "max_completion_token_model_patterns",
            "structured_output_style",
            "prompt_cache_body_field", "prompt_cache_header",
        }
        updates = {key: values[key] for key in allowed if key in values}
        if "reasoning_model_rules" in updates:
            updates["reasoning_model_rules"] = tuple(
                deepcopy(dict(rule)) for rule in (updates["reasoning_model_rules"] or ())
                if isinstance(rule, Mapping)
            )
        for key in (
            "aliases", "env_vars", "fallback_models", "reasoning_efforts", "sampling_fields",
            "sampling_forbidden_model_patterns",
            "max_completion_token_model_patterns",
        ):
            if key in updates and isinstance(updates[key], list):
                updates[key] = tuple(str(item) for item in updates[key])
        return replace(self, **updates)

    def public_dict(self, *, configured: bool = False, credential_count: int = 0,
                    model: str = "", base_url: str = "") -> dict:
        """Return metadata safe to expose to renderer and gateway clients."""
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "api_style": self.api_style,
            "auth_style": self.auth_style,
            "base_url": base_url or self.base_url,
            "models_url": self.models_url,
            "default_model": self.default_model,
            "fallback_models": list(self.fallback_models),
            "supports_vision": self.supports_vision,
            "supports_reasoning": self.supports_reasoning,
            "reasoning_efforts": list(self.reasoning_efforts),
            "reasoning_effort_field": self.reasoning_effort_field,
            "signup_url": self.signup_url,
            "configured": bool(configured),
            "credential_count": int(credential_count),
            "model": model or self.default_model,
        }
