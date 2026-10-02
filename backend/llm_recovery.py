"""Recovery inside the canonical router, never a second agent/tool loop."""
from __future__ import annotations

from contextlib import aclosing
import json

from model_providers import ProviderRequestError
from model_runtime.context import normalize_model_route, context_limit_tokens, validate_worker_model_route
from model_runtime.provider_recovery import REQUEST_BUDGET, RequestBudget, classify_provider_error


def route_key(router, route):
    return router._recovery_cooldowns.key(router, route)


def normalized_routes(router, rows):
    return [normalize_model_route(router, row) for row in rows]


def initial_effective_route(router, primary, policy, allow_recovery=True):
    if not allow_recovery or not policy["enabled"]:
        return primary
    if not router._recovery_cooldowns.remaining(route_key(router, primary)):
        return primary
    for candidate in normalized_routes(router, policy["fallback_routes"]):
        if route_key(router, candidate) != route_key(router, primary) and not router._recovery_cooldowns.remaining(route_key(router, candidate)):
            return candidate
    return primary


def portable_messages(messages):
    # The existing typed graph owns call/result relationships and portable
    # media. Its Chat-Completions rendering omits signed/encrypted replay
    # metadata. Never mutate the canonical input or manufacture a tool result.
    from model_runtime.message_graph import build_message_graph, render_openai_chat, ToolResultPart
    graph = build_message_graph(messages)
    projected = render_openai_chat(graph)
    for node, row in zip(graph.messages, projected):
        if any(isinstance(part, ToolResultPart) and part.is_error for part in node.parts):
            row["is_error"] = True
    return projected


def _note(event, **fields):
    try:
        from observability.trace_events import record_trace_event
        record_trace_event("provider:recovery", recovery_event=event, **fields)
    except Exception:
        pass


def _admit_context(router, route, messages, options):
    validate_worker_model_route(router, route)
    from model_runtime.context import model_route_support_coordinates
    if route["mode"] == "cloud" and router.provider_profile(route["provider"]) is None:
        raise ValueError("unknown configured recovery provider")
    coordinates = model_route_support_coordinates(router, route)
    router.validate_model_request(**coordinates, tools=options.get("tools"),
                                  internal_projection=bool(options.get("internal_projection")))
    limit = context_limit_tokens(router, route)
    from transcript_economy import approx_tokens
    tools = options.get("tools") or []
    schema_chars = len(json.dumps(tools, ensure_ascii=False, separators=(",", ":")))
    estimated = approx_tokens(messages) + (schema_chars + 2) // 3
    sampling = {**getattr(router, "sampling", {}), **(options.get("sampling") or {})}
    reserve = int(sampling.get("max_tokens") or 1024)
    _note("context_admission", provider=route["provider"], model=route["model"],
          estimated_input_tokens=estimated, output_reserve=reserve,
          known_context_limit=limit or None, estimate_kind="text_and_schema_estimate_media_unmeasured")
    if limit and estimated + reserve > limit:
        error = ProviderRequestError(route["provider"], "Configured recovery route cannot fit estimated input and output reserve", status_code=400)
        error.provider_error_type = "context_window_exceeded"
        raise error


