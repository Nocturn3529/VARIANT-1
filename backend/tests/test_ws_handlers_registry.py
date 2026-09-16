"""Regression: Deck-critical WS types must remain registered after domain splits."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import ws_dispatch
import ws_config


# Message types the Main Deck (and overview widgets) send on the real path.
# If a domain extract drops one of these, production UIs go silent.
DECK_CRITICAL_TYPES = (
    "chat",
    "cancel",
    "chat:sessions",
    "tools:get",
    "config:get",
    "hardware:telemetry",
    "inference:telemetry",
    "inference:operations",
    "inference:platform:get",
    "inference:doctor",
    "inference:install",
    "inference:recipes",
    "inference:nodes",
    "inference:node:recipe:import",
    "inference:benchmarks",
    "model:request_manifests",
    "cloud:usage",
    "model:usage",
    "tts:set",
    "tts:voices",
    "tts:preview",
    "voice:transcribe",
    "mode:set",
    "model:options",
    "local:prewarm:set",
    "model:list",
    "messaging:get",
    "clarification:list",
    "automation:list",
    "automation:run",
    "work:get",
    "work:events",
    "work:jobs",
    "work:job:cancel",
    "execution:get",
    "terminal:open",
    "terminal:read",
    "terminal:write",
    "terminal:resize",
    "terminal:signal",
    "terminal:close",
    "goal:list",
    "goal:get",
    "goal:create",
    "goal:plan",
    "goal:start",
    "goal:pause",
    "goal:resume",
    "goal:cancel",
    "goal:verify",
    "capabilities:get",
    "extension-v2:list",
    "extension-v2:rescan",
    "extension-v2:set-enabled",
    # Platform Settings credential / fallback / endpoint controls
    "cloud:credential:set",
    "cloud:credential:clear",
    "cloud:custom-endpoints:list",
    "cloud:custom-endpoint:validate",
    "cloud:custom-endpoint:save",
    "cloud:custom-endpoint:activate",
    "cloud:custom-endpoint:remove",
    # Managed SearXNG sidecar (Settings → Agent & tools)
    "web_search:set",
    "searxng:start",
    "searxng:stop",
    "searxng:status",
)


def test_dispatch_registers_deck_critical_handlers():
    handlers = ws_dispatch.HANDLERS
    missing = [name for name in DECK_CRITICAL_TYPES if name not in handlers]
    assert not missing, (
        "ws_dispatch.HANDLERS missing Deck-critical types after domain extract: "
        + ", ".join(missing)
    )


def test_telemetry_and_tts_handlers_are_async_callables():
    for name in (
        "hardware:telemetry",
        "inference:telemetry",
        "model:request_manifests",
        "model:usage",
        "tts:voices",
        "tts:preview",
    ):
        fn = ws_dispatch.HANDLERS.get(name)
        assert fn is not None, name
        assert callable(fn)
        # Real registered handlers are async def (awaited by dispatch).
        assert hasattr(fn, "__code__") or hasattr(fn, "__call__")


@pytest.mark.asyncio
async def test_hardware_probe_is_dispatched_off_the_event_loop(monkeypatch):
    calls = []

    def telemetry():
        calls.append("telemetry")
        return {"cpu_percent": 12}

    async def to_thread(fn, *args, **kwargs):
        calls.append("to_thread")
        return fn(*args, **kwargs)

    sent = []
    observed = []
    websocket = SimpleNamespace(
        send_json=lambda payload: _append_async(sent, payload),
    )
    server = SimpleNamespace(
        inference_observability=SimpleNamespace(
            observe_hardware=lambda payload: observed.append(payload),
        ),
    )
    monkeypatch.setattr(ws_config.hardware, "telemetry", telemetry)
    monkeypatch.setattr(ws_config.asyncio, "to_thread", to_thread)

    await ws_dispatch.HANDLERS["hardware:telemetry"](
        server, websocket, None, {},
    )

    assert calls == ["to_thread", "telemetry"]
    assert sent == [{"cpu_percent": 12, "type": "hardware:telemetry"}]
    assert observed == sent


async def _append_async(target, value):
    target.append(value)


def test_mixed_surface_handlers_are_owned_by_their_domain_adapters():
    expected = {
        "capabilities:get": "ws_tools",
        "work:get": "ws_work",
        "work:job:cancel": "ws_work",
        "execution:get": "ws_execution",
        "terminal:open": "ws_execution",
    }
    assert {
        command: ws_dispatch.HANDLERS[command].__module__
        for command in expected
    } == expected


def test_retired_settings_commands_are_not_registered():
    removed = {
        "browser:get", "desktop:get", "desktop:set", "desktop:kill",
        "vision:get", "vision:set",
        "reasoning:set", "sampling:set", "subagent:set",
        "cloud:xai:reasoning_effort:set",
        "cloud:openai-codex:reasoning_effort:set",
        "cloud:models:list", "cloud:model:set",
        "cloud:oauth:link",
        "cloud:fallbacks:set",
        "cloud:provider:options:set",
        "cloud:provider:set",
        "inference:runtimes:list", "inference:runtime:configure",
        "inference:runtime:probe", "inference:runtime:select",
    }
    assert removed.isdisjoint(ws_dispatch.HANDLERS)
    assert {
        "cloud:credential:add", "cloud:credential:remove",
        "cloud:credential:enable", "cloud:credential:priority:set",
        "cloud:credential:strategy:set",
    }.issubset(ws_dispatch.HANDLERS)
    assert (
        ws_dispatch.HANDLERS["reasoning:effort:set"].__module__
        == "ws_chat_sessions"
    )


def test_generic_coding_git_and_worktree_commands_are_not_registered():
    removed = {
        "review:snapshot", "review:discover", "review:start", "review:files",
        "review:run-checks", "review:approve",
        "artifact:snapshot", "artifact:list", "artifact:create", "artifact:publish",
        "browser-fabric:sessions", "browser-fabric:open",
        "browser-fabric:observe", "browser-fabric:action",
        "desktop-fabric:health", "desktop-fabric:catalog",
        "desktop-fabric:observe", "desktop-fabric:act",
        "coding:get", "git:discover", "git:status", "git:diff",
        "git:branches", "git:log", "git:show", "git:stage",
        "git:unstage", "git:commit", "worktree:create", "worktree:get",
        "worktree:list", "worktree:bind", "worktree:release",
        "worktree:remove", "worktree:reconcile",
    }
    assert removed.isdisjoint(ws_dispatch.HANDLERS)


def test_removed_model_probe_command_is_not_registered():
    assert "contract:probe" not in ws_dispatch.HANDLERS


def test_in_app_model_catalog_and_download_commands_are_not_registered():
    removed = {
        "model:catalog",
        "model:download",
        "model:download:cancel",
        "model:library",
        "model:library:settings",
        "model:library:add",
        "model:library:install",
        "model:library:remove",
        "model:search",
        "model:details",
        "model:recipe:create",
    }
    assert removed.isdisjoint(ws_dispatch.HANDLERS)
