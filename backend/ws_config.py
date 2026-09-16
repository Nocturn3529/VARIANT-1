"""WebSocket handlers for provider accounts, endpoints, credentials, and local inference."""

from __future__ import annotations

import asyncio
import base64
import ntpath
import os
import uuid

import background_tasks
from ws_session_settings import mark_settings_applied, session_settings_command
from observability import doctor
from model_runtime import hardware


def _sessions(srv):
    return srv.require_runtime().sessions


async def _send_tts_voices(srv, websocket=None, *, broadcast: bool = False) -> None:
    voice = srv.require_runtime().voice
    provider = voice.provider()
    try:
        payload = {
            "type": "tts:voices",
            "items": await voice.list_voices(),
            "current": voice.voice(),
            "route": voice.route(),
            "provider": provider,
        }
    except Exception as exc:
        payload = {
            "type": "tts:voices",
            "items": [],
            "error": str(exc),
            "route": voice.route(),
            "provider": provider,
        }
    if broadcast:
        await srv.hub.broadcast(payload)
    elif websocket is not None:
        await websocket.send_json(payload)


async def _send_tts_preview_terminal(
    websocket,
    session,
    *,
    request_id: str,
    purpose: str,
    session_id: str,
    audio: bytes = b"",
    mime_type: str = "audio/wav",
    error: str = "",
    cancelled: bool = False,
) -> None:
    identity = str(request_id or "").strip()
    delivered = getattr(session, "tts_preview_terminal_ids", None)
    if not isinstance(delivered, set):
        delivered = set()
        session.tts_preview_terminal_ids = delivered
    if identity and identity in delivered:
        return
    if identity:
        delivered.add(identity)
        if len(delivered) > 32:
            delivered.pop()
    payload = {
        "type": "tts:preview",
        "audio": base64.b64encode(audio or b"").decode("ascii"),
        "mime_type": str(mime_type or "audio/wav"),
        "purpose": str(purpose or "preview"),
        "request_id": identity,
        "session_id": str(session_id or ""),
    }
    if error:
        payload["error"] = str(error)
    if cancelled:
        payload["cancelled"] = True
    try:
        await websocket.send_json(payload)
    except Exception:
        pass


async def _cancel_tts_preview(websocket, session, *, request_id: str = "") -> bool:
    task = getattr(session, "tts_preview_task", None)
    active_id = str(getattr(session, "tts_preview_request_id", "") or "")
    if request_id and active_id and str(request_id) != active_id:
        return False
    purpose = "chat" if getattr(session, "tts_preview_session_id", "") else "preview"
    origin = str(getattr(session, "tts_preview_session_id", "") or "")
    cancelled = bool(task is not None and not task.done() and task.cancel())
    if task is not None and not task.done():
        await asyncio.gather(task, return_exceptions=True)
    if active_id:
        await _send_tts_preview_terminal(
            websocket, session, request_id=active_id, purpose=purpose,
            session_id=origin, cancelled=True,
        )
    if getattr(session, "tts_preview_task", None) is task:
        session.tts_preview_task = None
        session.tts_preview_request_id = ""
        session.tts_preview_session_id = ""
    return cancelled


async def _run_tts_preview(
    srv,
    websocket,
    session,
    *,
    request_id: str,
    purpose: str,
    session_id: str,
    text: str,
    voice_id: str,
) -> None:
    from speech.providers import audio_result
    service = srv.require_runtime().voice
    fallback_mime = service.mime_type()
    try:
        audio = audio_result(await service.synthesize(text, voice=voice_id), fallback_mime=fallback_mime)
    except asyncio.CancelledError:
        await _send_tts_preview_terminal(
            websocket, session, request_id=request_id, purpose=purpose,
            session_id=session_id, cancelled=True,
        )
        return
    except Exception as exc:
        await _send_tts_preview_terminal(
            websocket, session, request_id=request_id, purpose=purpose,
            session_id=session_id, error=str(exc),
        )
        return
    finally:
        if getattr(session, "tts_preview_task", None) is asyncio.current_task():
            session.tts_preview_task = None
            session.tts_preview_request_id = ""
            session.tts_preview_session_id = ""
    await _send_tts_preview_terminal(
        websocket, session, request_id=request_id, purpose=purpose,
        session_id=session_id, audio=audio.data,
        mime_type=audio.mime_type,
    )


async def _reconcile_local_engine_policy(srv, reason: str) -> None:
    """Apply a saved routing change without blocking the WebSocket receive loop."""
    try:
        action = await srv.require_runtime().models.reconcile_local_engine()
        print(f"[variant1-backend] local engine policy ({reason}): {action}", flush=True)
    except Exception as e:
        print(f"[variant1-backend] local engine policy ({reason}) failed: {e}", flush=True)
    finally:
        await srv.hub.broadcast(srv.engine_status_message())


def _model_identity_key(value: object) -> str:
    """Normalize either slash dialect for case-insensitive local identity."""
    return ntpath.normcase(ntpath.normpath(str(value or "").replace("/", "\\")))


def _canonical_local_model(current: str, items: list[dict]) -> str:
    """Resolve a configured/legacy path only when it names one scanned model."""
    raw = str(current or "").strip()
    if not raw:
        return ""
    needle = _model_identity_key(raw).lstrip("\\")
    matches = []
    for item in items:
        path = str(item.get("path") or "") if isinstance(item, dict) else ""
        if not path:
            continue
        candidate = _model_identity_key(path)
        if candidate == needle or candidate.endswith("\\" + needle):
            matches.append(path)
    return matches[0] if len(matches) == 1 else raw


