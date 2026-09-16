"""Privacy-safe receipts for the exact requests sent to model providers.

The manifest is deliberately out-of-band.  Nothing in this module mutates a
provider request, appends text to a prompt, or changes inference behavior.
HTTPX invokes the generated request hook after it has encoded the final JSON
body and immediately before network I/O.

Adapters build payloads via ``model_runtime.message_graph`` (canonical graph → wire).
This module mines the *final* wire body only to prove what left the process —
structural counts/roles/tool names, never content. That dual path is intentional:
prompt construction stays graph-owned; receipts stay post-adapter observers.

Only structural metadata is retained.  Prompt text, tool descriptions,
arguments/results, image data, header values, URLs, credentials, and unkeyed
payload hashes are excluded. Process-local keyed equality IDs support cache
diagnosis without retaining those values.
"""

from __future__ import annotations

import copy
import inspect
import json
import time
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Callable
from llm_usage import current_usage_category

from session_catalog.profiles import IPYTHON_SCHEMA_REVISION


SCHEMA = "variant1.model-request-manifest.v1"
PRIVACY_POLICY = "metadata_only_with_process_equality.v2"
ESTIMATOR = "utf8_bytes_div_4.v1"
MAX_ORDERED_MESSAGES = 128
MAX_TOOL_PATHS = 128
MAX_IMAGES = 16


def _prompt_cache_receipt(value: Any) -> dict[str, Any]:
    """Allowlist an opaque cache-identity receipt without retaining owners."""

    raw = value if isinstance(value, dict) else {}
    key_id = str(raw.get("key_id") or "")
    if key_id and not key_id.startswith("variant1-pc-v1-"):
        key_id = ""
    return {
        "identity_available": bool(raw.get("identity_available") and key_id),
        "key_id": key_id[:80] or None,
        "scope": _safe_name(raw.get("scope")) or None,
        "source": _safe_name(raw.get("source")) or None,
        "application": _safe_name(raw.get("application")) or "unavailable",
        "native_key_sent": bool(raw.get("native_key_sent")),
        "cache_enabled": (
            bool(raw.get("cache_enabled"))
            if "cache_enabled" in raw else None
        ),
    }


def provider_response_identity(*objects: Any) -> dict[str, str]:
    """Return only allowlisted provider identity metadata from responses.

    Callers pass already-parsed top-level response/chunk objects.  The helper
    intentionally does not recurse through arbitrary response data and never
    retains text, IDs, errors, or usage fields.
    """
    result: dict[str, str] = {}
    for value in objects:
        if not isinstance(value, dict):
            continue
        model_id = str(value.get("model") or "").strip()
        if model_id:
            result["provider_returned_model_id"] = model_id[:300]
        revision = str(
            value.get("model_revision") or value.get("model_version") or ""
        ).strip()
        if revision:
            result["model_revision"] = revision[:300]
        fingerprint = str(value.get("system_fingerprint") or "").strip()
        if fingerprint:
            result["system_fingerprint"] = fingerprint[:300]
    return result


def patch_provider_response_identity(
    router: Any, manifest_ref: Any, metadata: dict[str, str],
) -> None:
    """Best-effort response-identity patch for real and duck-typed routers."""
    patcher = getattr(router, "_patch_model_request_manifest_response", None)
    if not callable(patcher):
        return
    try:
        patcher(manifest_ref, metadata)
    except Exception:
        # Receipt enrichment must never turn a successful inference into a
        # provider failure.
        pass


@dataclass
class _ModelCallScope:
    logical_call_id: str
    requested_route: str
    selected_mode: str
    attempt: int = 0

    def next_attempt(self) -> int:
        self.attempt += 1
        return self.attempt