async def stream_with_recovery(router, messages, options, *, recovery_profile=None, allow_recovery=True):
    scope = router.recovery_scope()
    requested_mode = options.get("route")
    if scope is None and allow_recovery and not router.provider_recovery_policy()["enabled"]:
        # Preserve the existing explicitly configured unbound provider-chain
        # contract; bound chat requests still never enter that legacy chain.
        budget = RequestBudget()
        token = REQUEST_BUDGET.set(budget)
        try:
            selected_mode = requested_mode if requested_mode in {"local", "cloud"} else router.mode
            if selected_mode == "local":
                budget.consume("local")
            async with aclosing(router._stream_selected(messages, **options)) as stream:
                async for part in stream:
                    yield part
        finally:
            REQUEST_BUDGET.reset(token)
        return
    effective_mode = (router.bound_model_route() or scope.primary)["mode"] if scope is not None else None
    if scope is None or (requested_mode in {"local", "cloud"} and requested_mode != effective_mode):
        selected = normalize_model_route(router, mode=requested_mode if requested_mode in {"local", "cloud"} else router.mode)
        with router.bind_model_route(selected, allow_recovery=allow_recovery):
            # An explicit per-call mode override must not inherit native state
            # from the different route that owns the enclosing run.
            override_messages = portable_messages(messages) if scope is not None else messages
            async with aclosing(stream_with_recovery(router, override_messages, options,
                    recovery_profile=recovery_profile, allow_recovery=allow_recovery)) as stream:
                async for token in stream:
                    yield token
        return

    policy = scope.policy
    enabled = bool(allow_recovery and scope.allow_recovery and policy["enabled"])
    auxiliary = bool(options.get("internal_projection"))
    explicit_auxiliary = policy["auxiliary_routes"].get(recovery_profile, []) if enabled else []
    if explicit_auxiliary:
        candidates = normalized_routes(router, explicit_auxiliary)
        primary = candidates[0]
    else:
        primary = scope.primary
        current = router.bound_model_route() if enabled else primary
        candidates = [current or primary]
        if enabled:
            candidates.extend(normalized_routes(router, policy["fallback_routes"]))
    budget = RequestBudget(policy["max_attempts"], policy["max_wait_seconds"])
    budget_token = REQUEST_BUDGET.set(budget)
    last_error = None
    attempted = set()
    try:
        for index, candidate in enumerate(candidates):
            key = route_key(router, candidate)
            if key in attempted:
                continue
            attempted.add(key)
            if enabled:
                remaining = router._recovery_cooldowns.remaining(key)
                if remaining:
                    last_error = ProviderRequestError(candidate["provider"],
                        f"Provider retry is not yet eligible; approximately {int(remaining) + 1} seconds remain", status_code=429,
                        retry_after_seconds=remaining)
                    last_error.failure_kind = "cooldown"
                    _note("cooldown_skipped", provider=candidate["provider"], model=candidate["model"], remaining_seconds=remaining)
                    continue
            if budget.attempts >= budget.max_attempts:
                break
            pending_keys = {route_key(router, row) for row in candidates[index + 1:]}
            pending_keys.difference_update(attempted)
            budget.begin_candidate(len(pending_keys) if enabled else 0)
            changed = route_key(router, candidate) != route_key(router, scope.primary)
            call_messages = portable_messages(messages) if changed or scope.force_portable else messages
            call_options = {**options, "route": candidate["mode"]}
            # A candidate's exact context limit/capabilities must be re-admitted
            # before OAuth/model I/O. Don't replace primary compaction policy.
            if changed or explicit_auxiliary:
                try:
                    _admit_context(router, candidate, call_messages, call_options)
                except (ProviderRequestError, ValueError) as error:
                    last_error = error
                    _note("candidate_rejected", provider=candidate["provider"], model=candidate["model"], reason="context_or_local_identity")
                    continue
            visible = False
            diagnostics = call_options.get("stream_diagnostics")
            before_tools = int(getattr(diagnostics, "tool_deltas", 0) or 0)
            try:
                with router.temporary_model_route(candidate):
                    if candidate["mode"] == "local":
                        if enabled and not router.engine_ready:
                            raise ProviderRequestError("local", "Selected local engine is unavailable", status_code=404)
                        budget.consume("local")
                    async with aclosing(router._stream_selected(call_messages, **call_options)) as stream:
                        async for token in stream:
                            visible = visible or bool(token)
                            yield token
                router._recovery_cooldowns.succeeded(key)
                if enabled and changed and not auxiliary:
                    router.promote_effective_route(candidate)
                    _note("fallback_accepted", provider=candidate["provider"], model=candidate["model"], attempts=budget.attempts)
                return
            except Exception as error:
                last_error = error
                emitted_tools = int(getattr(diagnostics, "tool_deltas", 0) or 0) > before_tools
                if visible or emitted_tools or bool(getattr(error, "model_output_observed", False)):
                    if isinstance(error, ProviderRequestError):
                        error.model_output_observed = True
                        error.clean_turn_replay_safe = False
                    raise
                failure = classify_provider_error(error)
                if enabled:
                    router._recovery_cooldowns.failed(key, failure)
                if not enabled or not failure.fallback:
                    raise
                _note("fallback_candidate_failed", provider=candidate["provider"], model=candidate["model"], reason=failure.kind,
                      attempts=budget.attempts)
        if last_error is not None:
            # This router has already exhausted its logical-request policy;
            # the agent layer must not silently restart it as another episode.
            if isinstance(last_error, ProviderRequestError):
                last_error.clean_turn_replay_safe = False
            raise last_error
        raise ProviderRequestError(primary["provider"], "No configured recovery route was admitted")
    finally:
        REQUEST_BUDGET.reset(budget_token)


def enter_recovery_checkpoint(state):
    """Resume route provenance once per native run, before any model boundary."""
    from model_runtime.provider_recovery import ROUTE_SCOPE
    scope = ROUTE_SCOPE.get()
    if scope is None or scope.router is None:
        return
    run_id = str(state.get("run_id") or "")
    if scope.restored_run_id == run_id:
        return
    scope.restored_run_id = run_id
    scope.router.restore_recovery_checkpoint(state.get("model_recovery"))


def recovery_checkpoint_state():
    from model_runtime.provider_recovery import ROUTE_SCOPE
    scope = ROUTE_SCOPE.get()
    if scope is None or scope.router is None:
        return None
    return {"schema": "variant1.model-recovery-state.v1", "primary": dict(scope.primary),
            "effective": dict(scope.effective or scope.primary),
            "primary_identity": dict(scope.primary_identity or {}),
            "effective_identity": dict(scope.effective_identity or {}),
            "force_portable": scope.force_portable}