def _local_picker_model(srv, requested: str) -> tuple[str, str]:
    """Resolve one picker model to the exact scanned GGUF and projector."""
    raw = str(requested or "").strip()
    if not raw or srv.router.inference_runtime_id != "llamacpp":
        return raw, ""
    needle = _model_identity_key(raw)
    matches = []
    for item in srv.require_runtime().models.scan_models():
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        if _model_identity_key(path) == needle:
            matches.append((path, str(item.get("mmproj") or "")))
    if len(matches) != 1:
        raise ValueError("The selected local model is no longer in models\\user.")
    return matches[0]


def _local_models_msg(srv, *, current: str | None = None,
                      switching: bool | None = None) -> dict:
    """Return one authoritative local-model picker snapshot.

    Model restarts are asynchronous. Keeping the pending target on the server
    prevents a model:list refresh from making the picker look idle while the
    engine is still changing.
    """
    if switching is None:
        switching = bool(getattr(srv, "_local_model_switching", False))
    if current is None and switching:
        current = str(getattr(srv, "_local_model_switch_target", "") or "")
    if current is None:
        configured = (srv.router.cfg.get("local", {}) or {}).get("model", "") or ""
        current = str(configured)
    items = srv.require_runtime().models.scan_models()
    current = _canonical_local_model(str(current or ""), items)
    return {
        "type": "models",
        "items": items,
        "folder": os.path.join(srv.data_dir, "models", "user"),
        # Full path is the stable identity now that multiple repositories may
        # contain the same GGUF basename. Keep a basename projection for older
        # display-only consumers.
        "current": str(current or ""),
        "current_name": os.path.basename(str(current or "")),
        "switching": switching,
    }


async def _switch_local_model(srv, path: str, mmproj: str, generation: int) -> None:
    """Restart the local engine and always close the picker lifecycle."""
    outcome = "failed"
    failure = ""
    try:
        outcome = await srv.require_runtime().models.restart_engine(
            path,
            mmproj,
            should_apply=lambda: generation == getattr(
                srv, "_local_model_switch_generation", 0
            ),
        )
    except Exception as exc:
        failure = str(exc)
    finally:
        # A newer request may be queued behind the engine lock. Its pending
        # snapshot must remain visible until that newest restart completes.
        if generation == getattr(srv, "_local_model_switch_generation", 0):
            srv._local_model_switching = False
            current = str(
                (srv.router.cfg.get("local", {}) or {}).get("model", ""))
            print(
                f"[models] switch complete target={current} "
                f"ready={'yes' if srv.router.engine_ready else 'no'}",
                flush=True,
            )
            await srv.hub.broadcast(
                _local_models_msg(srv, current=current, switching=False)
            )
            if outcome in {"failed", "rolled_back"}:
                await srv.hub.broadcast({
                    "type": "models:error",
                    "operation": "switch",
                    "attempted": path,
                    "current": current,
                    "outcome": outcome,
                    "error": failure or (
                        "The model could not be loaded; the previous selection was restored."
                    ),
                })


