"""WebSocket handlers for durable chat session CRUD and search."""

from ws_session_settings import mark_settings_applied, session_settings_command


def _chat_runtime(srv):
    return srv.require_runtime().chat


def _sessions(srv):
    return srv.require_runtime().sessions


def _bind_runtime_attachment(srv, websocket, session, sid: str) -> None:
    runtimes = srv.require_runtime().session_runtimes
    if runtimes is not None and sid:
        runtimes.attach(
            sid,
            getattr(session, "attachment_id", ""),
            session,
            websocket,
        )


def _mutation_route(srv, sid: str) -> dict[str, str]:
    """Resolve the server-owned model route used to qualify mutation."""

    from model_runtime.context import (
        model_route_support_coordinates,
        session_model_route,
    )

    route = session_model_route(_sessions(srv), sid, srv.router)
    return model_route_support_coordinates(srv.router, route)


def _context_payload(srv, sid: str) -> dict:
    from model_runtime.context import session_model_route
    from session_context import context_limit_for_router, latest_session_context
    from session_projection import current_projection, projection_model_route

    route = session_model_route(_sessions(srv), sid, srv.router)
    projected, _ = current_projection(
        _sessions(srv), sid, model_route=projection_model_route(srv.router, _sessions(srv), sid, selected=route),
        snapshot_store=getattr(getattr(srv.require_runtime(), "session_runtimes", None), "snapshot_store", None),
    )
    snapshot = srv.router.model_request_manifest_snapshot()
    return latest_session_context(
        snapshot,
        sid,
        context_limit_tokens=context_limit_for_router(srv.router, route),
        route=route,
        projected_messages=projected,
    )


def _session_payload(srv, sid: str):
    from session_catalog.profiles import is_action_surface

    stored = _sessions(srv).get_session(sid)
    if stored is None:
        return None
    payload = dict(stored)
    installed = srv.require_runtime()
    runtimes = installed.session_runtimes
    kernels = installed.kernel
    controls = installed.session_control
    try:
        runtime = runtimes.snapshot(sid) if runtimes is not None else {}
    except Exception as exc:
        runtime = {"error": str(exc)}
    if runtime:
        runtime = dict(runtime)
        runtime["input_queue"] = runtimes.queue_snapshot(sid)
        runtime["kernel"] = (
            kernels.status(sid) if kernels is not None
            else {"state": "absent", "generation": 0, "pid": None}
        )
        surface = str(runtime.get("action_surface") or "")
        runtime["warning"] = (
            "Trusted local Python — same-user runtime, not sandboxed."
            if is_action_surface(surface) else ""
        )
        if is_action_surface(surface):
            catalog = installed.catalog
            if catalog is not None:
                try:
                    mutation = catalog.mutation_authority.mutation_toggle_status(
                        sid, **_mutation_route(srv, sid)
                    )
                    runtime["mutation_enabled"] = bool(mutation.get("enabled"))
                    runtime["mutation_effective_enabled"] = bool(
                        mutation.get("effective_enabled")
                    )
                    runtime["mutation_authority_revision"] = int(
                        mutation.get("authority_revision") or 0
                    )
                    runtime["mutation_toggle_available"] = bool(
                        mutation.get("available")
                    )
                    runtime["mutation_toggle_locked"] = bool(mutation.get("locked"))
                    runtime["mutation_toggle_reason"] = str(
                        mutation.get("reason") or ""
                    )[:500]
                except Exception as exc:
                    runtime["mutation_enabled"] = bool(
                        runtime.get("mutation_write_enabled", False)
                    )
                    runtime["mutation_effective_enabled"] = False
                    runtime["mutation_authority_revision"] = int(
                        runtime.get("mutation_authority_revision") or 0
                    )
                    runtime["mutation_toggle_available"] = False
                    runtime["mutation_toggle_locked"] = True
                    runtime["mutation_toggle_reason"] = str(exc)[:500]
                try:
                    document, _refs = catalog.namespace_document(sid)
                    mutation_status = dict(
                        (document.get("mutation") or {}).get("status") or {}
                    )
                    probation = {
                        str(row.get("slot_id") or ""): dict(row)
                        for row in (mutation_status.get("probation") or ())
                        if isinstance(row, dict)
                    }
                    runtime["active_slots"] = [
                        {
                            "slot_id": str(row.get("slot_id") or ""),
                            "slot_version": int(row.get("version") or 0),
                            "draft_id": str(row.get("draft_id") or ""),
                            "probation": probation.get(
                                str(row.get("slot_id") or ""), {}
                            ),
                        }
                        for row in (mutation_status.get("active") or ())
                        if isinstance(row, dict)
                    ]
                except Exception as exc:
                    runtime["catalog_status_error"] = str(exc)[:240]
                if getattr(catalog, "children", None) is not None:
                    try:
                        children = catalog.children.list(sid, limit=100)
                    except Exception:
                        children = []
                    runtime["children"] = {
                        "total": len(children),
                        "active": sum(
                            1 for row in children
                            if str(row.get("status") or "") in {"queued", "running", "cancelling"}
                        ),
                    }
        payload["runtime"] = runtime
    if controls is not None:
        payload["session_controls"] = controls.snapshot()
    return payload