class ModelRequestEventHooks(dict):
    """HTTPX-compatible hooks plus the exact emitted request receipt reference.

    HTTPX requires a plain mapping of event names to hook lists, so this remains
    a ``dict`` subclass.  ``request_ref`` is a small mutable metadata-only
    handle populated by the pre-I/O hook once the manifest ID exists.  Adapters
    retain the handle and use it to correlate provider usage with that exact
    physical request (including retries).
    """

    def __init__(self, request_hook: Callable, request_ref: dict[str, str]):
        super().__init__({"request": [request_hook]})
        self.request_ref = request_ref


_MODEL_CALL_SCOPE: ContextVar[_ModelCallScope | None] = ContextVar(
    "variant1_model_call_scope", default=None)


def begin_model_call(*, requested_route: str | None, selected_mode: str) -> Token:
    """Begin one logical model call; adapters record physical attempts below it."""
    scope = _ModelCallScope(
        logical_call_id=f"mcall_{uuid.uuid4().hex}",
        requested_route=str(requested_route or selected_mode or ""),
        selected_mode=str(selected_mode or ""),
    )
    return _MODEL_CALL_SCOPE.set(scope)


def end_model_call(token: Token | None) -> None:
    if token is not None:
        _MODEL_CALL_SCOPE.reset(token)


from .request_manifest_projection import (
    _canonical_json_hash_metrics,
    _estimated_tokens,
    _generation,
    _linkage,
    _rendered_messages,
    _rendered_tools,
    _safe_int,
    _safe_name,
    _source_messages,
    _source_tool_entries,
    _summarize_entries,
)


def _run_identity() -> dict:
    try:
        from run_context import current_run_context
        ctx = current_run_context()
    except Exception:
        ctx = None
    if ctx is None:
        return {"run_id": "", "source": "", "session_id": ""}
    chat = getattr(ctx, "chat_session", None)
    active = getattr(chat, "active", None) if chat is not None else None
    metadata = (
        getattr(ctx, "metadata", None)
        if isinstance(getattr(ctx, "metadata", None), dict)
        else {}
    )
    session_id = str(
        getattr(active, "turn_session_id", None)
        or getattr(chat, "turn_session_id", None)
        or getattr(chat, "viewed_session_id", None)
        or metadata.get("chat_id")
        or getattr(ctx, "session_id", None)
        or ""
    ).strip()
    return {
        "run_id": str(getattr(ctx, "run_id", "") or "")[:160],
        "source": str(getattr(ctx, "source", "") or "")[:80],
        "session_id": session_id[:160],
    }


def _surface_identity() -> dict:
    try:
        from run_context import current_run_context
        ctx = current_run_context()
    except Exception:
        ctx = None
    config = getattr(ctx, "run_config", None) if ctx is not None else None
    metadata = dict(getattr(ctx, "metadata", None) or {}) if ctx is not None else {}
    from agent_engine.session_capabilities import (
        effective_action_surface,
        session_capabilities,
    )

    action_surface = str(
        getattr(config, "action_surface", "") or "trusted-local.v1"
    )
    capabilities = session_capabilities(
        metadata.get("session_capabilities"),
        action_surface=action_surface,
    )
    return {
        "action_surface": action_surface[:120],
        "effective_action_surface": effective_action_surface(
            action_surface, capabilities
        )[:120],
        "mutation_write_enabled": bool(
            capabilities["mutation_write_enabled"]
        ),
        "mutation_authority_revision": int(
            capabilities["mutation_authority_revision"]
        ),
        "provider_tool_schema_revision": str(
            getattr(config, "provider_tool_schema_revision", "")
            or IPYTHON_SCHEMA_REVISION
        )[:120],
        # The graph revision is the executable harness contract.  Keep it next
        # to the provider schema so a post-adapter receipt can prove the exact
        # pinned surface that produced the request.
        "graph_revision": str(
            getattr(config, "graph_revision", "") or "revision_unavailable"
        )[:120],
        "run_config_revision": str(
            getattr(config, "name", "") or "revision_unavailable"
        )[:120],
    }


