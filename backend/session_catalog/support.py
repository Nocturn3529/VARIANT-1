"""Fail-closed provider/model qualification for the pinned Python surface.

The disclosure profile belongs to the durable chat.  A model route may be
rejected, but it may never cause the chat to receive a different tool topology.
"""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
from typing import Any, Iterable

from .profiles import (
    ACTION_SURFACE,
    IPYTHON_SCHEMA_REVISION,
    canonical_action_surface,
    is_action_surface,
)

QUALIFIED_STATUSES = frozenset({"qualified", "canary", "developer"})


class UnsupportedModelRoute(RuntimeError):
    """Raised before provider I/O when a pinned surface is not qualified."""

    def __init__(
        self,
        *,
        profile: str,
        provider: str,
        model: str,
        adapter: str,
        reason: str,
    ) -> None:
        self.profile = str(profile or "")
        self.provider = str(provider or "")
        self.model = str(model or "")
        self.adapter = str(adapter or "")
        self.reason = str(reason or "unsupported")
        super().__init__(
            "model route is not qualified for the chat's pinned action surface "
            f"({self.profile}; {self.provider}/{self.model}; {self.adapter}): "
            f"{self.reason}. Choose a qualified model, create/migrate a chat "
            "explicitly, or cancel."
        )


def _norm(value: Any) -> str:
    return str(value or "").strip().lower().replace("\\", "/")


def _matches(pattern: str, value: str) -> bool:
    clean = _norm(pattern) or "*"
    return fnmatch.fnmatchcase(_norm(value), clean)


@dataclass(frozen=True)
class SupportRule:
    profile: str
    provider: str
    model: str
    adapter: str
    status: str
    evidence: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SupportRule":
        return cls(
            profile=str(raw.get("profile") or "").strip(),
            provider=str(raw.get("provider") or "*").strip(),
            model=str(raw.get("model") or "*").strip(),
            adapter=str(raw.get("adapter") or "*").strip(),
            status=str(raw.get("status") or "unqualified").strip().lower(),
            evidence=str(raw.get("evidence") or "").strip(),
        )

    def matches(self, profile: str, provider: str, model: str, adapter: str) -> bool:
        return (
            _matches(self.profile, profile)
            and _matches(self.provider, provider)
            and _matches(self.model, model)
            and _matches(self.adapter, adapter)
        )


class SupportMatrix:
    """Immutable request-time view of configured route qualification."""

    def __init__(self, rules: Iterable[SupportRule] = ()) -> None:
        self.rules = tuple(rules)

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None) -> "SupportMatrix":
        block = dict(
            ((cfg or {}).get("action_surface") or (cfg or {}).get("astb") or {})
        )
        # Qualification is evidence, not a consequence of shipping one schema.
        # A missing matrix therefore admits no provider I/O.  Fresh installs
        # seed explicit developer-only routes in llm_config.default.json.
        rows = block.get("support_matrix") or ()
        rules = []
        for row in rows:
            if not isinstance(row, dict) or not str(row.get("profile") or "").strip():
                continue
            normalized = dict(row)
            normalized["profile"] = canonical_action_surface(normalized.get("profile"))
            rules.append(SupportRule.from_dict(normalized))
        # Connecting a newly shipped native OAuth route must not leave its
        # model picker unusable behind an older installation's evidence list.
        # These adapters have structural Python-tool protocol coverage, not a
        # claim that every model passed the task benchmark. Explicit denies
        # still win in qualification(), including operator revocations.
        oauth = (((cfg or {}).get('cloud') or {}).get('oauth') or {})
        for provider, models, adapter in (
            ('google-antigravity', 'gemini-*', 'gemini.*'),
            ('minimax-oauth', '*', 'anthropic.*'),
            ('minimax-oauth-cn', '*', 'anthropic.*'),
        ):
            record = oauth.get(provider) or {}
            if record.get('access_token') or record.get('refresh_token'):
                rules.append(SupportRule(ACTION_SURFACE, provider, models, adapter, 'developer',
                    'Connected native OAuth adapter; Python function-call, cancellation, refresh and wire fixtures verified. Model task quality is not benchmark-qualified.'))
        return cls(rules)

    def qualification(
        self,
        *,
        profile: str,
        provider: str,
        model: str,
        adapter: str,
    ) -> SupportRule | None:
        matches = [
            rule for rule in self.rules
            if rule.matches(profile, provider, model, adapter)
        ]
        if not matches:
            return None
        # A matching deny always wins.  This lets an operator revoke a narrow
        # route without rewriting a broader canary rule.
        denied = [rule for rule in matches if rule.status not in QUALIFIED_STATUSES]
        return denied[0] if denied else matches[0]

    def validate(
        self,
        *,
        profile: str,
        provider: str,
        model: str,
        adapter: str,
    ) -> SupportRule:
        clean_profile = canonical_action_surface(profile or ACTION_SURFACE)
        if not is_action_surface(clean_profile):
            raise UnsupportedModelRoute(
                profile=clean_profile,
                provider=provider,
                model=model,
                adapter=adapter,
                reason="unknown action-surface profile",
            )
        rule = self.qualification(
            profile=clean_profile,
            provider=provider,
            model=model,
            adapter=adapter,
        )
        if rule is None:
            raise UnsupportedModelRoute(
                profile=clean_profile,
                provider=provider,
                model=model,
                adapter=adapter,
                reason="no support-matrix qualification exists",
            )
        if rule.status not in QUALIFIED_STATUSES:
            raise UnsupportedModelRoute(
                profile=clean_profile,
                provider=provider,
                model=model,
                adapter=adapter,
                reason=f"route status is {rule.status}",
            )
        return rule

    def public_snapshot(self) -> dict[str, Any]:
        return {
            "schema": "variant1.astb.support-matrix.v1",
            "rules": [
                {
                    "profile": row.profile,
                    "provider": row.provider,
                    "model": row.model,
                    "adapter": row.adapter,
                    "status": row.status,
                    "evidence": row.evidence,
                }
                for row in self.rules
            ],
        }


def current_action_surface() -> tuple[str, str]:
    """Return the active request's effective support profile and schema.

    Mutation authority is a mutable session capability and never changes the
    provider surface or support qualification profile.
    """

    try:
        from run_context import current_run_context

        ctx = current_run_context()
    except Exception:
        ctx = None
    config = getattr(ctx, "run_config", None) if ctx is not None else None
    profile = canonical_action_surface(
        getattr(config, "action_surface", "") or ACTION_SURFACE
    )
    schema = str(
        getattr(config, "provider_tool_schema_revision", "")
        or IPYTHON_SCHEMA_REVISION
    )
    return profile, schema


def validate_tool_projection(profile: str, schema_revision: str, tools: list | None) -> None:
    names = [
        str(item.get("name") or "")
        for item in (tools or ())
        if isinstance(item, dict)
    ]
    if not is_action_surface(profile):
        raise UnsupportedModelRoute(
            profile=profile,
            provider="projection",
            model="projection",
            adapter=schema_revision,
            reason="unsupported action surface",
        )
    if schema_revision != IPYTHON_SCHEMA_REVISION:
        raise UnsupportedModelRoute(
            profile=profile,
            provider="projection",
            model="projection",
            adapter=schema_revision,
            reason="provider schema revision does not match persistent Python",
        )
    if names != ["ipython"]:
        raise UnsupportedModelRoute(
            profile=profile,
            provider="projection",
            model="projection",
            adapter=schema_revision,
            reason="requests must expose exactly one ipython schema",
        )
