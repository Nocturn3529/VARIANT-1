"""Status projections derived from an explicit ``AppHost``."""

from __future__ import annotations

import sys
import host_voice
from speech import local_tts as tts
from speech import providers as speech_providers


def _router(h):
    return getattr(h, "router", None)


def engine_label(h) -> str:
    router = _router(h)
    return (str(getattr(router.engine, "display_name", "local inference")) if router.mode == "local"
            else router.cfg.get("cloud", {}).get("provider", "cloud"))


def _web_search_status(h) -> dict:
    try:
        from web_search import providers as _wsp
        runtime = None
        mgr = getattr(h, "searxng", None)
        if mgr is not None:
            try:
                # Keep manager fields in sync with tools.json before snapshot.
                searx = (h.tools_cfg.web_search or {}).get("searxng") or {}
                if isinstance(searx, dict):
                    mgr.configure(searx)
                runtime = mgr.public_status()
            except Exception:
                runtime = None
        return _wsp.public_status(
            h.tools_cfg.web_search, runtime=runtime, router=_router(h))
    except Exception:
        return {
            "provider": "variant1",
            "variant1": {"engines": ["ddg", "bing"], "docker_required": False},
            "searxng": {"base_url": "", "autostart": False, "ready": False},
        }


def voice_state(h) -> dict:
    router = _router(h)
    whisper = getattr(h, "voice", None)
    local_stt = whisper.installed() if whisper is not None else False
    tts_assets = tts.asset_status()
    catalog = speech_providers.catalog(
        router, host_voice.voice_cfg(router), local_stt_available=local_stt)
    stt_provider = host_voice.stt_provider(router)
    tts_provider = host_voice.tts_provider(router)
    stt_selected = next((row for row in catalog["stt_providers"]
                         if row["id"] == stt_provider), {})
    tts_selected = next((row for row in catalog["tts_providers"]
                         if row["id"] == tts_provider), {})
    return {
        "stt": {
            "route": host_voice.stt_route(router), "provider": stt_provider,
            "available": bool(stt_selected.get("available")),
            "local_available": local_stt,
            "local_ready": getattr(whisper, "ready", False),
            "drop_path": getattr(whisper, "model_drop_dir", ""),
            "binary_path": getattr(whisper, "binary", ""),
            "model_path": getattr(whisper, "model", ""),
            "required_runtime": [
                "whisper-server.exe", "whisper.dll", "ggml.dll",
                "ggml-base.dll", "ggml-cpu.dll",
            ],
            "accepted_models": ["*.bin"],
            "providers": catalog["stt_providers"],
        },
        "tts": {
            "route": host_voice.tts_route(router), "provider": tts_provider,
            "available": bool(tts_selected.get("available")),
            "local_available": bool(tts_assets["available"]),
            "drop_path": tts_assets["drop_path"],
            "model_path": tts_assets["model_path"],
            "voices_path": tts_assets["voices_path"],
            "required_files": tts_assets["required_files"],
            "enabled": host_voice.tts_enabled(router),
            "speed": host_voice.tts_speed(router),
            "voice": host_voice.tts_voice(router),
            "providers": catalog["tts_providers"],
        },
    }


def tools_state(h) -> dict:
    registry = h.require_runtime().registry
    items = [
        {**t.spec(), "available": True}
        for t in registry.all()
        if not t.hidden
    ]
    skills = h.require_runtime().extensions.skills
    return {"type": "tools", "items": items,
            "web_search": _web_search_status(h),
            "skills": skills.list()}


def doctor_snapshot(h) -> dict:
    """Gather the live config/state the doctor linter inspects."""
    servers = {}
    mcp = h.require_runtime().extensions.mcp
    for row in mcp.configured():
        sid = str(row["server_id"])
        servers[sid] = {"status": mcp.status(sid).get("status", "disconnected")}
    router = _router(h)
    vision = h.vision_cfg()
    return {
        "mode": router.mode,
        "provider": router.cloud_provider,
        "has_key": router.has_cloud_key(router.cloud_provider),
        "key_tokens": dict((router.cfg.get("cloud", {}) or {}).get("keys", {}) or {}),
        "mcp_servers": servers,
        "engine_ready": router.engine_ready,
        "model_name": router.model_name,
        "is_windows": sys.platform.startswith("win"),
        "vision": vision,
    }