def register(on):
    # ---- Engine / model / voice config --------------------------------------------

    @on("config:get")
    async def _config_get(srv, websocket, session, msg):
        await websocket.send_json(srv.require_runtime().models.config_status())

    @on("hardware:telemetry")
    async def _hardware_telemetry(srv, websocket, session, msg):
        # Live per-device snapshot for Hardware + Overview widgets.
        snap = await asyncio.to_thread(hardware.telemetry)
        if isinstance(snap, dict):
            snap = {**snap, "type": "hardware:telemetry"}
        else:
            snap = {"type": "hardware:telemetry"}
        srv.inference_observability.observe_hardware(snap)
        await websocket.send_json(snap)

    @on("inference:telemetry")
    async def _inference_telemetry(srv, websocket, session, msg):
        snap = srv.router.inference_snapshot()
        if isinstance(snap, dict):
            snap = {**snap, "type": "inference:telemetry"}
        else:
            snap = {"type": "inference:telemetry"}
        srv.inference_observability.observe_inference(snap)
        await websocket.send_json(snap)

    @on("model:request_manifests")
    async def _model_request_manifests(srv, websocket, session, msg):
        """Return the bounded metadata-only record of actual provider requests.

        Optional ``manifest_id`` returns a single receipt for inspector
        drill-down. Exact prompt/tool/image payloads are never stored here.
        """
        manifest_id = str((msg or {}).get("manifest_id") or "").strip()
        await websocket.send_json(
            srv.router.model_request_manifest_snapshot(manifest_id=manifest_id))

    @on("cloud:usage")
    async def _cloud_usage(srv, websocket, session, msg):
        await websocket.send_json({"type": "cloud:usage", "cloud_usage": srv.router.usage_snapshot()})

    @on("model:usage")
    async def _model_usage(srv, websocket, session, msg):
        try:
            days = max(1, min(90, int(msg.get("days") or 30)))
        except (TypeError, ValueError):
            days = 30
        await websocket.send_json({
            "type": "model:usage",
            "model_usage": srv.router.model_usage_snapshot(days=days),
        })

    @on("mode:set")
    @session_settings_command("mode:set")
    async def _mode_set(srv, websocket, session, msg):
        mode = str(msg.get("mode") or "").strip().lower()
        if mode not in {"local", "cloud"}:
            await websocket.send_json({
                "type": "error",
                "error": "mode must be 'local' or 'cloud'",
            })
            return
        if str(msg.get("scope") or "").strip().lower() == "session":
            from model_runtime.context import (
                normalize_model_route,
                projection_budget_tokens,
                session_model_route,
            )
            from session_context import estimated_session_context
            from session_projection import project_session_conversation
            from transcript_economy import approx_tokens, ctx_compress_threshold

            from ws_protocol import chat_is_busy
            sid = str(msg.get("id") or "").strip() or srv.require_runtime().chat.viewed_session_id(session)
            if chat_is_busy(srv.require_runtime(), session, sid):
                await websocket.send_json({
                    "type": "error",
                    "code": "session_busy_model_switch",
                    "error": "Wait for the active turn to finish before switching models.",
                })
                return
            if not sid or not _sessions(srv).has_session(sid):
                await websocket.send_json({
                    "type": "error",
                    "code": "unknown_session",
                    "error": "Cannot switch the model for an unknown chat session.",
                })
                return
            current = session_model_route(_sessions(srv), sid, srv.router)
            requested_effort = str(msg.get("reasoning_effort") or "").strip().lower()
            target = normalize_model_route(
                srv.router,
                {"reasoning_effort": requested_effort} if requested_effort else None,
                mode=mode,
                provider=str(msg.get("provider") or "") or None,
                model=str(msg.get("model") or "") or None,
            )
            runtimes = srv.require_runtime().session_runtimes
            try:
                from agent_engine.session_capabilities import (
                    effective_action_surface,
                )
                from session_catalog.support import UnsupportedModelRoute
                from model_runtime.context import (
                    model_route_support_coordinates,
                )

                record = runtimes.ensure_runtime(sid)
                support_profile = effective_action_surface(
                    record.identity.action_surface,
                    record,
                )
                coordinates = model_route_support_coordinates(
                    srv.router, target
                )
                srv.router._support_matrix.validate(
                    profile=support_profile,
                    **coordinates,
                )
            except UnsupportedModelRoute as exc:
                await websocket.send_json({
                    "type": "error",
                    "code": "unsupported_model_route",
                    "error": str(exc),
                })
                return
            budget = projection_budget_tokens(srv.router, target)
            compressor = getattr(srv, "_compress_messages", None)
            try:
                with srv.router.bind_model_route(current):
                    from session_projection import projection_model_route

                    projected = await project_session_conversation(
                        _sessions(srv),
                        sid,
                        mode=target["mode"],
                        context_limit_tokens=budget,
                        compress_messages=compressor,
                        model_route=projection_model_route(srv.router, _sessions(srv), sid, selected=target),
                        snapshot_store=getattr(getattr(srv.require_runtime(), "session_runtimes", None), "snapshot_store", None),
                    )
            except Exception as exc:
                await websocket.send_json({
                    "type": "error",
                    "code": "context_compaction_failed",
                    "error": f"Could not prepare this session for the selected model: {exc}",
                })
                return
            threshold = ctx_compress_threshold(
                mode=target["mode"], ctx_size=budget)
            if approx_tokens(projected) > threshold:
                await websocket.send_json({
                    "type": "error",
                    "code": "context_compaction_failed",
                    "error": "This session could not be compacted enough for the selected model.",
                })
                return
            if target["mode"] == "local" and str(msg.get("model") or "").strip():
                try:
                    local_model, local_mmproj = _local_picker_model(
                        srv,
                        str(msg.get("model") or ""),
                    )
                except ValueError as exc:
                    await websocket.send_json({
                        "type": "error",
                        "code": "local_model_unavailable",
                        "error": str(exc),
                    })
                    return
                target["model"] = local_model
                configured = str(
                    (srv.router.cfg.get("local", {}) or {}).get("model") or ""
                )
                if (
                    srv.router.inference_runtime_id == "llamacpp"
                    and _model_identity_key(local_model)
                    != _model_identity_key(configured)
                ):
                    outcome = await srv.require_runtime().models.restart_engine(
                        local_model,
                        local_mmproj,
                    )
                    if outcome != "switched":
                        await websocket.send_json({
                            "type": "error",
                            "code": "local_model_switch_failed",
                            "error": "The local model could not be loaded.",
                        })
                        return
            if not _sessions(srv).set_model_route(sid, target):
                await websocket.send_json({
                    "type": "error",
                    "code": "model_switch_failed",
                    "error": "Could not save the session model route.",
                })
                return
            mark_settings_applied(websocket)
            if target["mode"] == "local" and not srv.router.engine_ready:
                try:
                    from model_runtime import engine_manager
                    with srv.router.bind_model_route(target):
                        await engine_manager.ensure_local_engine(srv.router)
                except Exception:
                    # The pinned route remains valid; the normal engine status
                    # and next-turn gate explain missing local runtime/model.
                    pass
            await websocket.send_json(estimated_session_context(
                sid,
                projected,
                context_limit_tokens=srv.router.context_limit_tokens(target),
                route=target,
            ))
            return
        srv.router.set_mode(mode)
        if msg.get("provider"):
            srv.router.set_cloud_provider(msg.get("provider"))
        await srv.hub.broadcast(srv.engine_status_message())
        await websocket.send_json(srv.require_runtime().models.config_status())
        background_tasks.spawn(
            _reconcile_local_engine_policy(srv, "route change"),
            name="local-engine-route-change",
        )


    @on("local:prewarm:set")
    async def _local_prewarm_set(srv, websocket, session, msg):
        srv.router.set_local_prewarm(bool(msg.get("value")))
        await srv.hub.broadcast(srv.engine_status_message())
        await websocket.send_json(srv.require_runtime().models.config_status())
        background_tasks.spawn(
            _reconcile_local_engine_policy(srv, "prewarm change"),
            name="local-engine-prewarm-change",
        )


    @on("tts:set")
    async def _tts_set(srv, websocket, session, msg):
        key = str(msg.get("key") or "")
        request_id = str(msg.get("request_id") or "")
        try:
            srv.require_runtime().voice.set_config(key, msg.get("value"))
        except Exception as exc:
            if request_id:
                await websocket.send_json({
                    "type": "speech:rejected",
                    "request_id": request_id,
                    "error": str(exc),
                })
                return
            raise
        if request_id:
            await websocket.send_json({
                "type": "speech:accepted",
                "request_id": request_id,
            })
        await srv.hub.broadcast(srv.engine_status_message())
        if key in {"tts_provider", "voice"}:
            background_tasks.spawn(
                _send_tts_voices(srv, broadcast=True),
                name="tts-voices-provider-refresh",
            )

    @on("speech:credential:set")
    async def _speech_credential_set(srv, websocket, session, msg):
        from service_credentials import replace
        from speech.providers import STT_PROVIDERS, TTS_PROVIDERS
        capability = str(msg.get("capability") or "").strip().lower()
        provider = str(msg.get("provider") or "").strip().lower()
        known = {
            "tts": {row["id"] for row in TTS_PROVIDERS},
            "stt": {row["id"] for row in STT_PROVIDERS},
        }
        try:
            if capability not in known or provider not in known[capability]:
                raise ValueError("unknown speech provider")
            replace(srv.router, capability, provider, str(msg.get("key") or ""))
        except Exception as exc:
            await websocket.send_json({"type": "speech:rejected",
                "request_id": str(msg.get("request_id") or ""), "error": str(exc)})
            return
        await websocket.send_json({"type": "speech:accepted",
            "request_id": str(msg.get("request_id") or "")})
        await srv.hub.broadcast(srv.engine_status_message())

    @on("speech:credential:clear")
    async def _speech_credential_clear(srv, websocket, session, msg):
        from service_credentials import clear
        capability = str(msg.get("capability") or "").strip().lower()
        provider = str(msg.get("provider") or "").strip().lower()
        try:
            if capability not in {"tts", "stt"}:
                raise ValueError("unknown speech capability")
            clear(srv.router, capability, provider)
        except Exception as exc:
            await websocket.send_json({"type": "speech:rejected",
                "request_id": str(msg.get("request_id") or ""), "error": str(exc)})
            return
        await websocket.send_json({"type": "speech:accepted",
            "request_id": str(msg.get("request_id") or "")})
        await srv.hub.broadcast(srv.engine_status_message())

    @on("tts:voices")
    async def _tts_voices(srv, websocket, session, msg):
        await _send_tts_voices(srv, websocket)

    @on("tts:preview")
    async def _tts_preview(srv, websocket, session, msg):
        # purpose/request_id are echoed so Main Deck can route chat "Play"
        # responses without clobbering Settings voice samples (and vice versa).
        purpose = str(msg.get("purpose") or "preview").strip() or "preview"
        request_id = str(msg.get("request_id") or "").strip() or (
            "tts-" + uuid.uuid4().hex
        )
        viewed = str(srv.require_runtime().chat.viewed_session_id(session) or "")
        supplied_session = str(msg.get("session_id") or "").strip()
        origin_session = supplied_session or (viewed if purpose == "chat" else "")
        if purpose == "chat" and supplied_session and supplied_session != viewed:
            await _send_tts_preview_terminal(
                websocket, session, request_id=request_id, purpose=purpose,
                session_id=supplied_session,
                error="chat changed before speech synthesis started",
            )
            return
        await _cancel_tts_preview(websocket, session)
        sample = str(msg.get("text") or "Hello from VARIANT-1.").strip() or "Hello from VARIANT-1."
        if purpose == "chat":
            sample = sample[:12000]
        session.tts_preview_request_id = request_id
        session.tts_preview_session_id = origin_session
        session.tts_preview_task = asyncio.create_task(
            _run_tts_preview(
                srv, websocket, session, request_id=request_id,
                purpose=purpose, session_id=origin_session, text=sample,
                voice_id=str(msg.get("voice") or ""),
            ),
            name=f"tts-preview:{request_id}",
        )

    @on("tts:preview:cancel")
    async def _tts_preview_cancel(srv, websocket, session, msg):
        await _cancel_tts_preview(
            websocket, session,
            request_id=str(msg.get("request_id") or ""),
        )

    @on("voice:transcribe")
    async def _voice_transcribe(srv, websocket, session, msg):
        voice = srv.require_runtime().voice
        request_id = str(msg.get("request_id") or "").strip() or (
            "stt-" + uuid.uuid4().hex
        )
        viewed = str(srv.require_runtime().chat.viewed_session_id(session) or "")
        supplied_session = str(msg.get("session_id") or "").strip()
        origin_session = supplied_session or viewed
        if supplied_session and supplied_session != viewed:
            await voice._send_transcript_terminal(
                websocket, session, request_id=request_id,
                session_id=supplied_session, text="",
                error="chat changed before transcription started",
            )
            return
        b64 = msg.get("audio") or ""
        try:
            wav = base64.b64decode(b64) if b64 else b""
        except Exception as e:
            await voice._send_transcript_terminal(
                websocket, session, request_id=request_id,
                session_id=origin_session, text="",
                error=f"transcription failed: {e}",
            )
            wav = None
        if wav is not None:
            await voice.cancel_transcription(websocket, session)
            # Run as a background task so the receive loop stays free to
            # handle a cancel/stop message (or a new chat turn) while
            # whisper.cpp is still working.
            session.transcribe_request_id = request_id
            session.transcribe_session_id = origin_session
            session.transcribe_task = asyncio.create_task(
                voice.transcribe_task(
                    websocket, session, wav, msg.get("language"),
                    request_id=request_id, session_id=origin_session,
                ),
                name=f"stt-transcribe:{request_id}",
            )

    @on("voice:transcribe:cancel")
    async def _voice_transcribe_cancel(srv, websocket, session, msg):
        await srv.require_runtime().voice.cancel_transcription(
            websocket, session,
            request_id=str(msg.get("request_id") or ""),
            session_id=str(msg.get("session_id") or ""),
        )


    # ---- Feature toggles ------------------------------------------------------------

    # ---- Local / cloud models --------------------------------------------------------

    @on("model:list")
    async def _model_list(srv, websocket, session, msg):
        try:
            await websocket.send_json(_local_models_msg(srv))
        except Exception as exc:
            await websocket.send_json({
                "type": "models:error",
                "error": str(exc),
            })


    @on("model:set")
    async def _model_set(srv, websocket, session, msg):
        if srv.router.inference_runtime_id != "llamacpp":
            await websocket.send_json({
                "type": "error",
                "error": "Switch to the bundled llama.cpp runtime before changing GGUF models.",
            })
            return
        path = str(msg.get("path") or "")
        mm = str(msg.get("mmproj") or "")
        if path:
            generation = int(getattr(srv, "_local_model_switch_generation", 0)) + 1
            srv._local_model_switch_generation = generation
            srv._local_model_switching = True
            srv._local_model_switch_target = path
            print(
                f"[models] switch requested target={os.path.basename(path)} "
                f"vision={'yes' if mm else 'no'} generation={generation}",
                flush=True,
            )
            await srv.hub.broadcast(
                _local_models_msg(
                    srv,
                    current=path,
                    switching=True,
                )
            )
            background_tasks.spawn(
                _switch_local_model(srv, path, mm, generation),
                name="model-engine-restart",
            )


    async def _broadcast_config(srv):
        """Cloud/config mutations must reach every open surface, not only the requester."""
        await srv.hub.broadcast(srv.require_runtime().models.config_status())
        await srv.hub.broadcast(srv.engine_status_message())

    @on("cloud:custom-endpoints:list")
    async def _cloud_custom_endpoints_list(srv, websocket, session, msg):
        await websocket.send_json({
            "type": "cloud:custom-endpoints",
            "items": srv.router.list_custom_endpoints(),
        })

    @on("cloud:custom-endpoint:validate")
    async def _cloud_custom_endpoint_validate(srv, websocket, session, msg):
        value = msg.get("endpoint") if isinstance(msg.get("endpoint"), dict) else msg
        request_id = str(msg.get("request_id") or "")
        try:
            result = await srv.router.validate_custom_endpoint(value)
        except Exception as exc:
            result = {
                "ok": False,
                "reachable": False,
                "models": [],
                "message": str(exc),
            }
        await websocket.send_json({
            "type": "cloud:custom-endpoint:validated",
            "request_id": request_id,
            "operation": "validate",
            **result,
        })

    @on("cloud:custom-endpoint:save")
    async def _cloud_custom_endpoint_save(srv, websocket, session, msg):
        value = msg.get("endpoint") if isinstance(msg.get("endpoint"), dict) else msg
        request_id = str(msg.get("request_id") or "")
        try:
            endpoint = srv.router.save_custom_endpoint(value)
        except Exception as exc:
            await websocket.send_json({
                "type": "cloud:custom-endpoint:error",
                "request_id": request_id,
                "operation": "save",
                "error": str(exc),
            })
            return
        await websocket.send_json({
            "type": "cloud:custom-endpoint:saved",
            "request_id": request_id,
            "operation": "save",
            "endpoint": endpoint,
        })
        await _broadcast_config(srv)

    @on("cloud:custom-endpoint:activate")
    async def _cloud_custom_endpoint_activate(srv, websocket, session, msg):
        request_id = str(msg.get("request_id") or "")
        try:
            endpoint = srv.router.activate_custom_endpoint(str(msg.get("id") or ""))
        except Exception as exc:
            await websocket.send_json({
                "type": "cloud:custom-endpoint:error",
                "request_id": request_id,
                "operation": "activate",
                "error": str(exc),
            })
            return
        await websocket.send_json({
            "type": "cloud:custom-endpoint:activated",
            "request_id": request_id,
            "operation": "activate",
            "endpoint": endpoint,
        })
        await _broadcast_config(srv)

    @on("cloud:custom-endpoint:remove")
    async def _cloud_custom_endpoint_remove(srv, websocket, session, msg):
        request_id = str(msg.get("request_id") or "")
        endpoint_id = str(msg.get("id") or "")
        was_active = any(
            row.get("id") == endpoint_id and row.get("is_current")
            for row in srv.router.list_custom_endpoints()
        )
        try:
            removed = srv.router.remove_custom_endpoint(endpoint_id)
        except Exception as exc:
            await websocket.send_json({
                "type": "cloud:custom-endpoint:error",
                "request_id": request_id,
                "operation": "remove",
                "id": endpoint_id,
                "error": str(exc),
            })
            return
        await websocket.send_json({
            "type": "cloud:custom-endpoint:removed",
            "request_id": request_id,
            "operation": "remove",
            "removed": removed,
            "id": endpoint_id,
            "fallback_mode": "local" if removed and was_active else "",
        })
        await _broadcast_config(srv)

    def _oauth_attempts(srv) -> dict:
        value = getattr(srv, "_oauth_attempts", None)
        if not isinstance(value, dict):
            value = {}
            srv._oauth_attempts = value
        return value

    def _oauth_owner(srv, provider: str, attempt_id: str) -> bool:
        current = _oauth_attempts(srv).get(provider) or {}
        return str(current.get("id") or "") == attempt_id

    async def _cancel_oauth_attempt(srv, provider: str, request_id: str = "") -> bool:
        attempts = _oauth_attempts(srv)
        current = attempts.get(provider) or {}
        if not current or (request_id and str(current.get("id") or "") != request_id):
            return False
        task = current.get("task")
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if attempts.get(provider) is current:
            attempts.pop(provider, None)
        return True


    @on("cloud:oauth:start")
    async def _cloud_oauth_start(srv, websocket, session, msg):
        provider = srv.router._kn(str(msg.get("provider") or ""))
        request_id = str(msg.get("request_id") or uuid.uuid4().hex).strip()[:128]
        if provider not in {"openai-codex", "xai", "minimax-oauth", "minimax-oauth-cn", "google-antigravity"}:
            await websocket.send_json({
                "type": "cloud:oauth:error",
                "provider": provider,
                "request_id": request_id,
                "error": "Interactive OAuth is not available for this provider",
            })
            return
        attempts = _oauth_attempts(srv)
        prior = attempts.get(provider) or {}
        prior_task = prior.get("task")
        if prior_task is not None and not prior_task.done():
            await websocket.send_json({
                "type": "cloud:oauth:busy",
                "provider": provider,
                "request_id": request_id,
                "error": f"A {provider} login is already waiting for approval",
            })
            return
        attempt_id = request_id
        open_browser = msg.get("open_browser", True) is not False

        async def finish_login() -> None:
            try:
                if provider == "openai-codex":
                    import openai_codex_oauth

                    grant = await openai_codex_oauth.request_device_code()
                    if not _oauth_owner(srv, provider, attempt_id):
                        return
                    await websocket.send_json({
                        "type": "cloud:oauth:pending",
                        "provider": provider,
                        "request_id": attempt_id,
                        "verification_url": grant.verification_uri,
                        "user_code": grant.user_code,
                        "expires_in": grant.expires_in,
                    })
                    if open_browser:
                        openai_codex_oauth.open_verification(grant)
                    tokens = await openai_codex_oauth.poll_device_code(grant)
                    if not _oauth_owner(srv, provider, attempt_id):
                        return
                    srv.router.set_oauth_tokens(
                        provider,
                        client_id=openai_codex_oauth.CODEX_OAUTH_CLIENT_ID,
                        access_token=tokens.access_token,
                        refresh_token=tokens.refresh_token,
                        token_type=tokens.token_type,
                        scope=tokens.scope,
                        expires_at=tokens.expires_at,
                        auth_flow="device_code",
                        account_id=tokens.account_id,
                        managed_external=False,
                        replace=True,
                    )
                elif provider == "xai":
                    import xai_oauth

                    config = xai_oauth.XaiOAuthConfig.from_env()
                    loop = asyncio.get_running_loop()
                    pending_notifications: list[asyncio.Task] = []

                    def on_authorize_url(url: str, redirect_uri: str) -> None:
                        if not _oauth_owner(srv, provider, attempt_id):
                            return
                        pending_notifications.append(loop.create_task(
                            websocket.send_json({
                                "type": "cloud:oauth:pending",
                                "provider": provider,
                                "request_id": attempt_id,
                                "verification_url": url,
                                "redirect_uri": redirect_uri,
                                "user_code": "",
                                "expires_in": int(xai_oauth.XAI_PKCE_CALLBACK_TIMEOUT),
                            })
                        ))

                    tokens = await xai_oauth.login_pkce(
                        config,
                        open_browser=open_browser,
                        on_authorize_url=on_authorize_url,
                    )
                    if pending_notifications:
                        await asyncio.gather(
                            *pending_notifications, return_exceptions=True
                        )
                    if not _oauth_owner(srv, provider, attempt_id):
                        return
                    srv.router.cfg.setdefault("cloud", {})[
                        "xai_credential_policy"
                    ] = "subscription_first"
                    srv.router.set_oauth_tokens(
                        provider,
                        client_id=config.client_id,
                        access_token=tokens.access_token,
                        refresh_token=tokens.refresh_token,
                        token_type=tokens.token_type,
                        scope=tokens.scope,
                        expires_at=tokens.expires_at,
                        auth_flow="pkce",
                        managed_external=False,
                        replace=True,
                    )
                elif provider == 'google-antigravity':
                    import google_ai_oauth

                    async def authorize_google(url):
                        if _oauth_owner(srv, provider, attempt_id):
                            await websocket.send_json({'type': 'cloud:oauth:pending', 'provider': provider,
                                'request_id': attempt_id, 'verification_url': url, 'user_code': '',
                                'expires_in': google_ai_oauth.CALLBACK_TIMEOUT})

                    tokens = await google_ai_oauth.login(open_browser=open_browser, on_authorize_url=authorize_google)
                    if not _oauth_owner(srv, provider, attempt_id):
                        return
                    srv.router.set_oauth_tokens(provider, client_id=google_ai_oauth.CLIENT_ID,
                        access_token=tokens.access_token, refresh_token=tokens.refresh_token,
                        token_type=tokens.token_type, scope=tokens.scope, expires_at=tokens.expires_at,
                        auth_flow='authorization_code_pkce', managed_external=False, replace=True,
                        project_id=tokens.project_id, account_id=tokens.account_id, account_tier=tokens.tier)
                else:
                    import minimax_oauth

                    grant = await minimax_oauth.request_user_code(provider)
                    if not _oauth_owner(srv, provider, attempt_id):
                        return
                    await websocket.send_json({
                        "type": "cloud:oauth:pending",
                        "provider": provider,
                        "request_id": attempt_id,
                        "verification_url": grant.verification_uri,
                        "user_code": grant.user_code,
                        "expires_in": grant.expires_in,
                    })
                    if open_browser:
                        minimax_oauth.open_verification(grant)
                    tokens = await minimax_oauth.poll_user_code(grant)
                    if not _oauth_owner(srv, provider, attempt_id):
                        return
                    srv.router.set_oauth_tokens(
                        provider,
                        client_id=minimax_oauth.CLIENT_ID,
                        access_token=tokens.access_token,
                        refresh_token=tokens.refresh_token,
                        token_type=tokens.token_type,
                        scope=tokens.scope,
                        expires_at=tokens.expires_at,
                        auth_flow="user_code_pkce",
                        managed_external=False,
                        replace=True,
                    )
                try:
                    await websocket.send_json({
                        "type": "cloud:oauth:complete",
                        "provider": provider,
                        "request_id": attempt_id,
                        "status": srv.router.oauth_status(provider),
                    })
                except Exception:
                    pass
                await _broadcast_config(srv)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                try:
                    await websocket.send_json({
                        "type": "cloud:oauth:error",
                        "provider": provider,
                        "request_id": attempt_id,
                        "error": str(exc),
                    })
                except Exception:
                    pass
            finally:
                if _oauth_owner(srv, provider, attempt_id):
                    _oauth_attempts(srv).pop(provider, None)

        task = background_tasks.spawn(
            finish_login(), name=f"{provider}-oauth-login"
        )
        attempts[provider] = {"id": attempt_id, "task": task}


    @on("cloud:oauth:cancel")
    async def _cloud_oauth_cancel(srv, websocket, session, msg):
        provider = srv.router._kn(str(msg.get("provider") or ""))
        request_id = str(msg.get("request_id") or "").strip()[:128]
        cancelled = await _cancel_oauth_attempt(srv, provider, request_id)
        await websocket.send_json({
            "type": "cloud:oauth:cancelled", "provider": provider,
            "request_id": request_id, "cancelled": cancelled,
        })


    @on("cloud:oauth:disconnect")
    async def _cloud_oauth_disconnect(srv, websocket, session, msg):
        provider = srv.router._kn(str(msg.get("provider") or ""))
        request_id = str(msg.get("request_id") or "").strip()[:128]
        if provider not in {"openai-codex", "xai", "minimax-oauth", "minimax-oauth-cn"}:
            await websocket.send_json({
                "type": "cloud:oauth:error",
                "provider": provider,
                "request_id": request_id,
                "error": "OAuth disconnect is not available for this provider",
            })
            return
        await _cancel_oauth_attempt(srv, provider)
        srv.router.clear_oauth(provider)
        await websocket.send_json({
            "type": "cloud:oauth:disconnected", "provider": provider,
            "request_id": request_id,
            "status": srv.router.oauth_status(provider),
        })
        await _broadcast_config(srv)

    @on("cloud:credential:set")
    async def _cloud_credential_set(srv, websocket, session, msg):
        prov = str(msg.get("provider") or srv.router.cloud_provider or "").strip()
        secret = str(msg.get("key") or msg.get("secret") or "")
        request_id = str(msg.get("request_id") or "")
        if not prov or not secret:
            await websocket.send_json({
                "type": "cloud:credential:rejected", "request_id": request_id,
                "provider": prov, "operation": "set",
                "error": "A provider and API key are required.",
            })
            return
        try:
            srv.router.replace_cloud_credential(prov, secret)
        except Exception as exc:
            await websocket.send_json({
                "type": "cloud:credential:rejected", "request_id": request_id,
                "provider": prov, "operation": "set", "error": str(exc),
            })
            return
        await websocket.send_json({
            "type": "cloud:credential:accepted", "request_id": request_id,
            "provider": prov, "operation": "set",
        })
        await _broadcast_config(srv)


    @on("cloud:credential:clear")
    async def _cloud_credential_clear(srv, websocket, session, msg):
        prov = str(msg.get("provider") or srv.router.cloud_provider or "").strip()
        request_id = str(msg.get("request_id") or "")
        if not prov:
            await websocket.send_json({
                "type": "cloud:credential:rejected", "request_id": request_id,
                "provider": prov, "operation": "clear",
                "error": "A provider is required.",
            })
            return
        try:
            srv.router.clear_cloud_credentials(prov)
        except Exception as exc:
            await websocket.send_json({
                "type": "cloud:credential:rejected", "request_id": request_id,
                "provider": prov, "operation": "clear", "error": str(exc),
            })
            return
        await websocket.send_json({
            "type": "cloud:credential:accepted", "request_id": request_id,
            "provider": prov, "operation": "clear",
        })
        await _broadcast_config(srv)

    def _credential_snapshot(srv, provider: str, request_id: str = "") -> dict:
        return {
            "type": "cloud:credential:items", "provider": provider,
            "request_id": request_id,
            **srv.router.credential_pools.snapshot(provider),
        }

    @on("cloud:credential:list")
    async def _cloud_credential_list(srv, websocket, session, msg):
        provider = srv.router._kn(str(msg.get("provider") or srv.router.cloud_provider or ""))
        await websocket.send_json(_credential_snapshot(srv, provider, str(msg.get("request_id") or "")[:128]))

    @on("cloud:credential:add")
    async def _cloud_credential_add(srv, websocket, session, msg):
        provider = srv.router._kn(str(msg.get("provider") or ""))
        request_id = str(msg.get("request_id") or "")[:128]
        secret = str(msg.get("key") or msg.get("secret") or "")
        try:
            if srv.router.provider_profile(provider) is None or not secret:
                raise ValueError("A known provider and API key are required")
            record = srv.router.add_cloud_credential(
                provider, secret,
                label=str(msg.get("label") or "")[:80],
                priority=(int(msg["priority"]) if msg.get("priority") is not None else None),
            )
        except Exception as exc:
            await websocket.send_json({
                "type": "cloud:credential:rejected", "request_id": request_id,
                "provider": provider, "operation": "add", "error": str(exc),
            })
            return
        await websocket.send_json({
            "type": "cloud:credential:accepted", "request_id": request_id,
            "provider": provider, "operation": "add", "credential": record,
        })
        await websocket.send_json(_credential_snapshot(srv, provider))
        await _broadcast_config(srv)

    @on("cloud:credential:remove")
    async def _cloud_credential_remove(srv, websocket, session, msg):
        provider = srv.router._kn(str(msg.get("provider") or ""))
        credential_id = str(msg.get("credential_id") or "")
        request_id = str(msg.get("request_id") or "")[:128]
        try:
            if not srv.router.remove_cloud_credential(provider, credential_id):
                raise ValueError("credential was not found")
        except Exception as exc:
            await websocket.send_json({
                "type": "cloud:credential:rejected", "request_id": request_id,
                "provider": provider, "operation": "remove", "error": str(exc),
            })
            return
        await websocket.send_json({
            "type": "cloud:credential:accepted", "request_id": request_id,
            "provider": provider, "operation": "remove", "credential_id": credential_id,
        })
        await websocket.send_json(_credential_snapshot(srv, provider))
        await _broadcast_config(srv)

    @on("cloud:credential:enable")
    async def _cloud_credential_enable(srv, websocket, session, msg):
        provider = srv.router._kn(str(msg.get("provider") or ""))
        credential_id = str(msg.get("credential_id") or "")
        request_id = str(msg.get("request_id") or "")[:128]
        enabled = msg.get("enabled") is True
        try:
            if not srv.router.set_cloud_credential_enabled(provider, credential_id, enabled):
                raise ValueError("credential was not found")
        except Exception as exc:
            await websocket.send_json({
                "type": "cloud:credential:rejected", "request_id": request_id,
                "provider": provider, "operation": "enable", "error": str(exc),
            })
            return
        await websocket.send_json({
            "type": "cloud:credential:accepted", "request_id": request_id,
            "provider": provider, "operation": "enable",
            "credential_id": credential_id, "enabled": enabled,
        })
        await websocket.send_json(_credential_snapshot(srv, provider))
        await _broadcast_config(srv)

    @on("cloud:credential:priority:set")
    async def _cloud_credential_priority(srv, websocket, session, msg):
        provider = srv.router._kn(str(msg.get("provider") or ""))
        credential_id = str(msg.get("credential_id") or "")
        request_id = str(msg.get("request_id") or "")[:128]
        try:
            priority = int(msg.get("priority"))
            if not srv.router.set_cloud_credential_priority(provider, credential_id, priority):
                raise ValueError("credential was not found")
        except Exception as exc:
            await websocket.send_json({
                "type": "cloud:credential:rejected", "request_id": request_id,
                "provider": provider, "operation": "priority", "error": str(exc),
            })
            return
        await websocket.send_json({
            "type": "cloud:credential:accepted", "request_id": request_id,
            "provider": provider, "operation": "priority",
            "credential_id": credential_id, "priority": priority,
        })
        await websocket.send_json(_credential_snapshot(srv, provider))
        await _broadcast_config(srv)

    @on("cloud:credential:strategy:set")
    async def _cloud_credential_strategy(srv, websocket, session, msg):
        provider = srv.router._kn(str(msg.get("provider") or ""))
        request_id = str(msg.get("request_id") or "")[:128]
        strategy = str(msg.get("strategy") or "")
        try:
            srv.router.set_cloud_credential_strategy(provider, strategy)
        except Exception as exc:
            await websocket.send_json({
                "type": "cloud:credential:rejected", "request_id": request_id,
                "provider": provider, "operation": "strategy", "error": str(exc),
            })
            return
        await websocket.send_json({
            "type": "cloud:credential:accepted", "request_id": request_id,
            "provider": provider, "operation": "strategy", "strategy": strategy,
        })
        await websocket.send_json(_credential_snapshot(srv, provider))
        await _broadcast_config(srv)


    @on("doctor:run")
    async def _doctor_run(srv, websocket, session, msg):
        # Doctor: one-shot "is my setup safe/sane?" health check.
        try:
            res = doctor.run_checks(srv.require_runtime().models.doctor_snapshot())
        except Exception as e:
            res = {"summary": "warn", "findings": [
                {"level": "warn", "title": "Health check failed to run",
                 "detail": str(e), "fix": ""}]}
        await websocket.send_json({"type": "doctor:result", **res})



