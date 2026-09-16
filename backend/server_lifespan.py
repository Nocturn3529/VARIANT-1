"""Lifespan start/stop workers for the composition root.

Bodies live here. They receive the process ``AppHost`` and read its lowercase
service fields directly.
"""

from __future__ import annotations

import asyncio
from typing import Any


def _router(h: Any):
    return getattr(h, "router", None)


def _hub(h: Any):
    return getattr(h, "hub", None)


def _record_worker_failure(
    h: Any,
    name: str,
    exc: BaseException | str,
    *,
    degrade: bool,
) -> None:
    message = str(exc or "worker exited unexpectedly")
    failures = list(getattr(h, "startup_failures", None) or [])
    failures.append({"worker": str(name), "error": message})
    h.startup_failures = failures[-20:]
    if degrade:
        h.startup_ready = False
        h.startup_error = f"{name}: {message}"
    print(f"[variant1-backend] worker {name} failed: {message}", flush=True)


def _supervise_task(
    h: Any,
    awaitable: Any,
    *,
    name: str,
    long_lived: bool,
    degrade: bool,
) -> asyncio.Task:
    task = asyncio.create_task(awaitable, name=name)

    def _completed(done: asyncio.Task) -> None:
        if done.cancelled():
            return
        try:
            exc = done.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            _record_worker_failure(h, name, exc, degrade=degrade)
        elif long_lived:
            _record_worker_failure(
                h, name, "worker exited unexpectedly", degrade=degrade
            )

    task.add_done_callback(_completed)
    return task


async def start_critical_services(h: Any) -> None:
    """Initialize durable identity/runtime state required before readiness."""
    runtime = h.require_runtime()
    children = getattr(getattr(runtime, "catalog", None), "children", None)
    hub = _hub(h)
    if children is not None and hub is not None:
        children.bind_change_publisher(
            hub.broadcast, loop=asyncio.get_running_loop()
        )
    runtime.lifecycle.dev_reset_on_launch()
    # The transcript store is ready before Work Fabric admits background jobs.
    summary = await runtime.session_runtimes.startup_reconcile(
        runtime.sessions
    )
    print(
        f"[session] session runtimes ready chats={summary['chats']} "
        f"tickets_requeued={summary['tickets_requeued']} "
        f"tickets_completed={summary['tickets_completed']} "
        f"deletions_deferred={summary.get('deletions_deferred', 0)}",
        flush=True,
    )
    # All job handlers are installed by host composition before lifespan entry.
    if children is not None:
        await children.reconcile_runtime_sagas()
    # Start the scheduler only after the durable stores above have been
    # recovered and validated, so no queued work can observe a partial runtime.
    await runtime.work.start()


async def start_optional_services(h: Any) -> None:
    """Warm optional engines, memory, sidecars, and event workers."""
    runtime = h.require_runtime()
    router = _router(h)
    hub = _hub(h)
    try:
        extension_runtime = getattr(runtime, "extensions", None)
        if extension_runtime is not None:
            state = await extension_runtime.start()
            print(
                f"[variant1-backend] extension runtime v2 ready "
                f"(packages={len(state.get('installed') or ())}, "
                f"mcp={len(state.get('mcp') or ())})",
                flush=True,
            )
    except Exception as e:
        # Package and skill catalog reads remain available when a configured
        # MCP transport cannot reconnect; its durable status exposes failure.
        print(f"[variant1-backend] extension runtime v2 degraded: {e}", flush=True)
    # Local mode requires the engine; local.prewarm keeps it loaded while the
    # selected route is cloud.
    if router is not None and router.wants_local_engine():
        try:
            await runtime.models.reconcile_local_engine()
            print("[variant1-backend] local engine ready", flush=True)
        except Exception as e:
            print(f"[variant1-backend] local engine unavailable: {e}", flush=True)
    else:
        print("[variant1-backend] local engine cold (cloud route; prewarm disabled)", flush=True)
    if hub is not None:
        await hub.broadcast(h.engine_status_message())

    peers = getattr(runtime, "peers", None)
    if peers is not None:
        await peers.start()

    # Internal runtime telemetry remains available to Overview, but it is not
    # persisted as a user-facing configuration surface.
    try:
        loop0 = asyncio.get_event_loop()
        hardware = await loop0.run_in_executor(None, runtime.lifecycle.detect_hardware)
        h.hardware = hardware
        print(
            f"[variant1-backend] hardware: tier {hardware['tier']} "
            f"(gpu={hardware.get('gpu')}, vram={hardware.get('vram_mb')}MiB, "
            f"ram={hardware.get('ram_mb')}MiB)",
            flush=True,
        )
    except Exception as e:
        print(f"[variant1-backend] hardware detection failed: {e}", flush=True)
    if hub is not None:
        await hub.broadcast(h.engine_status_message())

    # A saved recipe opts into startup explicitly. Only one is honored because
    # the supervisor has a one-active-model invariant.
    try:
        recipes = getattr(h, "runtime_recipes", None)
        autostart = next(
            (row for row in (recipes.recipes if recipes is not None else []) if row.get("autostart")),
            None,
        )
        if recipes is not None and autostart is not None:
            recipes.start_launch(str(autostart["id"]))
            print(f"[variant1-backend] inference recipe autostart queued: {autostart['name']}", flush=True)
    except Exception as e:
        print(f"[variant1-backend] inference recipe autostart failed: {e}", flush=True)

    await runtime.lifecycle.check_orphaned_task()
    # Voice engines are intentionally lazy: Whisper starts on the first mic
    # request and Kokoro loads on the first synthesis/voice-list request.
    #
    # Managed SearXNG (Docker): autostart when provider is searxng and
    # web_search.searxng.autostart is true. Manual Start from Settings when off.
    try:
        ws = getattr(h, "tools_cfg", None)
        mgr = getattr(h, "searxng", None)
        if mgr is not None and ws is not None:
            cfg = ws.web_search if hasattr(ws, "web_search") else {}
            provider = str((cfg or {}).get("provider") or "variant1").lower()
            searx = (cfg or {}).get("searxng") if isinstance((cfg or {}).get("searxng"), dict) else {}
            mgr.configure(searx)
            if provider == "searxng":
                await mgr.ensure_started_if_autostart()
                print(
                    f"[variant1-backend] searxng "
                    f"{'ready' if mgr.ready else 'cold'} "
                    f"at {mgr.base_url} "
                    f"(autostart={mgr.autostart}, docker={mgr.docker_available()})",
                    flush=True,
                )
    except Exception as e:
        print(f"[variant1-backend] searxng init failed: {e}", flush=True)
    bg = getattr(h, "background_tasks", None)
    if bg is not None:
        kernel_runtime = runtime.kernel
        async def _kernel_reaper_loop():
            while True:
                await asyncio.sleep(30.0)
                await kernel_runtime.reap_idle()

        bg.spawn(
            _kernel_reaper_loop(),
            name="kernel-reaper",
            reporter=lambda name, exc: _record_worker_failure(
                h, name, exc, degrade=True
            ),
        )