def engine_status_msg(h) -> dict:
    runtime = getattr(h, "runtime", None)
    memory_store = getattr(getattr(runtime, "memory", None), "store", None)
    work = getattr(runtime, "work", None)
    sessions = getattr(runtime, "sessions", None)
    execution = getattr(runtime, "execution", None)
    coding = getattr(runtime, "coding", None)
    goals = getattr(runtime, "goals", None)
    artifacts = getattr(runtime, "artifacts", None)
    browser = getattr(runtime, "browser", None)
    desktop = getattr(runtime, "desktop", None)
    extensions = getattr(runtime, "extensions", None)
    model_ready = (
        h.router.cloud_route_ready()
        if h.router.mode == "cloud"
        else h.router.engine_ready
    )
    return {"type": "engine", "engine": engine_label(h), "ready": h.router.engine_ready,
            "model_ready": bool(model_ready),
            "model": h.router.model_name,
            "memory": memory_store is not None,
            "memory_count": (
                memory_store.count_items() if memory_store is not None else 0
            ),
            "mode": h.router.mode,
            "local_prewarm": h.router.local_prewarm,
            "local_engine_wanted": h.router.wants_local_engine(),
            "provider": h.router.cloud_provider,
            "cloud_model": h.router.get_cloud_model(),
            "cloud_usage": h.router.usage_snapshot(),
            "hardware": h.hardware,
            "keys": {p: h.router.has_cloud_key(p)
                     for p in (
                         "anthropic", "openai", "openai-codex", "xai",
                         "nvidia", "gemini",
                     )},
            # First-class subscription OAuth status (no token material).
            # `xai` stays a bool for older UI; `xai_detail` is full non-secret status.
            "oauth": {
                "xai": bool(h.router.oauth_status("xai").get("connected")),
                "xai_detail": h.router.oauth_status("xai"),
                "openai_codex": bool(
                    h.router.oauth_status("openai-codex").get("connected")
                ),
                "openai_codex_detail": h.router.oauth_status("openai-codex"),
            },
            "dpapi": (
                h.secretstore.is_available()
                if getattr(h, "secretstore", None) is not None
                else __import__("security.secretstore", fromlist=["is_available"]).is_available()
            ),
            "inference_runtime": h.router.inference_runtime_id,
            "local_runtime": h.router.engine.runtime_status(),
            "agent_mode": h.agent_mode(),
             "scheduler": h.scheduler.status() if getattr(h, "scheduler", None) else {},
             "work": {
                 "ready": bool(work and getattr(work, "started", False)),
                 "scheduler_running": bool(
                     work and getattr(getattr(work, "scheduler", None), "running", False)
                 ),
                 "active_jobs": int(
                     getattr(getattr(work, "scheduler", None), "active_count", 0) or 0
                 ),
             },
             "sessions": {
                 "ready": bool(sessions is not None),
             },
             "execution": (
                 execution.capability_report()
                 if execution is not None
                 and callable(getattr(execution, "capability_report", None))
                 else {"ready": bool(execution is not None)}
             ),
             "coding": (
                 coding.health()
                 if coding is not None and callable(getattr(coding, "health", None))
                 else {"ready": bool(coding is not None)}
             ),
             "goals": {
                 "ready": bool(goals is not None),
                 "active": (
                     goals.repository.count_goals(status="running")
                     + goals.repository.count_goals(status="queued")
                     + goals.repository.count_goals(status="waiting_user")
                     + goals.repository.count_goals(status="waiting_external")
                     if goals is not None else 0
                 ),
             },
             "artifacts": (
                 artifacts.state()
                 if artifacts is not None and callable(getattr(artifacts, "state", None))
                 else {"ready": False}
             ),
             "browser_fabric": (
                 browser.health()
                 if browser is not None and callable(getattr(browser, "health", None))
                 else {"ready": False}
             ),
             "desktop_fabric": (
                 desktop.capability_report()
                 if desktop is not None and callable(getattr(desktop, "capability_report", None))
                 else {"ready": False}
             ),
             "extensions_v2": (
                 extensions.state()
                 if extensions is not None and callable(getattr(extensions, "state", None))
                 else {"ready": False}
             ),
            "voice": voice_state(h),
            "vision": h.vision_cfg(),
            "runtime_process": (
                h.runtime_recipes.process_status()
                if getattr(h, "runtime_recipes", None) is not None else {}
            )}


def config_status_msg(h) -> dict:
    m = engine_status_msg(h)
    m["type"] = "config"
    m["providers"] = h.router.list_provider_info()
    credential_snapshots = {item["name"]: h.router.credential_pools.snapshot(item["name"]) for item in m["providers"]}
    m["credentials_by_provider"] = {name: row["items"] for name, row in credential_snapshots.items()}
    m["credential_strategy_by_provider"] = {name: row["strategy"] for name, row in credential_snapshots.items()}
    m["credential_revision_by_provider"] = {name: row["revision"] for name, row in credential_snapshots.items()}
    m["oauth_by_provider"] = {
        item["name"]: h.router.oauth_status(item["name"])
        for item in m["providers"] if "oauth" in item.get("auth_methods", [])
    }
    m["custom_endpoints"] = h.router.list_custom_endpoints()
    m["provider_plugin_errors"] = list(h.router.provider_registry.errors)
    m["provider_plugins"] = h.router.provider_registry.plugin_catalog()
    if getattr(h, "runtime_installer", None) is not None:
        m["inference_platform"] = {
            "targets": h.runtime_installer.discover_targets(),
            "install_jobs": h.runtime_installer.jobs_snapshot().get("items", []),
            "local_runtime": h.runtime_installer.local_runtime_status(),
        }
    gateway = getattr(h, "gateway", None)
    m["messaging_gateway"] = gateway.public_state() if gateway is not None else {}
    return m
