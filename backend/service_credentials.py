"""Encrypted credentials shared by non-inference provider registries.

Search, speech, and messaging providers are services, not model routes.  They
still use the router's DPAPI-backed credential pool so VARIANT-1 has one secret
authority and never puts API keys in tool or UI configuration files.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ServiceCredential:
    secret: str = field(repr=False)
    base_url: str = ""
    source: str = ""


async def resolve(router, service: str, provider: str, *, default_base_url: str = "",
                  env_vars: Iterable[str] = (), shared_provider: str = "") -> ServiceCredential:
    """Select the secret and its destination together, before request dispatch."""
    try:
        dedicated = router.credential_pools.leases(credential_key(service, provider)) if router else ()
    except Exception:
        dedicated = ()
    if dedicated:
        lease = dedicated[0]
        return ServiceCredential(str(lease.secret or ""),
                                 str(getattr(lease, "base_url", "") or default_base_url), "service")
    if router is not None and shared_provider:
        try:
            await router.ensure_oauth_fresh(shared_provider)
        except Exception:
            pass
        try:
            leases = router._credential_leases(shared_provider)
        except Exception:
            leases = ()
        if leases:
            lease = leases[0]
            base = router.provider_base_url(shared_provider, lease)
            return ServiceCredential(str(lease.secret or ""), str(base or default_base_url), "inference")
    for name in env_vars:
        value = str(os.environ.get(str(name), "") or "").strip()
        if value:
            return ServiceCredential(value, default_base_url, "environment")
    return ServiceCredential("", default_base_url)


def credential_key(service: str, provider: str) -> str:
    clean_service = str(service or "service").strip().lower().replace("_", "-")
    clean_provider = str(provider or "unknown").strip().lower().replace("_", "-")
    return f"service-{clean_service}-{clean_provider}"


def _first_pool_secret(router, key: str) -> str:
    if router is None:
        return ""
    try:
        leases = router.credential_pools.leases(key)
    except Exception:
        return ""
    return str(leases[0].secret if leases else "")


async def secret(
    router,
    service: str,
    provider: str,
    *,
    env_vars: Iterable[str] = (),
    shared_provider: str = "",
) -> str:
    """Resolve one secret without copying it into service configuration.

    A service-specific encrypted value wins.  Providers such as xAI/OpenAI may
    intentionally share the already-connected inference account.  Environment
    variables remain a final, non-persisted deployment option.
    """

    value = _first_pool_secret(router, credential_key(service, provider))
    if value:
        return value
    shared = str(shared_provider or "").strip()
    if router is not None and shared:
        try:
            await router.ensure_oauth_fresh(shared)
        except Exception:
            pass
        try:
            leases = router._credential_leases(shared)
        except Exception:
            leases = ()
        if leases:
            return str(leases[0].secret or "")
    for name in env_vars:
        value = str(os.environ.get(str(name), "") or "").strip()
        if value:
            return value
    return ""


def configured(
    router,
    service: str,
    provider: str,
    *,
    env_vars: Iterable[str] = (),
    shared_provider: str = "",
) -> bool:
    if _first_pool_secret(router, credential_key(service, provider)):
        return True
    if router is not None and shared_provider:
        try:
            if router.has_cloud_key(shared_provider):
                return True
        except Exception:
            pass
    return any(str(os.environ.get(str(name), "") or "").strip() for name in env_vars)


def replace(router, service: str, provider: str, value: str) -> dict:
    if router is None:
        raise RuntimeError("credential store is unavailable")
    return router.credential_pools.replace(
        credential_key(service, provider),
        str(value or "").strip(),
        label=f"{provider} {service} credential",
    )


def clear(router, service: str, provider: str) -> bool:
    if router is None:
        raise RuntimeError("credential store is unavailable")
    return router.credential_pools.clear(credential_key(service, provider))