def register(on):
    @on("session:settings:get")
    async def _session_settings_get(srv, websocket, session, msg):
        from model_runtime.context import session_model_route
        sid = str(msg.get("session_id") or msg.get("id") or "").strip() or _chat_runtime(srv).viewed_session_id(session)
        response = {"type":"session:settings:snapshot", "request_id":str(msg.get("request_id") or "")[:512],
                    "session_id":sid, "pending":False, "route":None}
        if not _sessions(srv).has_session(sid):
            response["error"] = "unknown_session"
        else:
            registry = srv.require_runtime().session_runtimes
            response["pending"] = registry.configuration_pending(sid)
            response["route"] = session_model_route(_sessions(srv), sid, srv.router)
        await websocket.send_json(response)

    @on("chat:context")
    async def _chat_context(srv, websocket, session, msg):
        sid = str(msg.get("id") or "") or _chat_runtime(srv).viewed_session_id(session)
        await websocket.send_json(_context_payload(srv, sid))

    @on("chat:project:set")
    async def _chat_project_set(srv, websocket, session, msg):
        chat_id = str(msg.get("chat_id") or "").strip()
        request_id = str(msg.get("request_id") or "").strip()[:512]
        response = {
            "type": "chat:project:result",
            "chat_id": chat_id,
            "request_id": request_id,
            "ok": False,
            "project": None,
        }
        try:
            if not chat_id or not _sessions(srv).has_session(chat_id):
                error = ValueError("unknown chat session")
                error.code = "unknown_chat"
                raise error
            project = _sessions(srv).set_project(chat_id, msg.get("root"))
            response["ok"] = True
            response["project"] = project
        except Exception as exc:
            response["error"] = {
                "code": str(getattr(exc, "code", "project_update_failed")),
                "message": str(exc),
            }
            await websocket.send_json(response)
            return
        await websocket.send_json(response)
        try:
            await srv.hub.broadcast(_chat_runtime(srv).sessions_message())
        except Exception as exc:
            print(
                f"[chat] project list broadcast failed after commit: {exc}",
                flush=True,
            )

    @on("model:options")
    async def _model_options(srv, websocket, session, msg):
        from model_options import model_options

        sid = str(msg.get("id") or "") or _chat_runtime(srv).viewed_session_id(session)
        request_id = str(msg.get("request_id") or "")[:160]
        try:
            payload = await model_options(
                srv,
                sid,
                request_id=request_id,
                refresh=bool(msg.get("refresh")),
            )
        except Exception:
            await websocket.send_json({
                "type": "model:options:error",
                "request_id": request_id,
                "session_id": sid,
                "error": "Could not load the model catalog.",
            })
            return
        await websocket.send_json(payload)

    @on("reasoning:effort:set")
    @session_settings_command("reasoning:effort:set")
    async def _reasoning_effort_set(srv, websocket, session, msg):
        from model_runtime.context import session_model_route

        sid = str(msg.get("id") or "") or _chat_runtime(srv).viewed_session_id(session)
        effort = str(msg.get("effort") or "").strip().lower()
        from ws_protocol import chat_is_busy
        if chat_is_busy(srv.require_runtime(), session, sid):
            await websocket.send_json({
                "type": "error",
                "code": "session_busy_reasoning_change",
                "error": "Wait for the active turn to finish before changing reasoning effort.",
            })
            return
        if not sid or not _sessions(srv).has_session(sid):
            await websocket.send_json({
                "type": "error", "code": "unknown_session",
                "error": "Cannot change reasoning for an unknown chat session.",
            })
            return
        route = session_model_route(_sessions(srv), sid, srv.router)
        choices = tuple(route.get("reasoning_efforts") or ())
        if effort not in choices:
            await websocket.send_json({
                "type": "error", "code": "unsupported_reasoning_effort",
                "error": "This model does not support that reasoning effort.",
            })
            return
        route["reasoning_effort"] = effort
        if not _sessions(srv).set_model_route(sid, route):
            await websocket.send_json({
                "type": "error", "code": "reasoning_change_failed",
                "error": "Could not save the session reasoning effort.",
            })
            return
        mark_settings_applied(websocket)
        await websocket.send_json(_context_payload(srv, sid))

    @on("chat:sessions")
    async def _chat_sessions(srv, websocket, session, msg):
        await websocket.send_json(_chat_runtime(srv).sessions_message())

    @on("chat:session:get")
    async def _chat_session_get(srv, websocket, session, msg):
        sid = msg.get("id") or _chat_runtime(srv).viewed_session_id(session)
        sess = _sessions(srv).get_session(sid)
        # Fetching/refreshing a transcript is not navigation. Only explicit
        # new/switch operations select the next input target.
        await websocket.send_json({
            "type": "chat:session",
            "session": _session_payload(srv, sid) if sess is not None else None,
        })

    @on("chat:session:new")
    async def _chat_session_new(srv, websocket, session, msg):
        from model_runtime.context import normalize_model_route

        try:
            sid = _sessions(srv).create_session(request_id=str(msg.get("request_id") or ""))
        except Exception as exc:
            await websocket.send_json({
                "type": "chat:new:result", "request_id": str(msg.get("request_id") or "")[:512],
                "requested_id": "", "effective_id": str(session.viewed_session_id or ""),
                "status": "rejected", "error": str(exc),
            })
            return
        if not _sessions(srv).get_model_route(sid):
            _sessions(srv).set_model_route(sid, normalize_model_route(srv.router))
        session.viewed_session_id = sid
        _bind_runtime_attachment(srv, websocket, session, sid)
        await srv.hub.broadcast(_chat_runtime(srv).sessions_message())
        await websocket.send_json({"type": "chat:session",
            "session": _session_payload(srv, sid),
            **({"navigation": {"request_id": str(msg["request_id"])[:512],
                 "requested_id": "", "effective_id": sid, "status": "created"}}
               if msg.get("request_id") else {})})

    @on("chat:session:switch")
    async def _chat_session_switch(srv, websocket, session, msg):
        # Per-client sessions: bind THIS connection's view/write target, and update
        # the shared default (used by new connections) without forcing other live
        # connections to navigate — their own viewed_session_id is untouched.
        requested_id = str(msg.get("id") or "")
        request_id = str(msg.get("request_id") or "")[:200]
        if str(getattr(session, "view_role", "main") or "main") == "detached_chat":
            pinned_id = str(getattr(session, "viewed_session_id", "") or "")
            if requested_id != pinned_id or not _sessions(srv).has_session(pinned_id):
                await websocket.send_json({
                    "type": "chat:switch:result",
                    "request_id": request_id,
                    "requested_id": requested_id,
                    "effective_id": pinned_id,
                    "status": "rejected",
                    "error": "detached_chat_owner_mismatch",
                })
                return
            _bind_runtime_attachment(srv, websocket, session, pinned_id)
            await websocket.send_json({
                "type": "chat:session",
                "session": _session_payload(srv, pinned_id),
                "navigation": {
                    "request_id": request_id,
                    "requested_id": requested_id,
                    "effective_id": pinned_id,
                    "status": "switched",
                },
            })
            return
        try:
            sid = _sessions(srv).set_active(requested_id)
        except Exception as exc:
            await websocket.send_json({
                "type": "chat:switch:result", "request_id": request_id,
                "requested_id": requested_id,
                "effective_id": str(session.viewed_session_id or ""),
                "status": "rejected", "error": str(exc),
            })
            return
        session.viewed_session_id = sid
        _bind_runtime_attachment(srv, websocket, session, sid)
        await srv.hub.broadcast(_chat_runtime(srv).sessions_message())
        await websocket.send_json({
            "type": "chat:session", "session": _session_payload(srv, sid),
            "navigation": {"request_id": request_id, "requested_id": requested_id,
                           "effective_id": sid,
                           "status": "switched" if sid == requested_id else "fallback"},
        })

    @on("chat:session:rename")
    async def _chat_session_rename(srv, websocket, session, msg):
        _sessions(srv).rename(str(msg.get("id") or ""), str(msg.get("title") or ""))
        await srv.hub.broadcast(_chat_runtime(srv).sessions_message())

    @on("chat:session:delete")
    async def _chat_session_delete(srv, websocket, session, msg):
        sid = str(msg.get("id") or "")
        if not _sessions(srv).has_session(sid):
            await websocket.send_json({"type": "chat:session:error", "error": "unknown session"})
            return
        runtimes = srv.require_runtime().session_runtimes
        fallback = await runtimes.tombstone_chat_owner(sid, _sessions(srv))
        await srv.hub.broadcast({
            "type": "chat:session:deleted", "id": sid, "session_id": sid,
        })
        if session.viewed_session_id == sid:
            session.viewed_session_id = fallback
            _bind_runtime_attachment(srv, websocket, session, fallback)
        await srv.hub.broadcast(_chat_runtime(srv).sessions_message())
        await websocket.send_json({
            "type": "chat:session",
            "session": _session_payload(
                srv, _chat_runtime(srv).viewed_session_id(session)),
        })

    @on("chat:session:pin")
    async def _chat_session_pin(srv, websocket, session, msg):
        _sessions(srv).set_flag(str(msg.get("id") or ""), pinned=bool(msg.get("value")))
        await srv.hub.broadcast(_chat_runtime(srv).sessions_message())

    @on("chat:session:archive")
    async def _chat_session_archive(srv, websocket, session, msg):
        _sessions(srv).set_flag(str(msg.get("id") or ""), archived=bool(msg.get("value")))
        await srv.hub.broadcast(_chat_runtime(srv).sessions_message())

    @on("chat:session:annotate")
    async def _chat_session_annotate(srv, websocket, session, msg):
        """Attach the STEPS strip to the last assistant message.

        The Deck finishes a turn with a rich step list that only lived in memory;
        this makes it durable so section/session switches keep the expander.
        Does not rebroadcast the full session (avoids wiping the live transcript).
        """
        sid = str(msg.get("id") or "") or _chat_runtime(srv).viewed_session_id(session)
        if not sid or not _sessions(srv).has_session(sid):
            await websocket.send_json({
                "type": "chat:session:error",
                "error": "unknown session",
            })
            return
        steps = msg.get("steps") if "steps" in msg else None
        receipt = msg.get("receipt") if "receipt" in msg else None
        if steps is None and receipt is None:
            await websocket.send_json({"type": "chat:session:annotated", "ok": True, "id": sid})
            return
        sess = _sessions(srv).annotate_last_assistant(
            sid, steps=steps, receipt=receipt if isinstance(receipt, dict) else None,
            run_id=str(msg.get("run_id") or "").strip(),
        )
        if sess is None:
            await websocket.send_json({
                "type": "chat:session:error",
                "error": "could not annotate session",
            })
            return
        await websocket.send_json({
            "type": "chat:session:annotated",
            "ok": True,
            "id": sid,
        })

    @on("chat:runtime:get")
    async def _chat_runtime_get(srv, websocket, session, msg):
        sid = str(msg.get("id") or "") or _chat_runtime(srv).viewed_session_id(session)
        payload = _session_payload(srv, sid) or {}
        await websocket.send_json({
            "type": "chat:runtime",
            "id": sid,
            "runtime": payload.get("runtime") or {},
        })

    @on("chat:runtime:action")
    async def _chat_runtime_action(srv, websocket, session, msg):
        sid = str(msg.get("id") or "") or _chat_runtime(srv).viewed_session_id(session)
        action = str(msg.get("action") or "").strip()
        try:
            if action == "stop_cell":
                runtimes = srv.require_runtime().session_runtimes
                owner = runtimes.active_run_owner(sid)
                if owner is not None:
                    request_interrupt = getattr(owner[0], "request_interrupt", None)
                    if callable(request_interrupt):
                        request_interrupt()
                task = runtimes.cancel_active_run(sid)
                outcome = {"cancel_requested": task is not None}
            elif action in {"restart_kernel", "stop_kernel"}:
                closed = await srv.require_runtime().kernel.close_chat(
                    sid, reason="explicit_" + action
                )
                outcome = {"kernel_closed": bool(closed)}
            elif action == "reset_session_tools":
                outcome = srv.require_runtime().catalog.reset(sid)
            else:
                raise ValueError(f"unknown chat runtime action: {action!r}")
            await websocket.send_json({
                "type": "chat:runtime:action:done",
                "id": sid,
                "action": action,
                "outcome": outcome,
            })
            await websocket.send_json({
                "type": "chat:session",
                "session": _session_payload(srv, sid),
            })
        except Exception as exc:
            await websocket.send_json({
                "type": "chat:session:error", "error": str(exc),
            })

    @on("chat:runtime:mutation:set")
    async def _chat_runtime_mutation_set(srv, websocket, session, msg):
        sid = str(msg.get("id") or "") or _chat_runtime(srv).viewed_session_id(session)
        request_id = str(msg.get("request_id") or "")[:200]
        requested_enabled = msg.get("enabled")
        expected_revision = msg.get("expected_revision")
        if not sid or not _sessions(srv).has_session(sid):
            await websocket.send_json({
                "type": "chat:runtime:mutation:set:rejected",
                "id": sid,
                "enabled": (
                    requested_enabled if isinstance(requested_enabled, bool) else False
                ),
                "request_id": request_id,
                "error": "unknown session",
            })
            return
        try:
            if not isinstance(requested_enabled, bool):
                raise ValueError("mutation enabled must be a boolean")
            if (
                isinstance(expected_revision, bool)
                or not isinstance(expected_revision, int)
                or expected_revision < 0
            ):
                raise ValueError(
                    "mutation expected_revision must be a non-negative integer"
                )
            enabled = requested_enabled
            result = srv.require_runtime().catalog.mutation_authority.set_mutation(
                sid,
                enabled=enabled,
                expected_revision=expected_revision,
                actor="chat_composer:user",
                **_mutation_route(srv, sid),
            )
            try:
                status = (
                    srv.require_runtime().catalog.mutation_authority.mutation_toggle_status(
                        sid,
                        **_mutation_route(srv, sid),
                    )
                )
            except Exception as status_error:
                # The CAS has committed. A projection failure cannot relabel a
                # successful authority change as rejected.
                print(
                    "[chat] mutation status projection failed after commit: "
                    f"{status_error}",
                    flush=True,
                )
                status = {
                    "enabled": bool(result.get("mutation_enabled")),
                    "effective_enabled": False,
                    "authority_revision": int(
                        result.get("authority_revision") or 0
                    ),
                }
            await websocket.send_json({
                "type": "chat:runtime:mutation:set:done",
                "id": sid,
                "enabled": bool(
                    result.get("mutation_enabled")
                    if isinstance(result.get("mutation_enabled"), bool)
                    else status.get("enabled")
                ),
                "effective_enabled": bool(status.get("effective_enabled")),
                "request_id": request_id,
                "authority_revision": int(
                    status.get("authority_revision")
                    or result.get("authority_revision")
                    or 0
                ),
                "result": result,
            })
            # Mutation authority is a per-chat durable fact. Rehydrate exactly
            # the Decks attached to this chat; a global chat:session broadcast
            # would switch unrelated idle windows to the wrong transcript.
            snapshot = {
                "type": "chat:session",
                "session": _session_payload(srv, sid),
            }
            runtimes = srv.require_runtime().session_runtimes
            transports = runtimes.attached_transports(sid)
            for transport in transports:
                try:
                    await transport.send_json(snapshot)
                except Exception as delivery_error:
                    print(
                        "[chat] mutation session delivery failed: "
                        f"{delivery_error}",
                        flush=True,
                    )
        except Exception as exc:
            await websocket.send_json({
                "type": "chat:runtime:mutation:set:rejected",
                "id": sid,
                "enabled": (
                    requested_enabled if isinstance(requested_enabled, bool) else False
                ),
                "request_id": request_id,
                "error": str(exc),
            })
            await websocket.send_json({
                "type": "chat:session",
                "session": _session_payload(srv, sid),
            })

    @on("chat:search")
    async def _chat_search(srv, websocket, session, msg):
        query = str(msg.get("query") or "")
        try:
            limit = max(1, min(30, int(msg.get("limit") or 12)))
        except (TypeError, ValueError):
            limit = 12
        items = _sessions(srv).search(query, limit=limit) if query.strip() else []
        await websocket.send_json({"type": "chat:search:results",
                                   "query": query, "items": items})
