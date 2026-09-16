"""WebSocket handlers for tools, capabilities, and search."""

from model_runtime import capabilities


async def _config_error(websocket, msg, operation, exc):
    await websocket.send_json({
        "type": "tools:rejected",
        "request_id": str(msg.get("request_id") or ""),
        "operation": operation,
        "error": str(exc)[:1000],
    })


def register(on):
    @on("tools:get")
    async def _tools_get(srv, websocket, session, msg):
        await websocket.send_json(srv.require_runtime().tool_settings.state())

    @on("capabilities:get")
    async def _capabilities_get(srv, websocket, session, msg):
        caps = capabilities.resolve(srv.router)
        await websocket.send_json({"type": "capabilities", **caps.to_dict()})

    @on("web_search:set")
    async def _web_search_set(srv, websocket, session, msg):
        """Configure the selected web-search provider and non-secret options."""
        updates = {}
        if msg.get("provider") is not None:
            updates["provider"] = str(msg.get("provider") or "variant1")
        if msg.get("variant1") is not None and isinstance(msg.get("variant1"), dict):
            updates["variant1"] = msg.get("variant1")
        if msg.get("searxng") is not None and isinstance(msg.get("searxng"), dict):
            updates["searxng"] = msg.get("searxng")
        provider_options = msg.get("options")
        if isinstance(provider_options, dict):
            provider = str(msg.get("provider") or "").strip().lower()
            if provider:
                updates[provider] = provider_options
        # Flat convenience fields from Settings UI
        if msg.get("searxng_base_url") is not None:
            updates.setdefault("searxng", {})["base_url"] = str(msg.get("searxng_base_url") or "")
        if msg.get("searxng_autostart") is not None:
            updates.setdefault("searxng", {})["autostart"] = bool(msg.get("searxng_autostart"))
        if msg.get("searxng_managed") is not None:
            updates.setdefault("searxng", {})["managed"] = bool(msg.get("searxng_managed"))
        if updates:
            try:
                srv.tools_cfg.set_web_search_config(updates)
            except Exception as exc:
                await _config_error(websocket, msg, "web_search.set", exc)
                return
            mgr = getattr(srv, "searxng", None)
            if mgr is not None:
                searx = (srv.tools_cfg.web_search or {}).get("searxng") or {}
                if isinstance(searx, dict):
                    await mgr.reconfigure(searx)
        await websocket.send_json({
            "type": "tools:accepted",
            "request_id": str(msg.get("request_id") or ""),
            "operation": "web_search.set",
        })
        await srv.hub.broadcast(srv.require_runtime().tool_settings.state())

    @on("web_search:credential:set")
    async def _web_search_credential_set(srv, websocket, session, msg):
        from service_credentials import replace
        provider = str(msg.get("provider") or "").strip().lower()
        try:
            from web_search.providers import provider_definitions
            known = {row["id"] for row in provider_definitions()}
            if provider not in known:
                raise ValueError("unknown web-search provider")
            replace(srv.router, "web", provider, str(msg.get("key") or ""))
        except Exception as exc:
            await _config_error(websocket, msg, "web_search.credential.set", exc)
            return
        await websocket.send_json({"type": "tools:accepted",
            "request_id": str(msg.get("request_id") or ""),
            "operation": "web_search.credential.set"})
        await srv.hub.broadcast(srv.require_runtime().tool_settings.state())

    @on("web_search:credential:clear")
    async def _web_search_credential_clear(srv, websocket, session, msg):
        from service_credentials import clear
        provider = str(msg.get("provider") or "").strip().lower()
        try:
            clear(srv.router, "web", provider)
        except Exception as exc:
            await _config_error(websocket, msg, "web_search.credential.clear", exc)
            return
        await websocket.send_json({"type": "tools:accepted",
            "request_id": str(msg.get("request_id") or ""),
            "operation": "web_search.credential.clear"})
        await srv.hub.broadcast(srv.require_runtime().tool_settings.state())

    @on("searxng:start")
    async def _searxng_start(srv, websocket, session, msg):
        """Manual Start button — run managed SearXNG even when autostart is off."""
        mgr = getattr(srv, "searxng", None)
        if mgr is None:
            await websocket.send_json({
                "type": "searxng:status", "ready": False,
                "running": False, "owned": False,
                "error": "SearXNG manager is unavailable.",
            })
            return
        searx = (srv.tools_cfg.web_search or {}).get("searxng") or {}
        if isinstance(searx, dict):
            await mgr.reconfigure(searx)
        try:
            await mgr.start(force=True)
            await srv.hub.broadcast(srv.require_runtime().tool_settings.state())
            await websocket.send_json({
                "type": "searxng:status", **mgr.public_status(),
            })
        except Exception as e:
            mgr.last_error = str(e)
            await srv.hub.broadcast(srv.require_runtime().tool_settings.state())
            await websocket.send_json({
                "type": "searxng:status", **mgr.public_status(),
            })

    @on("searxng:stop")
    async def _searxng_stop(srv, websocket, session, msg):
        """Manual Stop — tear down the managed container (force)."""
        mgr = getattr(srv, "searxng", None)
        if mgr is None:
            await websocket.send_json({
                "type": "searxng:status", "ready": False,
                "running": False, "owned": False,
                "error": "SearXNG manager is unavailable.",
            })
            return
        try:
            # force=True so the button stops even if another session started it.
            await mgr.stop(force=True)
            await srv.hub.broadcast(srv.require_runtime().tool_settings.state())
            await websocket.send_json({
                "type": "searxng:status", **mgr.public_status(),
            })
        except Exception as e:
            mgr.last_error = str(e)
            await websocket.send_json({
                "type": "searxng:status", **mgr.public_status(),
            })

    @on("searxng:status")
    async def _searxng_status(srv, websocket, session, msg):
        mgr = getattr(srv, "searxng", None)
        if mgr is None:
            await websocket.send_json({
                "type": "searxng:status",
                "ready": False, "error": "manager not loaded",
            })
            return
        await mgr.probe()
        await websocket.send_json({"type": "searxng:status", **mgr.public_status()})