def _record_manifest_reference(manifest_id: str) -> None:
    try:
        from run_context import current_run_context
        ctx = current_run_context()
    except Exception:
        ctx = None
    if ctx is None or not isinstance(getattr(ctx, "metadata", None), dict):
        return
    refs = ctx.metadata.setdefault("model_request_manifest_ids", [])
    if not isinstance(refs, list):
        refs = []
        ctx.metadata["model_request_manifest_ids"] = refs
    refs.append(str(manifest_id))
    del refs[:-64]


def _empty_context_lineage() -> dict:
    return {
        "schema": "variant1.context-lineage.v1",
        "purpose": "unknown",
        "selection": {"considered": 0, "kept": 0, "dropped": 0},
        "selections": [],
        "items": [],
        "transforms": [],
        "aggregates": {
            "selection_total": 0,
            "item_total": 0,
            "transform_total": 0,
            "items_by_kind": {},
            "transforms_by_kind": {},
        },
        "selections_truncated_count": 0,
        "items_truncated_count": 0,
        "transforms_truncated_count": 0,
    }


def _lineage_transform_kinds(receipt: dict) -> set[str]:
    transforms = receipt.get("transforms") if isinstance(receipt, dict) else None
    kinds: set[str] = set()
    for row in transforms or []:
        if isinstance(row, dict):
            kind = str(row.get("kind") or "").strip()
            if kind:
                kinds.add(kind)
    return kinds


def _context_lineage_for_manifest(
    source_messages: list,
) -> tuple[dict, dict]:
    """Return a sanitized lineage sidecar and explicit availability flags.

    Prefers the message-attached receipt (exact model-bound selection for this
    request). Falls back to the active run's live receipt so headless/internal
    paths still surface projection/supersession transforms without inventing
    content.
    """
    try:
        from observability.context_lineage import (
            current_run_receipt,
            new_receipt,
            receipt_from_messages,
            sanitize_receipt,
        )

        receipt = receipt_from_messages(source_messages)
        available = isinstance(receipt, dict)
        if not available:
            live = current_run_receipt()
            if isinstance(live, dict):
                receipt = sanitize_receipt(live)
                available = isinstance(receipt, dict)
        if not isinstance(receipt, dict):
            receipt = new_receipt("unknown")
            available = False
        kinds = _lineage_transform_kinds(receipt)
        items = receipt.get("items") if isinstance(receipt.get("items"), list) else []
        selections = (
            receipt.get("selections")
            if isinstance(receipt.get("selections"), list) else []
        )
        provenance = {
            "context_receipt_available": available,
            "selection_available": bool(selections) or bool(
                (receipt.get("selection") or {}).get("considered")),
            "observation_projection_available": any(
                kind in kinds
                for kind in (
                    "tool_output_clipped",
                    "research_excerpted",
                    "provider_projection",
                )
            ) or any(
                isinstance(row, dict) and row.get("kind") == "tool_observation"
                for row in items
            ),
            "supersession_available": any(
                kind in kinds
                for kind in ("image_superseded",)
            ),
            "compression_receipt_available": (
                receipt.get("purpose") == "context_compression"
                or "context_compression" in kinds
                or "summary_input_projected" in kinds
            ),
            # Filled true only when provider usage is later patched onto the
            # exact manifest_id; initial emission is explicitly pending.
            "usage_available": False,
        }
        return receipt, provenance
    except Exception:
        # Keep manifest creation fail-open even during partial upgrades while
        # still exposing a stable structural shape to inspectors.
        return _empty_context_lineage(), {
            "context_receipt_available": False,
            "selection_available": False,
            "observation_projection_available": False,
            "supersession_available": False,
            "compression_receipt_available": False,
            "usage_available": False,
        }