async def start_workers(h: Any) -> list:
    """Complete critical initialization, then launch supervised workers."""
    lifecycle = h.require_runtime().lifecycle
    h.startup_ready = False
    h.startup_error = ""
    h.startup_failures = []
    try:
        # Runtime reconciliation must finish before the application advertises
        # readiness.
        await lifecycle.start_critical_services()
        tasks = [
            _supervise_task(
                h,
                lifecycle.start_optional_services(),
                name="optional-service-startup",
                long_lived=False,
                degrade=False,
            ),
            _supervise_task(
                h,
                lifecycle.automation_loop(),
                name="automation-loop",
                long_lived=True,
                degrade=True,
            ),
            _supervise_task(
                h,
                lifecycle.consolidation_loop(),
                name="consolidation-loop",
                long_lived=True,
                degrade=True,
            ),
        ]
        gateway = getattr(h, "gateway", None)
        if gateway is not None and hasattr(gateway, "start"):
            tasks.append(_supervise_task(
                h,
                gateway.start(),
                name="messaging-gateway-start",
                long_lived=False,
                degrade=False,
            ))
        h.startup_ready = True
        return tasks
    except Exception as exc:
        h.startup_ready = False
        h.startup_error = str(exc)
        _record_worker_failure(h, "critical-startup", exc, degrade=True)
        raise


async def shutdown(h: Any) -> dict:
    """Continue independent teardown and return identified cleanup failures."""
    import logging
    h.startup_ready = False
    runtime = h.require_runtime()
    failures = []

    async def close(name, operation, *, sync=False):
        try:
            result = await asyncio.to_thread(operation) if sync else await operation()
            if isinstance(result, dict) and result.get("ok") is False:
                raise RuntimeError(str(result.get("failures") or result))
            return True
        except Exception as exc:
            failure = {"service": name, "error_type": type(exc).__name__, "error": str(exc)}
            failures.append(failure)
            logging.getLogger(__name__).exception("shutdown failed for %s", name)
            return False

    peer_publication = getattr(h, "peer_bridge_publication", None)
    if peer_publication is not None:
        await close("peer_bridge_ingress", peer_publication.close, sync=True)
    gateway = getattr(h, "gateway", None)
    if gateway is not None:
        if callable(getattr(gateway, "close_ingress", None)):
            await close("messaging.ingress", gateway.close_ingress)
            await close("messaging.drain", gateway.drain)
        else:
            await close("messaging", gateway.stop)
    grok_peers = getattr(h, "grok_peer_integration", None)
    if grok_peers is not None and callable(getattr(grok_peers, "shutdown", None)):
        await close("grok_peer_integration", grok_peers.shutdown)
    for name in ("peers", "work", "extensions", "browser"):
        service = getattr(runtime, name, None)
        if service is not None:
            await close(name, service.shutdown)
    desktop = getattr(runtime, "desktop", None)
    if desktop is not None and await close("desktop", desktop.close, sync=True):
        from desktop_fabric import uninstall_desktop_fabric
        await close("desktop.uninstall", lambda: uninstall_desktop_fabric(h, desktop), sync=True)
    for name in ("coding", "execution"):
        service = getattr(runtime, name, None)
        if service is not None:
            await close(name, service.shutdown, sync=True)
    for name in ("session_runtimes", "kernel"):
        await close(name, getattr(runtime, name).shutdown)
    for name in ("local_models", "inference_benchmarks", "runtime_recipes", "runtime_installer"):
        service = getattr(h, name, None)
        if service is not None and hasattr(service, "shutdown"):
            await close(name, service.shutdown)
    background = getattr(h, "background_tasks", None)
    if background is not None:
        await close("background_tasks", background.cancel_all)
    mcp = getattr(h, "mcp", None)
    if mcp is not None:
        await close("mcp", mcp.disconnect_all)
    voice = getattr(h, "voice", None)
    if voice is not None:
        await close("voice", voice.stop)
    search = getattr(h, "searxng", None)
    if search is not None:
        await close("searxng", lambda: search.stop(force=False))
    router = _router(h)
    if router is not None:
        await close("models", router.stop)
    h.shutdown_result = {"ok": not failures, "failures": failures}
    return h.shutdown_result
