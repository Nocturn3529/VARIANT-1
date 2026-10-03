"""User Settings projection over the canonical provider recovery policy."""
from __future__ import annotations

import hashlib
import json
import threading

from model_runtime.provider_recovery import PROFILES, normalize_recovery_config

_LOCK = threading.RLock()
_FIELDS = {"enabled", "max_attempts", "max_wait_seconds", "fallback_routes", "auxiliary_routes"}


class SettingsConflict(ValueError):
    pass


class SettingsValidation(ValueError):
    pass


def _snapshot(router):
    config = normalize_recovery_config(router.provider_recovery_policy())
    revision = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"config": config, "revision": revision, "profiles": sorted(PROFILES)}


def snapshot(router):
    with _LOCK:
        return _snapshot(router)


def save(router, message):
    with _LOCK:
        current = _snapshot(router)
        if message.get("expected_revision") != current["revision"]:
            raise SettingsConflict("Routing settings changed. Refresh before saving your draft.")
        raw = message.get("config")
        if not isinstance(raw, dict) or set(raw) - _FIELDS:
            raise SettingsValidation("Use the supported routing policy fields only.")
        try:
            normalized = normalize_recovery_config(raw)
        except ValueError as exc:
            raise SettingsValidation(str(exc)) from exc
        routes = normalized["fallback_routes"] + [route for chain in normalized["auxiliary_routes"].values() for route in chain]
        for route in routes:
            if route["mode"] == "cloud" and router.provider_profile(route["provider"]) is None:
                raise SettingsValidation("A routing chain names an unavailable provider. Choose a supported provider.")
            if "reasoning_effort" in route:
                efforts = router.reasoning_efforts(route["provider"], route["model"]) if route["mode"] == "cloud" else ()
                if route["reasoning_effort"] not in efforts:
                    raise SettingsValidation("A route's reasoning effort is unsupported. Choose a declared effort or inherit the route default.")
        router.configure_provider_recovery(normalized)
        return _snapshot(router)