def build_model_request_manifest(
    *,
    provider: str,
    api_style: str,
    transport: str,
    adapter: str,
    adapter_version: str,
    model: str,
    payload: dict,
    source_messages: list,
    source_tools: list | None = None,
    requested_images: Any = None,
    endpoint_path: str = "",
    retry_kind: str = "",
    context_limit_tokens: int = 0,
    wire_body_bytes: int = 0,
    prompt_cache: dict[str, Any] | None = None,
    header_names: tuple[str, ...] | None = None,
    payload_basis: str = 'prepared_payload',
    output_budget: dict[str, Any] | None = None,
) -> dict:
    """Return an allowlisted structural receipt for one finalized request."""
    payload = payload if isinstance(payload, dict) else {}
    scope = _MODEL_CALL_SCOPE.get()
    if scope is None:
        scope = _ModelCallScope(
            logical_call_id=f"mcall_{uuid.uuid4().hex}",
            requested_route="direct",
            selected_mode="",
        )
    attempt = scope.next_attempt()
    source_entries, source_calls, source_results = _source_messages(source_messages)
    rendered_entries, rendered_calls, rendered_results, rendered_images = \
        _rendered_messages(payload, transport)
    source_summary = _summarize_entries(source_entries)
    rendered_summary = _summarize_entries(rendered_entries)

    requested_tools = _source_tool_entries(source_tools)
    rendered_tools, canonical_tool_items = _rendered_tools(payload, transport)
    rendered_schema_metrics = []
    for row in rendered_tools:
        if not isinstance(row, dict):
            continue
        rendered_schema_metrics.append({
            "name": str(row.get("name") or ""),
            "rendered_schema_bytes": _safe_int(
                row.pop("rendered_schema_bytes", 0)),
            "estimated_schema_tokens": _safe_int(
                row.pop("estimated_schema_tokens", 0)),
        })
    requested_categories = {
        str(row.get("name") or ""): str(row.get("category") or "")
        for row in requested_tools
        if isinstance(row, dict) and row.get("name")
    }
    for row in rendered_tools:
        if not isinstance(row, dict):
            continue
        category = requested_categories.get(str(row.get("name") or ""), "")
        if category:
            row["category"] = category
    schema_sha, schema_byte_count = _canonical_json_hash_metrics(
        canonical_tool_items) if canonical_tool_items else ("", 0)

    transient_images = (
        [item for item in requested_images if item]
        if isinstance(requested_images, (list, tuple))
        else ([requested_images] if requested_images else [])
    )
    transient_image_count = len(transient_images)
    captured_image_count = sum(
        1
        for item in transient_images
        if isinstance(item, dict)
        and (
            str((item.get("capture") or {}).get("status") or "") == "captured"
            or bool(item.get("artifact_ref"))
        )
    )
    requested_image_count = int(source_summary["images"]) + transient_image_count
    source_protocol = _linkage(source_calls, source_results)
    rendered_protocol = _linkage(rendered_calls, rendered_results)
    call_loss = len(rendered_calls) < len(source_calls)
    result_loss = len(rendered_results) < len(source_results)
    image_loss = len(rendered_images) < requested_image_count
    tool_schema_loss = len(rendered_tools) < len(requested_tools)
    source_linkage_valid = not (
        source_protocol["duplicate_call_ids"]
        or source_protocol["orphan_results"]
        or source_protocol["unanswered_calls"]
        or source_protocol["calls_with_ids"] != source_protocol["calls"]
        or source_protocol["results_with_ids"] != source_protocol["results"]
    )
    rendered_linkage_valid = not (
        rendered_protocol["duplicate_call_ids"]
        or rendered_protocol["orphan_results"]
        or rendered_protocol["unanswered_calls"]
        or rendered_protocol["calls_with_ids"] != rendered_protocol["calls"]
        or rendered_protocol["results_with_ids"] != rendered_protocol["results"]
    )

    generation = _generation(payload, transport, len(rendered_tools))
    remote_limit = max(0, int(generation['max_output_tokens'] or 0))
    generation_budget = {'application': 'remote' if remote_limit else 'unspecified',
                         'requested_tokens': remote_limit or None, 'resource_limit_bytes': None}
    if output_budget is not None:
        generation_budget = {key: output_budget.get(key) for key in generation_budget}
        if generation_budget['application'] == 'advisory_unapplied':
            generation['max_output_tokens'] = None
    context_lineage, lineage_provenance = _context_lineage_for_manifest(
        source_messages)
    prompt_bytes = (
        rendered_summary["text_utf8_bytes"]
        + rendered_summary["tool_argument_utf8_bytes"]
        + rendered_summary["tool_result_utf8_bytes"]
    )
    message_tokens = _estimated_tokens(prompt_bytes) + 4 * len(rendered_entries)
    schema_tokens = _estimated_tokens(
        schema_byte_count) if canonical_tool_items else 0
    estimated_input = message_tokens + schema_tokens
    context_limit = max(0, int(context_limit_tokens or 0))
    output_reserve = max(0, int(generation_budget['requested_tokens'] or 0))
    remaining = context_limit - estimated_input - output_reserve if context_limit else None

    transforms: list[dict] = []
    source_system_count = source_summary["role_counts"].get("system", 0)
    rendered_system_count = rendered_summary["role_counts"].get("system", 0)
    if source_system_count != rendered_system_count:
        transforms.append({
            "kind": "system_message_count_changed",
            "source": source_system_count,
            "rendered": rendered_system_count,
        })
    source_system_chars = sum(
        e["text_chars"] for e in source_entries if e["role"] == "system")
    rendered_system_chars = sum(
        e["text_chars"] for e in rendered_entries if e["role"] == "system")
    if source_system_chars != rendered_system_chars:
        transforms.append({
            "kind": "system_text_size_changed",
            "char_delta": rendered_system_chars - source_system_chars,
        })
    source_total_content = (
        source_summary["text_chars"] + source_summary["tool_argument_chars"]
        + source_summary["tool_result_chars"])
    rendered_total_content = (
        rendered_summary["text_chars"] + rendered_summary["tool_argument_chars"]
        + rendered_summary["tool_result_chars"])
    if source_total_content != rendered_total_content:
        transforms.append({
            "kind": "content_size_changed",
            "char_delta": rendered_total_content - source_total_content,
        })
    if requested_image_count or rendered_images:
        transforms.append({
            "kind": "image_projection",
            "requested": requested_image_count,
            "rendered": len(rendered_images),
        })
    if call_loss or result_loss:
        transforms.append({
            "kind": "tool_protocol_loss",
            "calls_lost": max(0, len(source_calls) - len(rendered_calls)),
            "results_lost": max(0, len(source_results) - len(rendered_results)),
        })
    if tool_schema_loss:
        transforms.append({
            "kind": "tool_schema_loss",
            "tools_lost": max(0, len(requested_tools) - len(rendered_tools)),
        })

    if len(rendered_entries) > MAX_ORDERED_MESSAGES:
        head_count = min(16, MAX_ORDERED_MESSAGES)
        tail_count = MAX_ORDERED_MESSAGES - head_count
        ordered_rendered = (
            rendered_entries[:head_count] + rendered_entries[-tail_count:])
        first_omitted = rendered_entries[head_count]["position"]
        last_omitted = rendered_entries[-tail_count - 1]["position"]
        ordered_window = {
            "policy": "head_tail",
            "head_count": head_count,
            "tail_count": tail_count,
            "omitted_count": len(rendered_entries) - MAX_ORDERED_MESSAGES,
            "first_omitted_position": first_omitted,
            "last_omitted_position": last_omitted,
        }
    else:
        ordered_rendered = rendered_entries
        ordered_window = {
            "policy": "complete",
            "head_count": len(rendered_entries),
            "tail_count": 0,
            "omitted_count": 0,
            "first_omitted_position": None,
            "last_omitted_position": None,
        }

    endpoint = str(endpoint_path or "")
    if "?" in endpoint:
        endpoint = endpoint.split("?", 1)[0]
    endpoint = endpoint[:200] if endpoint.startswith("/") else ""
    manifest_id = f"mreq_{uuid.uuid4().hex}"
    run_identity = _run_identity()
    try:
        from .cache_diagnostics import CACHE_EQUALITY

        cache_diagnostics = CACHE_EQUALITY.record(
            payload=payload, transport=transport,
            owner=str((prompt_cache or {}).get('key_id') or run_identity.get('session_id')
                      or run_identity.get('run_id') or ''),
            lane=(provider, model, transport, adapter, run_identity.get('source'), current_usage_category()),
            manifest_id=manifest_id, header_names=header_names,
        )
    except Exception:
        cache_diagnostics = {'available': False, 'reason': 'diagnostics_unavailable'}
    return {
        "type": "model:request_manifest",
        "schema": SCHEMA,
        "manifest_id": manifest_id,
        "logical_call_id": scope.logical_call_id,
        "attempt": attempt,
        "call_category": _safe_name(current_usage_category()),
        "captured_at": round(time.time(), 3),
        "run": run_identity,
        "surface": _surface_identity(),
        "route": {
            "requested": scope.requested_route,
            "selected_mode": scope.selected_mode,
            "physical_mode": "local" if str(provider).lower() == "local" else "cloud",
            "provider": _safe_name(provider),
            "api_style": _safe_name(api_style),
            "transport": _safe_name(transport),
            "adapter": _safe_name(adapter),
            "adapter_version": _safe_name(adapter_version),
            "model": str(model or "")[:300],
            # Provider response metadata is patched onto this exact receipt
            # after the stream finishes.  Never infer a revision from the
            # requested model alias.
            "provider_returned_model_id": "revision_unavailable",
            "model_revision": "revision_unavailable",
            "system_fingerprint": "revision_unavailable",
            "retry_kind": _safe_name(retry_kind),
            "endpoint_path": endpoint,
        },
        "request": {
            "wire_body_bytes": max(0, int(wire_body_bytes or 0)),
            "payload_basis": payload_basis if payload_basis in {
                'prepared_payload', 'httpx_encoded_json',
            } else 'unavailable',
            "payload_keys": sorted(_safe_name(k) for k in payload.keys())[:100],
        },
        "prompt_cache": _prompt_cache_receipt(prompt_cache),
        "cache_diagnostics": cache_diagnostics,
        "messages": {
            "source": source_summary,
            "rendered": rendered_summary,
            "ordered_rendered": ordered_rendered,
            "ordered_rendered_window": ordered_window,
            "ordered_rendered_truncated_count": ordered_window["omitted_count"],
        },
        "tools": {
            "requested_count": len(requested_tools),
            "rendered_count": len(rendered_tools),
            "requested": requested_tools[:MAX_TOOL_PATHS],
            "requested_truncated_count": max(
                0, len(requested_tools) - MAX_TOOL_PATHS),
            "rendered": rendered_tools[:MAX_TOOL_PATHS],
            "rendered_schema_metrics": rendered_schema_metrics[:MAX_TOOL_PATHS],
            "rendered_schema_metrics_truncated_count": max(
                0, len(rendered_schema_metrics) - MAX_TOOL_PATHS),
            "rendered_truncated_count": max(
                0, len(rendered_tools) - MAX_TOOL_PATHS),
            "rendered_schema_sha256": schema_sha,
            "rendered_schema_bytes": schema_byte_count,
            "estimated_schema_tokens": schema_tokens,
            "schema_loss": tool_schema_loss,
        },
        "images": {
            "captured_count": captured_image_count,
            "source_count": int(source_summary["images"]),
            "selected_count": requested_image_count,
            "requested_count": requested_image_count,
            "encoded_count": len(rendered_images),
            "rendered_count": len(rendered_images),
            "loss": image_loss,
            "rendered": rendered_images[:MAX_IMAGES],
            "rendered_truncated_count": max(
                0, len(rendered_images) - MAX_IMAGES),
        },
        "tool_protocol": {
            "source": source_protocol,
            "rendered": rendered_protocol,
            "call_loss": call_loss,
            "result_loss": result_loss,
            "source_valid": source_linkage_valid,
            "rendered_valid": rendered_linkage_valid,
            "valid": not (call_loss or result_loss) and rendered_linkage_valid,
        },
        "generation": generation,
        "generation_budget": generation_budget,
        "budget": {
            "estimator": ESTIMATOR,
            "estimated_message_tokens": message_tokens,
            "estimated_schema_tokens": schema_tokens,
            "estimated_image_tokens": None if rendered_images else 0,
            "estimated_input_tokens_lower_bound": estimated_input,
            "context_limit_tokens": context_limit or None,
            "output_reserve_tokens": output_reserve,
            "remaining_margin_tokens": remaining,
            "over_budget": remaining < 0 if remaining is not None else None,
        },
        "transforms": transforms,
        "context_lineage": context_lineage,
        "provenance": dict(lineage_provenance),
        # Usage starts absent; the router patches allowlisted provider metrics
        # onto this same manifest_id after the response (or estimated local
        # counts). Inspectors treat missing usage as pending, not failure.
        "usage": None,
        "privacy": {
            "policy": PRIVACY_POLICY,
            "prompt_text_stored": False,
            "tool_values_stored": False,
            "image_data_stored": False,
            "headers_stored": False,
            "url_stored": False,
            "payload_hash_stored": False,
            "process_scoped_equality_ids_stored": bool(cache_diagnostics.get('available')),
            "exact_payload_ref": None,
        },
    }


async def _emit_manifest(router: Any, manifest: dict) -> None:
    _record_manifest_reference(str(manifest.get("manifest_id") or ""))
    try:
        from observability.trace_events import record_model_manifest

        record_model_manifest(manifest)
    except Exception:
        # A trace sink must never prevent the provider request.
        pass
    recorder = getattr(router, "_record_model_request_manifest", None)
    if callable(recorder):
        try:
            result = recorder(copy.deepcopy(manifest))
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass
def model_request_event_hooks(
    router: Any,
    *,
    provider: str,
    api_style: str,
    transport: str,
    adapter: str,
    adapter_version: str,
    model: str,
    payload: dict,
    source_messages: list,
    source_tools: list | None = None,
    requested_images: Any = None,
    endpoint_path: str = "",
    retry_kind: str = "",
    context_limit_tokens: int = 0,
    prompt_cache: dict[str, Any] | None = None,
    output_budget: dict[str, Any] | None = None,
) -> ModelRequestEventHooks:
    """Return a fail-open HTTPX hook for one finalized provider payload."""

    request_ref: dict[str, str] = {"manifest_id": ""}

    async def on_request(request: Any) -> None:
        try:
            observed_payload = payload
            payload_basis = 'prepared_payload'
            wire_bytes = 0
            try:
                wire_bytes = len(request.content)
                decoded = json.loads(request.content)
                if isinstance(decoded, dict):
                    observed_payload = decoded
                    payload_basis = 'httpx_encoded_json'
            except Exception:
                pass
            request_headers = getattr(request, 'headers', None)
            header_names = tuple(request_headers.keys()) if request_headers is not None else None
            manifest = build_model_request_manifest(
                provider=provider,
                api_style=api_style,
                transport=transport,
                adapter=adapter,
                adapter_version=adapter_version,
                model=model,
                payload=observed_payload,
                source_messages=source_messages,
                source_tools=source_tools,
                requested_images=requested_images,
                endpoint_path=endpoint_path,
                retry_kind=retry_kind,
                context_limit_tokens=context_limit_tokens,
                wire_body_bytes=wire_bytes,
                prompt_cache=prompt_cache,
                header_names=header_names,
                payload_basis=payload_basis,
                output_budget=output_budget,
            )
            request_ref["manifest_id"] = str(manifest.get("manifest_id") or "")
            await _emit_manifest(router, manifest)
        except Exception:
            # Observability must never prevent a provider request.
            return

    return ModelRequestEventHooks(on_request, request_ref)
