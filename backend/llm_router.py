"""
VARIANT-1 LLM router — stable route facade and cloud control plane.

Composition (issue #9):
  - ``llm_router_config`` — load/save + mode/sampling helpers
  - ``llm_local_stream`` — llama-server streaming (LocalEngineClient surface)
  - ``llm_cloud_stream`` — provider HTTP streams + cloud-only failover
  - ``llm_usage`` — token/usage normalization + observe_usage
  - ``llm_manifest_bus`` — post-adapter request-receipt store/publish
  - ``model_runtime.message_graph`` — canonical graph → provider payload
  - ``model_runtime.request_manifest`` — privacy-safe final-wire receipts
    (observer only; never mutates the request)

``LLMRouter`` owns user-selected mode routing, credentials/OAuth, engine
lifecycle, and the public ``stream()`` entry. It never changes between local
and cloud after a request starts. Provider wire formats and manifest mining
stay out of this file.
"""

from contextlib import aclosing

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import inspect
import os
import time

import httpx

from llm_profiles import complete, complete_turn  # noqa: F401 — public facade
from llm_router_config import (
    apply_local_model as _cfg_apply_local_model,
    apply_mode as _cfg_apply_mode,
    save_config as _cfg_save_config,
)
from llm_local_stream import (
    build_local_template_payload as _build_local_template_payload,
    call_local as _local_call_local,
    render_local_messages as _render_local_messages,
)
from model_runtime.llama_server import LlamaServer, LocalEngineError
from model_runtime.external_runtime import ExternalOpenAIRuntime
from model_runtime.runtime_catalog import (
    configure_runtime as _configure_inference_runtime,
    runtime_config as _inference_runtime_config,
    selected_runtime_id as _selected_inference_runtime_id,
    set_selected_runtime as _set_selected_inference_runtime,
)
from model_providers import CredentialLease, CredentialPoolStore, default_registry
from model_runtime.telemetry import LocalInferenceTelemetry
from observability.cloud_usage import CloudUsageTelemetry
from model_runtime.request_manifest import begin_model_call, end_model_call
from model_runtime.prompt_cache import resolve_prompt_cache_identity
from llm_usage import (
    current_usage_observer,
    normalize_manifest_usage,
    observe_usage,  # noqa: F401 — public facade
)
from llm_usage import current_usage_category
from llm_manifest_bus import ModelRequestManifestBus
from llm_stream_diagnostics import (
    StreamDiagnostics,
    counting_tool_sink,
    log_finish_reason,
)
from session_catalog.support import SupportMatrix, validate_tool_projection
from model_runtime.secret_egress import SecretEgressFirewall

from llm_cloud_stream import (
    call_cloud as _cloud_call_cloud,
)


_BOUND_MODEL_ROUTE: ContextVar[dict | None] = ContextVar(
    "variant1_bound_model_route", default=None)

_NATIVE_OAUTH_PROVIDERS = frozenset({
    "openai-codex", "xai", "minimax-oauth", "minimax-oauth-cn",
    "google-antigravity",
})
_MINIMAX_OAUTH_PROVIDERS = frozenset({"minimax-oauth", "minimax-oauth-cn"})


def _enabled_local_reasoning_budget(cfg: dict) -> int:
    """Configured positive thinking cap, or -1 for explicit unrestricted mode."""

    raw = (cfg.get("local", {}) or {}).get("reasoning_budget", -1)
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        value = -1
    return max(-1, value)


class LLMRouter:
    """Single model interface with a user-selected local or cloud route."""

    def __init__(self, cfg: dict, app_root: str, config_path: str = None,
                 data_dir: str = None):
        self.cfg = cfg
        self.app_root = app_root
        self.data_dir = data_dir or app_root
        self.config_path = config_path
        self.mode = cfg.get("mode", "local")
        self.sampling = cfg.get("sampling", {})
        self.engine = self.build_inference_runtime(
            _selected_inference_runtime_id(cfg), require_configured=False)
        config_dir = os.path.dirname(config_path) if config_path else None
        self.provider_registry = default_registry(app_root, config_dir)
        from model_providers.custom_endpoints import (
            endpoint_records,
            register_endpoints,
        )
        register_endpoints(self.provider_registry, endpoint_records(self.cfg))
        self.credential_pools = CredentialPoolStore(
            self.cfg, self._kn, self.save_config)
        # OAuth refresh tokens may rotate. Serialize each provider's read →
        # refresh → persist chain so concurrent chats cannot consume the same
        # refresh generation and overwrite one another.
        self._oauth_refresh_locks: dict[str, asyncio.Lock] = {}
        self._oauth_revisions: dict[str, int] = {}
        # Local reasoning follows the loaded model/runtime. There is no global
        # user toggle; an optional positive cap or -1 remains a deployment
        # configuration detail.
        self.engine.reasoning_budget = _enabled_local_reasoning_budget(cfg)
        usage_path = os.path.join(os.path.dirname(config_path), "cloud_usage.json") if config_path else None
        self._cloud_usage = CloudUsageTelemetry(usage_path)
        self._inference = LocalInferenceTelemetry()
        self._inference_sink = None
        self._inference_last_emit = 0.0
        # Post-adapter request receipts (observer bus — not on the hot path).
        self._manifest_bus = ModelRequestManifestBus()
        self._support_matrix = SupportMatrix.from_config(self.cfg)
        self._model_secret_resolvers = []
        self._secret_egress_firewall = SecretEgressFirewall(
            known_secret_resolver=self._known_model_secrets,
        )

    def _record_usage(self, provider: str, prompt_tokens=0, completion_tokens=0,
                      total_tokens=None, *, model="", raw_usage=None,
                      inference_time_s=None, manifest_ref=None, latency_ms=None,
                      ttft_ms=None, prefill_tps=None, generation_tps=None):
        provider = self._kn(provider or self.cloud_provider or "unknown")
        pricing = (self.cfg.get("cloud", {}) or {}).get("pricing", {})
        event = self._cloud_usage.record(
            provider, model, prompt_tokens, completion_tokens, total_tokens,
            raw_usage=raw_usage, custom_pricing=pricing,
            inference_time_s=inference_time_s,
            runtime_id=(self.inference_runtime_id if provider == "local" else ""),
            latency_ms=latency_ms, ttft_ms=ttft_ms,
            prefill_tps=prefill_tps, generation_tps=generation_tps)
        observer = current_usage_observer()
        if observer is not None:
            try:
                observed = dict(event or {})
                observed["call_category"] = current_usage_category()
                observer(observed)
            except Exception:
                pass
        try:
            normalized = normalize_manifest_usage(
                provider,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                raw_usage=raw_usage,
            )
            normalized["call_category"] = current_usage_category()
            if isinstance(event, dict):
                # Pricing/timing are computed by the canonical usage ledger,
                # not by provider-shape normalization. Carry those correlated
                # scalar results into the manifest trace without prompt data.
                normalized["cost_usd"] = event.get("cost_usd")
            self._patch_model_request_manifest_usage(manifest_ref, normalized)
        except Exception:
            # Usage lineage is observability only.  Never fail inference,
            # telemetry accounting, or a caller because correlation failed.
            pass

    def _record_usage_outcome(self, provider: str, model: str, *, status="error",
                              latency_ms=None):
        provider = self._kn(provider or self.cloud_provider or "unknown")
        return self._cloud_usage.record_outcome(
            provider,
            model,
            status=status,
            latency_ms=latency_ms,
            runtime_id=(self.inference_runtime_id if provider == "local" else ""),
        )

    def _observe_usage_performance(self, provider: str, model: str, **metrics):
        provider = self._kn(provider or self.cloud_provider or "unknown")
        return self._cloud_usage.observe_performance(provider, model, **metrics)

    def _observe_cloud_response(self, provider, model, response, error=""):
        self._cloud_usage.observe_response(
            self._kn(provider), model, getattr(response, "status_code", 0),
            getattr(response, "headers", {}), error=error)

    def usage_snapshot(self) -> dict:
        cloud = self.cfg.get("cloud", {}) or {}
        return self._cloud_usage.snapshot(
            budget_usd=cloud.get("monthly_budget_usd"),
            active_provider=self._kn(self.cloud_provider))

    def model_usage_snapshot(self, days=30) -> dict:
        return self._cloud_usage.model_usage_snapshot(days=days)

    def set_inference_telemetry_sink(self, sink) -> None:
        """Receive local inference snapshots; the sink may be sync or async."""
        self._inference_sink = sink

    def inference_snapshot(self) -> dict:
        snapshot = self._inference.snapshot(
            model=self.model_name, engine_ready=self.engine_ready)
        return self._with_inference_runtime(snapshot)

    def _with_inference_runtime(self, snapshot: dict) -> dict:
        engine = self.engine
        return {
            **(snapshot or {}),
            "runtime_id": str(getattr(engine, "runtime_id", "llamacpp")),
            "runtime_name": str(getattr(engine, "display_name", "llama.cpp")),
            "runtime_managed": bool(getattr(engine, "managed", True)),
            "runtime_endpoint": str(getattr(engine, "base_url", "") or ""),
        }

    async def _publish_inference(self, snapshot: dict = None, *, force=False) -> None:
        if not snapshot or self._inference_sink is None:
            return
        now = time.monotonic()
        if not force and now - self._inference_last_emit < 0.25:
            return
        self._inference_last_emit = now
        snapshot = self._with_inference_runtime({
            **snapshot,
            "model": self.model_name or snapshot.get("model", ""),
            "engine_ready": self.engine_ready,
        })
        try:
            result = self._inference_sink(snapshot)
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass

    def set_model_request_manifest_sink(self, sink) -> None:
        """Receive privacy-safe, post-adapter request receipts."""
        self._manifest_bus.set_sink(sink)

    def refresh_support_matrix(self) -> dict:
        """Reload operator qualification after an explicit config change."""
        self._support_matrix = SupportMatrix.from_config(self.cfg)
        return self._support_matrix.public_snapshot()

    def replace_support_matrix(self, rules: list[dict]) -> dict:
        """Atomically persist an explicit operator qualification/revocation set."""
        from session_catalog.profiles import ACTION_SURFACE, canonical_action_surface

        if not isinstance(rules, list) or len(rules) > 200:
            raise ValueError("support matrix must be a list of at most 200 rules")
        clean: list[dict] = []
        allowed_status = {"qualified", "canary", "developer", "unqualified", "revoked"}
        for index, raw in enumerate(rules):
            if not isinstance(raw, dict):
                raise ValueError(f"support rule {index} must be an object")
            profile = canonical_action_surface(raw.get("profile") or "")
            status = str(raw.get("status") or "unqualified").strip().lower()
            if profile != ACTION_SURFACE or status not in allowed_status:
                raise ValueError(f"support rule {index} has an invalid profile/status")
            clean.append({
                "profile": profile,
                "provider": str(raw.get("provider") or "*").strip(),
                "model": str(raw.get("model") or "*").strip(),
                "adapter": str(raw.get("adapter") or "*").strip(),
                "status": status,
                "evidence": str(raw.get("evidence") or "").strip()[:2000],
            })
        old = list((
            ((self.cfg.get("action_surface") or self.cfg.get("astb") or {}).get("support_matrix"))
            or ()
        ))
        self.cfg.setdefault("action_surface", {})["support_matrix"] = clean
        try:
            self.save_config(strict=True)
            return self.refresh_support_matrix()
        except Exception:
            self.cfg.setdefault("action_surface", {})["support_matrix"] = old
            self.refresh_support_matrix()
            raise

    def register_model_secret_resolver(self, resolver) -> None:
        """Register a host-owned source of plaintext values for exact redaction."""
        if callable(resolver) and resolver not in self._model_secret_resolvers:
            self._model_secret_resolvers.append(resolver)

    def set_secret_egress_firewall(self, firewall) -> None:
        if firewall is None or not callable(getattr(firewall, "project", None)):
            raise TypeError("secret-egress firewall must expose project()")
        self._secret_egress_firewall = firewall

    def _known_model_secrets(self) -> list[tuple[str, str]]:
        """Resolve managed credentials only for the in-memory sanitation pass."""
        from security import secretstore

        rows: list[tuple[str, str]] = []
        for profile in self.provider_registry.list():
            provider = self._kn(profile.name)
            for record in self.credential_pools.records(provider):
                try:
                    value = secretstore.decrypt(str(record.get("secret") or ""))
                except Exception:
                    value = ""
                if value:
                    rows.append((f"provider.{provider}", value))
            for env_name in profile.env_vars:
                value = os.environ.get(env_name) or ""
                if value:
                    rows.append((f"env.{env_name}", value))
            oauth = (
                ((self.cfg.get("cloud", {}) or {}).get("oauth", {}) or {})
                .get(provider) or {}
            )
            for field in ("access_token", "refresh_token"):
                try:
                    value = secretstore.decrypt(str(oauth.get(field) or ""))
                except Exception:
                    value = ""
                if value:
                    rows.append((f"oauth.{provider}.{field}", value))
            if (
                provider == "openai-codex"
                and oauth.get("auth_flow") == "codex_cli"
                and oauth.get("managed_external") is True
            ):
                # The externally managed token is never persisted by VARIANT-1,
                # but it is still a known secret for the final prompt egress
                # sanitation pass.
                value = self.get_oauth_access_token(provider)
                if value:
                    rows.append(("oauth.openai-codex.external_access", value))
        for provider in self.credential_pools.pool_names():
            if any(profile.name == provider or self._kn(profile.name) == provider
                   for profile in self.provider_registry.list()):
                continue
            for record in self.credential_pools.records(provider):
                try:
                    value = secretstore.decrypt(str(record.get("secret") or ""))
                except Exception:
                    value = ""
                if value:
                    rows.append((f"pool.{provider}", value))
        for resolver in tuple(self._model_secret_resolvers):
            extra = resolver() or ()
            for item in extra:
                if isinstance(item, (tuple, list)) and len(item) == 2 and item[1]:
                    rows.append((str(item[0] or "host"), str(item[1])))
        return rows

    @staticmethod
    def _model_egress_scope() -> str:
        try:
            from run_context import current_run_context
            ctx = current_run_context()
        except Exception:
            ctx = None
        if ctx is None:
            return ""
        metadata = dict(getattr(ctx, "metadata", None) or {})
        return str(getattr(getattr(ctx, 'work_scope', None), 'chat_id', '') or metadata.get("chat_id") or getattr(ctx, "run_id", "") or "")

    def prepare_cloud_payload(self, payload: dict, *, provider: str, model: str) -> dict:
        projection = self._secret_egress_firewall.project(
            payload,
            provider=provider,
            model=model,
            scope=self._model_egress_scope(),
        )
        try:
            from run_context import current_run_context
            ctx = current_run_context()
        except Exception:
            ctx = None
        if ctx is not None and isinstance(getattr(ctx, "metadata", None), dict):
            refs = ctx.metadata.setdefault("model_egress_projection_refs", [])
            if isinstance(refs, list):
                refs.append({
                    "local": projection.local_ref,
                    "cloud": projection.cloud_ref,
                    "known_replacements": projection.known_replacements,
                    "redacted_history_fields": projection.redacted_history_fields,
                })
        return projection.payload

    def validate_model_request(
        self,
        *,
        provider: str,
        model: str,
        adapter: str,
        tools: list | None,
        internal_projection: bool = False,
    ) -> dict:
        from session_catalog.support import current_action_surface

        profile, schema_revision = current_action_surface()
        if internal_projection:
            if tools:
                raise TypeError("internal model projections cannot expose action tools")
        else:
            validate_tool_projection(profile, schema_revision, tools)
        rule = self._support_matrix.validate(
            profile=profile,
            provider=provider,
            model=model,
            adapter=adapter,
        )
        return {
            "profile": profile,
            "provider_schema_revision": schema_revision,
            "status": rule.status,
            "evidence": rule.evidence,
        }

    def model_request_manifest_snapshot(self, *, manifest_id: str = "") -> dict:
        """Return the bounded in-memory receipt window; no prompt content is stored.

        When ``manifest_id`` is set, return only that receipt (or an empty
        items list). This is the inspector drill-down path — still metadata
        only, never an exact payload reference.
        """
        return self._manifest_bus.snapshot(manifest_id=manifest_id)

    async def _record_model_request_manifest(self, manifest: dict) -> None:
        """Store and enqueue one receipt without delaying the provider request."""
        await self._manifest_bus.record(manifest)

    def _patch_model_request_manifest_usage(
        self, manifest_ref, normalized_usage: dict,
    ) -> None:
        """Patch and republish the exact bounded request receipt, fail-open."""
        self._manifest_bus.patch_usage(manifest_ref, normalized_usage)

    def _patch_model_request_manifest_response(
        self, manifest_ref, metadata: dict,
    ) -> None:
        """Attach allowlisted provider-returned identity metadata."""
        self._manifest_bus.patch_response_metadata(manifest_ref, metadata)

    # --- config persistence + cloud mode/keys (Step 6) ------------------
    @property
    def mode(self) -> str:
        bound = _BOUND_MODEL_ROUTE.get()
        if isinstance(bound, dict) and bound.get("mode") in {"local", "cloud"}:
            return str(bound["mode"])
        return getattr(self, "_mode", "local")

    @mode.setter
    def mode(self, value: str) -> None:
        self._mode = str(value or "local")

    @contextmanager
    def bind_model_route(self, route: dict | None):
        """Pin route/provider/model for the current async turn.

        ``ContextVar`` isolation means Settings may change process defaults
        without changing an already-running request or its helper calls.
        """
        from model_runtime.context import normalize_model_route

        token = _BOUND_MODEL_ROUTE.set(normalize_model_route(self, route))
        try:
            yield _BOUND_MODEL_ROUTE.get()
        finally:
            _BOUND_MODEL_ROUTE.reset(token)

    def bound_model_route(self) -> dict | None:
        route = _BOUND_MODEL_ROUTE.get()
        return dict(route) if isinstance(route, dict) else None

    def push_model_route(self, route: dict | None):
        """Low-level token API for turn runners with an existing ``finally``."""
        from model_runtime.context import normalize_model_route

        return _BOUND_MODEL_ROUTE.set(normalize_model_route(self, route))

    @staticmethod
    def reset_model_route(token) -> None:
        _BOUND_MODEL_ROUTE.reset(token)

    def context_limit_tokens(self, route: dict | None = None) -> int:
        from model_runtime.context import context_limit_tokens

        return context_limit_tokens(self, route or self.bound_model_route())

    def projection_budget_tokens(self, route: dict | None = None) -> int:
        from model_runtime.context import projection_budget_tokens

        return projection_budget_tokens(self, route or self.bound_model_route())

    def save_config(self, *, strict: bool = False) -> bool:
        return _cfg_save_config(self.config_path, self.cfg, strict=strict)

    @property
    def inference_runtime_id(self) -> str:
        return str(getattr(self.engine, "runtime_id", "llamacpp") or "llamacpp")

    def configure_inference_runtime(self, runtime_id: str, updates: dict) -> dict:
        configured = _configure_inference_runtime(self.cfg, runtime_id, updates)
        self.save_config()
        return configured

    def build_inference_runtime(self, runtime_id: str, *, require_configured=True):
        runtime_id = str(runtime_id or "llamacpp").strip().lower()
        if runtime_id == "llamacpp":
            return LlamaServer(
                self.cfg.get("local", {}), self.app_root, self.data_dir,
            )
        source = _inference_runtime_config(self.cfg, runtime_id)
        if require_configured and not (source.get("endpoint") and source.get("model")):
            raise ValueError("configure an endpoint and served model before selecting this runtime")
        return ExternalOpenAIRuntime(runtime_id, self.cfg)

    def commit_inference_runtime(self, runtime_id: str, engine) -> None:
        _set_selected_inference_runtime(self.cfg, runtime_id)
        self.engine = engine
        self.engine.reasoning_budget = _enabled_local_reasoning_budget(self.cfg)
        self.save_config()

    def set_mode(self, mode: str):
        if not _cfg_apply_mode(self.cfg, mode):
            raise ValueError("mode must be 'local' or 'cloud'")
        self.mode = mode
        self.save_config()

    @property
    def local_prewarm(self) -> bool:
        """Keep the local engine loaded even when the active route is cloud."""
        return bool((self.cfg.get("local", {}) or {}).get("prewarm", False))

    def set_local_prewarm(self, on: bool) -> None:
        self.cfg.setdefault("local", {})["prewarm"] = bool(on)
        self.save_config()

    def wants_local_engine(self) -> bool:
        """Whether routing policy currently needs a ready local engine."""
        return self.mode == "local" or self.local_prewarm

    @property
    def reasoning(self) -> bool:
        return True

    @property
    def cloud_provider(self) -> str:
        bound = _BOUND_MODEL_ROUTE.get()
        if isinstance(bound, dict) and bound.get("provider") not in {None, "", "local"}:
            return self._kn(str(bound["provider"]))
        return self._kn((self.cfg.get("cloud", {}) or {}).get("provider", "anthropic"))

    def set_cloud_provider(self, provider: str):
        provider = self._kn(provider)
        if not self.provider_registry.get(provider):
            raise ValueError(f"unknown model provider: {provider}")
        self.cfg.setdefault("cloud", {})["provider"] = provider
        _cfg_apply_mode(self.cfg, "cloud")
        self.mode = "cloud"
        self.save_config()

    def set_local_model(
        self,
        model_path: str,
        mmproj_path: str = "",
        *,
        strict: bool = False,
    ) -> bool:
        _cfg_apply_local_model(self.cfg, model_path, mmproj_path)
        return self.save_config(strict=strict)

    def _kn(self, provider: str) -> str:
        return self.provider_registry.canonical_name(provider)

    def provider_profile(self, provider: str = None):
        """Resolved profile, including safe per-install declarative overrides."""
        name = self._kn(provider or self.cloud_provider)
        profile = self.provider_registry.get(name)
        if profile is None:
            return None
        options = ((self.cfg.get("cloud", {}) or {}).get("provider_options", {}) or {}).get(name) or {}
        return profile.with_overrides(options.get("profile") if isinstance(options, dict) else None)

    def provider_options(self, provider: str = None) -> dict:
        name = self._kn(provider or self.cloud_provider)
        value = ((self.cfg.get("cloud", {}) or {}).get("provider_options", {}) or {}).get(name) or {}
        return dict(value) if isinstance(value, dict) else {}

    def set_provider_options(self, provider: str, updates: dict) -> None:
        name = self._kn(provider)
        if not self.provider_registry.get(name):
            raise ValueError(f"unknown model provider: {name}")
        if not isinstance(updates, dict):
            raise ValueError("provider options must be an object")
        if name in {"ollama", "hermes", "openai-codex"} and (
            "base_url" in updates
            or (
                isinstance(updates.get("profile"), dict)
                and "base_url" in updates["profile"]
            )
        ):
            display = {
                "ollama": "Ollama Desktop Cloud",
                "hermes": "Hermes Agent",
                "openai-codex": "OpenAI Codex OAuth",
            }[name]
            endpoint_kind = (
                "fixed loopback credential-bound endpoint"
                if name in {"ollama", "hermes"}
                else "fixed credential-bound endpoint"
            )
            raise ValueError(
                f"{display} uses a {endpoint_kind}; its base URL "
                "cannot be overridden"
            )
        allowed = {"base_url", "profile"}
        row = self.cfg.setdefault("cloud", {}).setdefault("provider_options", {}).setdefault(name, {})
        for key in allowed:
            if key in updates:
                row[key] = updates[key]
        self.save_config()

    def provider_base_url(self, provider: str, lease: CredentialLease = None) -> str:
        name = self._kn(provider)
        if name == "ollama":
            from model_runtime.ollama_cloud import OLLAMA_CLOUD_OPENAI_BASE

            return OLLAMA_CLOUD_OPENAI_BASE
        if name == "hermes":
            from model_runtime.hermes_proxy import HERMES_PROXY_OPENAI_BASE

            return HERMES_PROXY_OPENAI_BASE
        if name == "openai-codex":
            from openai_codex_oauth import (
                CODEX_RESPONSES_BASE,
                validate_codex_base_url,
            )

            # This route carries a ChatGPT subscription bearer. Ignore every
            # persisted profile/base override and keep it on the exact
            # first-party endpoint.
            return validate_codex_base_url(CODEX_RESPONSES_BASE)
        if name == 'google-antigravity':
            from google_ai_oauth import BASE_URL
            return BASE_URL
        profile = self.provider_profile(name)
        if name in _MINIMAX_OAUTH_PROVIDERS and lease is not None and lease.source == "oauth":
            # Subscription tokens belong only on the fixed MiniMax inference
            # host for that region, not on an API-key gateway/base override.
            return str(profile.base_url).rstrip("/")
        options = self.provider_options(name)
        legacy_env = {
            "anthropic": "VARIANT1_ANTHROPIC_BASE", "openai": "VARIANT1_OPENAI_BASE",
            "xai": "VARIANT1_XAI_BASE", "nvidia": "VARIANT1_NVIDIA_BASE",
            "gemini": "VARIANT1_GEMINI_BASE",
        }
        base = str((lease.base_url if lease else "") or options.get("base_url") or
                   os.environ.get(legacy_env.get(name, ""), "") or
                   (profile.base_url if profile else "")).rstrip("/")
        # An API-key profile may intentionally target a compatible gateway. An
        # xAI subscription bearer may not: keep it on xAI-controlled HTTPS hosts
        # so a saved base-url override cannot exfiltrate the OAuth credential.
        if name == "xai" and lease is not None and lease.source == "oauth":
            try:
                from xai_oauth import validate_xai_https_url
                return validate_xai_https_url(base, label="xAI subscription base URL")
            except Exception as exc:
                raise LocalEngineError(str(exc)) from exc
        return base

    def list_provider_info(self) -> list[dict]:
        items = []
        for profile in self.provider_registry.list():
            name = profile.name
            records = self.credential_pools.public_records(name)
            api_key_configured = bool(records) or any(
                os.environ.get(env_name) for env_name in profile.env_vars
            )
            configured = self.has_cloud_key(name) or profile.auth_style == "optional"
            item = profile.public_dict(
                configured=configured, credential_count=len(records),
                model=self.get_cloud_model(name), base_url=self.provider_base_url(name))
            auth_methods = []
            if name in _NATIVE_OAUTH_PROVIDERS:
                auth_methods.append("oauth")
            if profile.env_vars:
                auth_methods.append("api_key")
            if name.startswith("custom-"):
                auth_methods.append("custom")
            elif (
                profile.auth_style == "optional"
                and not profile.env_vars
                and name in {"hermes", "ollama", "lmstudio"}
            ):
                auth_methods.append("external")
            item.update({
                "auth_methods": auth_methods,
                "api_key_configured": api_key_configured,
                "credential_env_vars": list(profile.env_vars),
                "origin": self.provider_registry.origin(name),
                "credential_strategy": self.credential_pools.strategy(name),
                "base_url_editable": name not in {
                    "ollama", "hermes", "openai-codex", "google-antigravity", *_MINIMAX_OAUTH_PROVIDERS,
                },
                "custom": name.startswith("custom-"),
            })
            items.append(item)
        return items

    def list_custom_endpoints(self) -> list[dict]:
        from model_providers.custom_endpoints import endpoint_records

        rows = []
        for row in endpoint_records(self.cfg):
            provider = row["id"]
            credentials = self.credential_pools.public_records(provider)
            rows.append({
                **row,
                "is_current": self.mode == "cloud" and self.cloud_provider == provider,
                "has_api_key": bool(credentials),
                "credential_count": len(credentials),
            })
        return rows

    async def validate_custom_endpoint(self, value: dict) -> dict:
        from model_providers.custom_endpoints import normalize_endpoint

        row = normalize_endpoint(value, require_model=False)
        if not row["discover_models"]:
            return {"ok": True, "reachable": None, "discovery_skipped": True,
                    "base_url": row["base_url"], "models": list(dict.fromkeys([row["model"], *row["models"]])) if row["model"] else row["models"],
                    "latency_ms": 0, "message": "Configuration is valid; model discovery is disabled."}
        key = str(value.get("api_key") or "").strip()
        if not key:
            leases = self.credential_pools.leases(row["id"])
            key = str(leases[0].secret if leases else "")
        headers = {"Accept": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        started = time.monotonic()
        url = f"{row['base_url'].rstrip('/')}/models"
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=8.0),
                follow_redirects=True,
                trust_env=False,
            ) as client:
                response = await client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
            source = payload.get("data") if isinstance(payload, dict) else payload
            models = []
            for item in source if isinstance(source, list) else ():
                model = (
                    str(item.get("id") or item.get("name") or "").strip()
                    if isinstance(item, dict) else str(item or "").strip()
                )
                if model and model not in models:
                    models.append(model)
                if len(models) >= 500:
                    break
            return {
                "ok": True,
                "reachable": True,
                "base_url": row["base_url"],
                "models": models,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "message": (
                    f"Endpoint is reachable; found {len(models)} model(s)."
                    if models else "Endpoint is reachable."
                ),
            }
        except Exception as exc:
            status = int(getattr(getattr(exc, "response", None), "status_code", 0) or 0)
            return {
                "ok": False,
                "reachable": bool(status),
                "base_url": row["base_url"],
                "models": [],
                "latency_ms": int((time.monotonic() - started) * 1000),
                "status_code": status,
                "message": str(exc)[:500],
            }

    def save_custom_endpoint(self, value: dict) -> dict:
        from model_providers.custom_endpoints import (
            endpoint_records,
            normalize_endpoint,
            profile_for_endpoint,
        )

        raw = dict(value) if isinstance(value, dict) else {}
        row = normalize_endpoint(raw)
        rows = endpoint_records(self.cfg)
        explicit_id = bool(str(raw.get("id") or "").strip())
        existing_ids = {item["id"] for item in rows}
        if not explicit_id and row["id"] in existing_ids:
            stem = row["id"]
            index = 2
            while f"{stem}-{index}" in existing_ids:
                index += 1
            row["id"] = f"{stem}-{index}"
        replaced = False
        next_rows = []
        for item in rows:
            if item["id"] == row["id"]:
                next_rows.append(row)
                replaced = True
            else:
                next_rows.append(item)
        if not replaced:
            if len(next_rows) >= 64:
                raise ValueError("custom endpoint limit reached")
            next_rows.append(row)

        cloud = self.cfg.setdefault("cloud", {})
        cloud["custom_endpoints"] = next_rows
        cloud[f"{row['id']}_model"] = row["model"]
        context_windows = cloud.setdefault("context_windows", {})
        for key in tuple(context_windows):
            if key.startswith(f"{row['id']}/"):
                context_windows.pop(key, None)
        if row["context_length"]:
            context_windows[f"{row['id']}/{row['model']}"] = row["context_length"]
        self.provider_registry.register(
            profile_for_endpoint(row),
            origin={"kind": "custom_endpoint", "name": row["name"]},
        )
        if raw.get("make_default") is True:
            cloud["provider"] = row["id"]
            _cfg_apply_mode(self.cfg, "cloud")
            self.mode = "cloud"
        key = str(raw.get("api_key") or "").strip()
        if key:
            self.credential_pools.replace(
                row["id"], key,
                label=str(raw.get("credential_label") or "Endpoint key"),
            )
        self.save_config(strict=True)
        return next(
            item for item in self.list_custom_endpoints()
            if item["id"] == row["id"]
        )

    def activate_custom_endpoint(self, endpoint_id: str) -> dict:
        endpoint_id = self._kn(endpoint_id)
        row = next(
            (item for item in self.list_custom_endpoints()
             if item["id"] == endpoint_id),
            None,
        )
        if row is None:
            raise ValueError("unknown custom endpoint")
        cloud = self.cfg.setdefault("cloud", {})
        cloud["provider"] = endpoint_id
        cloud[f"{endpoint_id}_model"] = row["model"]
        _cfg_apply_mode(self.cfg, "cloud")
        self.mode = "cloud"
        self.save_config(strict=True)
        return {**row, "is_current": True}

    def remove_custom_endpoint(self, endpoint_id: str) -> bool:
        from model_providers.custom_endpoints import endpoint_records

        endpoint_id = self._kn(endpoint_id)
        rows = endpoint_records(self.cfg)
        kept = [row for row in rows if row["id"] != endpoint_id]
        if len(kept) == len(rows):
            return False
        cloud = self.cfg.setdefault("cloud", {})
        cloud["custom_endpoints"] = kept
        cloud.pop(f"{endpoint_id}_model", None)
        for key in tuple((cloud.get("context_windows") or {})):
            if str(key).startswith(f"{endpoint_id}/"):
                cloud["context_windows"].pop(key, None)
        for section in (
            "credential_pools", "pool_strategies", "pool_cursors",
            "provider_options", "keys",
        ):
            block = cloud.get(section)
            if isinstance(block, dict):
                block.pop(endpoint_id, None)
        fallbacks = cloud.get("fallback_chain")
        if isinstance(fallbacks, list):
            cloud["fallback_chain"] = [
                item for item in fallbacks if self._kn(item) != endpoint_id
            ]
        if self.mode == "cloud" and self.cloud_provider == endpoint_id:
            cloud["provider"] = "anthropic"
            _cfg_apply_mode(self.cfg, "local")
            self.mode = "local"
        self.provider_registry.unregister(endpoint_id)
        self.credential_pools.clear_runtime_status(endpoint_id)
        self.save_config(strict=True)
        return True

    def list_cloud_credentials(self, provider: str) -> list[dict]:
        return self.credential_pools.public_records(provider)

    def add_cloud_credential(self, provider: str, secret: str, **fields) -> dict:
        return self.credential_pools.add(provider, secret, **fields)

    def replace_cloud_credential(self, provider: str, secret: str) -> dict:
        profile = self.provider_profile(provider)
        label = f"{profile.display_name} API key" if profile else "API key"
        return self.credential_pools.replace(provider, secret, label=label)

    def clear_cloud_credentials(self, provider: str) -> bool:
        return self.credential_pools.clear(provider)

    def remove_cloud_credential(self, provider: str, credential_id: str) -> bool:
        if self.provider_profile(provider) is None:
            raise ValueError(f"unknown model provider: {provider}")
        return self.credential_pools.remove(provider, credential_id)

    def set_cloud_credential_enabled(
        self, provider: str, credential_id: str, enabled: bool,
    ) -> bool:
        if self.provider_profile(provider) is None:
            raise ValueError(f"unknown model provider: {provider}")
        return self.credential_pools.set_enabled(provider, credential_id, enabled)

    def set_cloud_credential_priority(
        self, provider: str, credential_id: str, priority: int,
    ) -> bool:
        if self.provider_profile(provider) is None:
            raise ValueError(f"unknown model provider: {provider}")
        return self.credential_pools.set_priority(provider, credential_id, priority)

    def set_cloud_credential_strategy(self, provider: str, strategy: str) -> None:
        if self.provider_profile(provider) is None:
            raise ValueError(f"unknown model provider: {provider}")
        self.credential_pools.set_strategy(provider, strategy)

    def get_fallback_chain(self) -> list[str]:
        chain = (self.cfg.get("cloud", {}) or {}).get("fallback_chain") or []
        out = []
        for item in chain if isinstance(chain, list) else []:
            name = self._kn(item)
            if name and name not in out and self.provider_registry.get(name):
                out.append(name)
        return out

    def set_fallback_chain(self, providers: list[str]) -> None:
        if not isinstance(providers, list):
            raise ValueError("fallback chain must be a list")
        chain = []
        for item in providers:
            name = self._kn(item)
            if not self.provider_registry.get(name):
                raise ValueError(f"unknown model provider: {item}")
            if name != self.cloud_provider and name not in chain:
                chain.append(name)
        self.cfg.setdefault("cloud", {})["fallback_chain"] = chain
        self.save_config()

    def has_cloud_key(self, provider: str) -> bool:
        if self.has_oauth(provider):
            return True
        if self.credential_pools.records(provider):
            return True
        profile = self.provider_profile(provider)
        if profile and profile.auth_style == "optional":
            return True
        return any(os.environ.get(name) for name in (profile.env_vars if profile else ()))

    def cloud_route_ready(self, *, require_vision: bool = False) -> bool:
        providers = [self.cloud_provider, *self.get_fallback_chain()]
        for provider in dict.fromkeys(providers):
            profile = self.provider_profile(provider)
            if profile and (not require_vision or profile.supports_vision) and self.has_cloud_key(provider):
                return True
        return False

    # --- subscription OAuth -------------------------------------------------
    # VARIANT-1-owned token pairs live under cloud.oauth[<provider>] with DPAPI
    # encryption. OpenAI Codex may instead be an explicit non-secret link to
    # Codex-managed ChatGPT auth; in that mode VARIANT-1 never persists the token.
    def _oauth_rec(self, provider: str) -> dict:
        return ((self.cfg.get("cloud", {}) or {}).get("oauth", {}) or {}).get(self._kn(provider)) or {}

    def has_oauth(self, provider: str) -> bool:
        rec = self._oauth_rec(provider)
        if (
            self._kn(provider) == "openai-codex"
            and rec.get("auth_flow") == "codex_cli"
            and rec.get("managed_external") is True
        ):
            try:
                from openai_codex_oauth import codex_cli_status

                return bool(codex_cli_status().get("connected"))
            except Exception:
                return False
        return bool(rec.get("access_token") or rec.get("refresh_token"))

    def get_oauth_access_token(self, provider: str) -> str:
        rec = self._oauth_rec(provider)
        if (
            self._kn(provider) == "openai-codex"
            and rec.get("auth_flow") == "codex_cli"
            and rec.get("managed_external") is True
        ):
            try:
                from openai_codex_oauth import load_codex_cli_tokens

                return load_codex_cli_tokens().access_token
            except Exception as e:
                print(
                    f"[router] Codex-managed OAuth token unavailable: {e}",
                    flush=True,
                )
                return ""
        expires_at = int(rec.get("expires_at") or 0)
        if expires_at and expires_at <= int(time.time()):
            return ""
        from security.secretstore import decrypt
        enc = rec.get("access_token") or ""
        if not enc:
            return ""
        try:
            return decrypt(enc)
        except Exception as e:
            print(f"[router] oauth token decrypt failed for {provider}: {e}", flush=True)
            return ""

    def set_oauth_tokens(self, provider: str, *, client_id: str = None,
                         access_token: str = None, refresh_token: str = None,
                         token_type: str = None, scope: str = None,
                         expires_at: int = None, auth_flow: str = None,
                         account_id: str = None,
                         project_id: str = None, account_tier: str = None,
                         managed_external: bool = None,
                         replace: bool = False):
        """Upsert the OAuth record for a provider. Only the fields passed are
        changed (mirrors credential-pool persistence), so a refresh that returns
        just a new access token keeps the existing refresh token."""
        from security.secretstore import encrypt
        oauth = self.cfg.setdefault("cloud", {}).setdefault("oauth", {})
        name = self._kn(provider)
        import copy
        previous = copy.deepcopy(oauth.get(name))
        previous_revision = self._oauth_revisions.get(name, 0)
        # Prepare encryption and conversions before publishing a new account.
        rec = {} if replace else copy.deepcopy(previous or {})
        if client_id is not None:
            rec["client_id"] = client_id
        if access_token is not None:
            rec["access_token"] = encrypt(access_token) if access_token else ""
        if refresh_token is not None:
            rec["refresh_token"] = encrypt(refresh_token) if refresh_token else ""
        if token_type is not None:
            rec["token_type"] = token_type or "Bearer"
        if scope is not None:
            rec["scope"] = scope
        if expires_at is not None:
            rec["expires_at"] = int(expires_at)
        if auth_flow is not None:
            rec["auth_flow"] = str(auth_flow or "").strip()
        if account_id is not None:
            rec["account_id"] = str(account_id or "").strip()
        if project_id is not None:
            rec['project_id'] = str(project_id or '').strip()
        if account_tier is not None:
            rec['account_tier'] = str(account_tier or '').strip()
        if managed_external is not None:
            rec["managed_external"] = bool(managed_external)
        oauth[name] = rec
        self._oauth_revisions[name] = previous_revision + 1
        try:
            if self.save_config() is False:
                raise OSError('OAuth credentials could not be saved.')
        except BaseException:
            if previous is None:
                oauth.pop(name, None)
            else:
                oauth[name] = previous
            self._oauth_revisions[name] = previous_revision
            raise
        self.refresh_support_matrix()
        self.credential_pools.clear_runtime_status(name, "oauth")

    def oauth_account_id(self, provider: str) -> str:
        return str(self._oauth_rec(provider).get("account_id") or "").strip()

    def oauth_required_for_route(self, provider: str) -> bool:
        name = self._kn(provider)
        if name in _MINIMAX_OAUTH_PROVIDERS or name == 'google-antigravity':
            return True
        if not self._oauth_rec(name):
            return False
        if name == "openai-codex":
            return True
        if name == "xai":
            policy = str(
                (self.cfg.get("cloud", {}) or {}).get("xai_credential_policy")
                or "subscription_first"
            ).strip().lower()
            return policy in {"subscription_first", "subscription_only"}
        return False

    def invalidate_oauth_access_token(self, provider: str) -> None:
        """Discard one rejected access generation while preserving refresh."""

        name = self._kn(provider)
        rec = self._oauth_rec(name)
        if not rec or (
            name == "openai-codex"
            and rec.get("auth_flow") == "codex_cli"
            and rec.get("managed_external") is True
        ):
            self.credential_pools.clear_runtime_status(name, "oauth")
            return
        rec["access_token"] = ""
        rec["expires_at"] = 0
        self.save_config()
        self.credential_pools.clear_runtime_status(name, "oauth")

    def clear_oauth(self, provider: str):
        name = self._kn(provider)
        previous_revision = self._oauth_revisions.get(name, 0)
        oauth = (self.cfg.get("cloud", {}) or {}).get("oauth", {})
        if isinstance(oauth, dict):
            previous = oauth.pop(name, None)
            self._oauth_revisions[name] = previous_revision + 1
            try:
                if self.save_config() is False:
                    raise OSError('OAuth disconnect could not be saved.')
            except BaseException:
                if previous is not None:
                    oauth[name] = previous
                self._oauth_revisions[name] = previous_revision
                raise
        self.refresh_support_matrix()
        self.credential_pools.clear_runtime_status(name, "oauth")

    def oauth_status(self, provider: str) -> dict:
        """Non-secret status for the UI/CLI: connected? which client? when it
        expires? Never returns token material."""
        import time
        rec = self._oauth_rec(provider)
        name = self._kn(provider)
        if (
            name == "openai-codex"
            and rec.get("auth_flow") == "codex_cli"
            and rec.get("managed_external") is True
        ):
            from openai_codex_oauth import codex_cli_status

            return {
                "provider": name,
                **codex_cli_status(),
                "client_id_set": bool(rec.get("client_id")),
                "scope": "",
                "token_type": "Bearer",
                "transport": "responses",
                "adapter": "openai_codex.responses",
                "source": "codex_cli",
            }
        now = int(time.time())
        exp = int(rec.get("expires_at") or 0)
        has_access = bool(rec.get("access_token"))
        has_refresh = bool(rec.get("refresh_token"))
        expired = bool(exp and exp <= now)
        return {
            "provider": name,
            "connected": bool(has_refresh or (has_access and not expired)),
            "usable": bool(has_access and not expired),
            "needs_refresh": bool(has_refresh and (not has_access or expired)),
            "client_id_set": bool(rec.get("client_id")),
            "expires_at": exp,
            "expires_in": max(0, exp - now) if exp else 0,
            "scope": rec.get("scope", ""),
            "token_type": rec.get("token_type", "Bearer"),
            "auth_flow": rec.get("auth_flow") or "",
            "managed_external": bool(rec.get("managed_external")),
            "account_id_present": bool(rec.get("account_id")),
            "project_id_present": bool(rec.get('project_id')),
            "account_tier": str(rec.get('account_tier') or ''),
            "transport": (
                "responses" if name in {"xai", "openai-codex"}
                else "anthropic" if name in _MINIMAX_OAUTH_PROVIDERS
                else "gemini" if name == 'google-antigravity' else ""
            ),
            "adapter": (
                "openai_codex.responses" if name == "openai-codex"
                else "xai.responses" if name == "xai"
                else "minimax_oauth.anthropic" if name in _MINIMAX_OAUTH_PROVIDERS
                else "google_ai.generate_content" if name == 'google-antigravity' else ""
            ),
            "source": "variant1",
        }

    async def ensure_oauth_fresh(self, provider: str, *, skew: int = None) -> bool:
        name = self._kn(provider)
        lock = self._oauth_refresh_locks.setdefault(name, asyncio.Lock())
        async with lock:
            return await self._ensure_oauth_fresh_locked(name, skew=skew)

    async def _ensure_oauth_fresh_locked(
        self, provider: str, *, skew: int = None,
    ) -> bool:
        """Refresh the stored OAuth access token if it's missing or about to
        expire. Best-effort: on failure the stale/absent token is left in place
        and the ensuing request surfaces the real error. Returns True when a
        usable (fresh-enough) access token is present afterward.

        xAI SuperGrok tokens are short-lived (~6h); Hermes uses a 1h early
        refresh skew so long-idle sessions stay warm.
        """
        import time as _time
        name = self._kn(provider)
        rec = self._oauth_rec(provider)
        if not rec:
            return False
        revision = self._oauth_revisions.get(name, 0)
        admitted_record = dict(rec)

        def still_owned():
            return (self._oauth_revisions.get(name, 0) == revision
                    and self._oauth_rec(name) == admitted_record)
        if (
            name == "openai-codex"
            and rec.get("auth_flow") == "codex_cli"
            and rec.get("managed_external") is True
        ):
            try:
                import openai_codex_oauth

                await openai_codex_oauth.ensure_codex_cli_fresh(
                    skew=120 if skew is None else skew
                )
                return still_owned()
            except Exception as e:
                print(
                    f"[router] Codex-managed OAuth refresh failed: {e}",
                    flush=True,
                )
                return False
        if skew is None:
            skew = 3600 if name == "xai" else 120
        exp = int(rec.get("expires_at") or 0)
        have_access = bool(rec.get("access_token"))
        if have_access and exp and exp - skew > int(_time.time()):
            return True  # still good
        refresh_enc = rec.get("refresh_token") or ""
        if not refresh_enc:
            return bool(
                have_access
                and (not exp or exp > int(_time.time()))
            )
        if name not in _NATIVE_OAUTH_PROVIDERS:
            return have_access
        try:
            from security.secretstore import decrypt
            if name == "xai":
                import xai_oauth

                cfg = xai_oauth.XaiOAuthConfig.from_env(
                    client_id=rec.get("client_id", "")
                )
                tok = await xai_oauth.refresh(cfg, decrypt(refresh_enc))
            elif name == 'google-antigravity':
                import google_ai_oauth
                tok = await google_ai_oauth.refresh(decrypt(refresh_enc))
            elif name in _MINIMAX_OAUTH_PROVIDERS:
                import minimax_oauth

                tok = await minimax_oauth.refresh(name, decrypt(refresh_enc))
            else:
                import openai_codex_oauth

                tok = await openai_codex_oauth.refresh(decrypt(refresh_enc))
        except Exception as e:
            print(f"[router] {name} oauth refresh failed: {e}", flush=True)
            return still_owned() and bool(
                have_access and (not exp or exp > int(_time.time()))
            )
        if not still_owned():
            return False
        self.set_oauth_tokens(
            name,
            access_token=tok.access_token,
            # OAuth servers may omit refresh_token when the existing token
            # remains valid. ``None`` preserves that chain; an empty string
            # would silently disconnect the next refresh.
            refresh_token=(tok.refresh_token or None),
            token_type=tok.token_type,
            scope=tok.scope or rec.get("scope", ""),
            expires_at=tok.expires_at,
            auth_flow=rec.get("auth_flow") or "",
            account_id=getattr(tok, "account_id", "") or rec.get("account_id", ""),
            managed_external=False,
        )
        return True

    def get_cloud_model(self, provider: str = None) -> str:
        p = self._kn(provider or self.cloud_provider)
        bound = _BOUND_MODEL_ROUTE.get()
        if isinstance(bound, dict):
            bound_provider = self._kn(str(bound.get("provider") or ""))
            bound_model = str(bound.get("model") or "").strip()
            if bound_model and p == bound_provider:
                return bound_model
        model_key = f"{p}_model"
        selected = (self.cfg.get("cloud", {}) or {}).get(model_key, "") or ""
        profile = self.provider_profile(p)
        return selected or (profile.default_model if profile else "")

    def set_cloud_model(self, provider: str, model: str):
        p = self._kn(provider)
        if p == "ollama":
            from model_runtime.ollama_cloud import require_cloud_model_id

            model = require_cloud_model_id(model)
        cloud = self.cfg.setdefault("cloud", {})
        cloud[f"{p}_model"] = model or ""
        self.save_config()

    def reasoning_efforts(self, provider: str = None, model: str = None) -> tuple[str, ...]:
        """Return the current model route's declared, provider-wire effort scale."""
        name = self._kn(provider or self.cloud_provider)
        profile = self.provider_profile(name)
        efforts = tuple(getattr(profile, "reasoning_efforts", ()) or ())
        selected = str(model or self.get_cloud_model(name) or "").lower()
        if name == "openai-codex" and not selected.startswith("gpt-5.6"):
            efforts = tuple(value for value in efforts if value != "max")
        return efforts

    def get_reasoning_effort(self, provider: str = None, model: str = None) -> str:
        """Resolve one session-bound reasoning effort without provider UI state."""
        name = self._kn(provider or self.cloud_provider)
        selected = str(model or self.get_cloud_model(name) or "")
        efforts = self.reasoning_efforts(name, selected)
        if not efforts:
            return ""
        bound = _BOUND_MODEL_ROUTE.get()
        requested = ""
        if isinstance(bound, dict) and self._kn(str(bound.get("provider") or "")) == name:
            requested = str(bound.get("reasoning_effort") or "").strip().lower()
        if requested in efforts:
            return requested
        if name == "xai" and "low" in efforts:
            return "low"
        if name == "openai-codex":
            return "max" if "max" in efforts else "xhigh"
        return "medium" if "medium" in efforts else efforts[0]

    def _credential_leases(self, provider: str) -> list[CredentialLease]:
        """Ordered credentials: connected OAuth, saved pool, environment, optional auth.

        xAI personal SuperGrok defaults to ``subscription_first``. OpenAI Codex
        is subscription-only and never falls through to API keys or anonymous
        access under this provider identity.
        """
        name = self._kn(provider)
        policy = "try_both"
        if name == "xai":
            policy = str(
                (self.cfg.get("cloud", {}) or {}).get("xai_credential_policy")
                or "subscription_first"
            ).strip().lower() or "subscription_first"
        elif name == "openai-codex":
            policy = "subscription_only"
        elif name in _MINIMAX_OAUTH_PROVIDERS or name == 'google-antigravity':
            policy = "subscription_only"
        leases = []
        oauth = self.get_oauth_access_token(name) if policy != "api_key_only" else ""
        if oauth:
            rec = self._oauth_rec(name)
            source = (
                "codex_cli_oauth"
                if name == "openai-codex" and rec.get("auth_flow") == "codex_cli"
                else "oauth"
            )
            lease = CredentialLease(
                name, "oauth", "Subscription", oauth, source=source
            )
            if self.credential_pools.available(lease):
                leases.append(lease)
        # SuperGrok personal path: Subscription alone (clear errors).
        if (
            name == "xai"
            and policy in {"subscription_first", "subscription_only"}
            and bool(self._oauth_rec(name))
        ):
            return leases
        if name == "openai-codex":
            return leases
        if policy != "subscription_only":
            leases.extend(self.credential_pools.leases(name))
        profile = self.provider_profile(name)
        seen = {lease.secret for lease in leases}
        if policy != "subscription_only":
            for env_name in profile.env_vars if profile else ():
                secret = os.environ.get(env_name) or ""
                if secret and secret not in seen:
                    lease = CredentialLease(name, f"env:{env_name}", env_name,
                                            secret, source="environment")
                    if not self.credential_pools.available(lease):
                        continue
                    leases.append(lease)
                    seen.add(secret)
        if profile and profile.auth_style == "optional" and not leases:
            lease = CredentialLease(name, "anonymous", "No authentication", "",
                                    source="anonymous")
            if self.credential_pools.available(lease):
                leases.append(lease)
        return leases

    @staticmethod
    def _provider_headers(profile, key: str) -> dict:
        headers = dict(profile.default_headers or {})
        if profile.auth_style == "x-api-key" and key:
            headers["x-api-key"] = key
        elif profile.auth_style == "bearer" and key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    @staticmethod
    def _openai_url(base: str, path: str) -> str:
        base = str(base or "").rstrip("/")
        clean = path.lstrip("/")
        return f"{base}/{clean}" if base.endswith("/v1") else f"{base}/v1/{clean}"

    async def list_cloud_models(
        self,
        provider: str = None,
        *,
        start_if_needed: bool = True,
    ):
        """Fetch available models from the cloud provider's API (for browse/select)."""
        prov = self._kn(provider or self.cloud_provider)
        profile = self.provider_profile(prov)
        if not profile:
            raise LocalEngineError(f"unknown cloud provider: {prov}")
        if prov.startswith("custom-"):
            from model_providers.custom_endpoints import endpoint_records
            endpoint = next((row for row in endpoint_records(self.cfg) if row["id"] == prov), None)
            if endpoint is not None and not endpoint["discover_models"]:
                return list(dict.fromkeys([endpoint["model"], *endpoint["models"]]))
        if prov == "ollama":
            from model_runtime.ollama_cloud import (
                OllamaCloudError,
                available_cloud_models,
            )

            try:
                return await available_cloud_models(start_if_needed=start_if_needed)
            except OllamaCloudError as exc:
                raise LocalEngineError(str(exc)) from exc
        if prov == "hermes":
            from model_runtime.hermes_proxy import HermesProxyError, available_models

            try:
                return await available_models(start_if_needed=start_if_needed)
            except HermesProxyError as exc:
                raise LocalEngineError(str(exc)) from exc
        if prov == 'google-antigravity':
            import google_ai_oauth
            if not await self.ensure_oauth_fresh(prov):
                raise LocalEngineError('Google AI subscription is not connected; sign in from Accounts.')
            leases = self._credential_leases(prov)
            if not leases:
                raise LocalEngineError('Google AI subscription has no usable token.')
            return await google_ai_oauth.list_models(leases[0].secret)
        if prov == "openai-codex":
            import openai_codex_oauth

            if not await self.ensure_oauth_fresh(prov):
                raise LocalEngineError(
                    "OpenAI Codex OAuth is not connected; link Codex or sign in"
                )
            leases = self._credential_leases(prov)
            if not leases:
                raise LocalEngineError(
                    "OpenAI Codex OAuth has no usable access token"
                )
            try:
                return await openai_codex_oauth.list_models(
                    leases[0].secret,
                    account_id=self.oauth_account_id("openai-codex"),
                )
            except openai_codex_oauth.OpenAICodexOAuthError as exc:
                raise LocalEngineError(str(exc)) from exc
        await self.ensure_oauth_fresh(prov)
        leases = self._credential_leases(prov)
        if not leases:
            raise LocalEngineError(f"no API key set for {prov} to list models")
        lease = leases[0]
        key = lease.secret
        base = self.provider_base_url(prov, lease)
        if not base:
            raise LocalEngineError(f"no base URL configured for {prov}")
        try:
            if profile.api_style == "openai":
                url = profile.models_url or self._openai_url(base, "models")
                headers = self._provider_headers(profile, key)
                async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                    r = await client.get(url, headers=headers)
                    if r.status_code != 200:
                        raise LocalEngineError(f"list models failed: {r.status_code} {r.text[:100]}")
                    data = r.json()
                    ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
                    return sorted(set(ids))
            elif profile.api_style == "anthropic":
                url = base.rstrip("/") + "/v1/models"
                headers = self._provider_headers(profile, key)
                headers["anthropic-version"] = "2023-06-01"
                async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                    r = await client.get(url, headers=headers)
                    if r.status_code != 200:
                        raise LocalEngineError(f"list models failed: {r.status_code}")
                    data = r.json()
                    ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
                    return sorted(ids)
            elif profile.api_style == "gemini":
                url = f"{base}/models?key={key}&pageSize=100"
                async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                    r = await client.get(url)
                    if r.status_code != 200:
                        raise LocalEngineError(f"list models failed: {r.status_code} {r.text[:100]}")
                    data = r.json()
                    ids = []
                    for m in data.get("models", []):
                        name = m.get("name", "").split("/")[-1]
                        methods = m.get("supportedGenerationMethods", [])
                        if "generateContent" in methods or "streamGenerateContent" in methods:
                            ids.append(name)
                    return sorted(ids)
            else:
                raise LocalEngineError(f"model listing not supported for provider {prov}")
        except LocalEngineError:
            raise
        except Exception as e:
            raise LocalEngineError(f"failed to list {prov} models: {e}")

    @property
    def engine_ready(self) -> bool:
        # Clear stale ready=true when the managed process has already exited.
        poll = getattr(self.engine, "poll_process", None)
        if callable(poll):
            poll()
        return bool(self.engine.ready)

    @property
    def model_name(self) -> str:
        model = str(getattr(self.engine, "model", "") or "")
        if not model:
            return ""
        return os.path.basename(model) if self.inference_runtime_id == "llamacpp" else model

    def active_model_name(self, route: str = None) -> str:
        """Human-readable model selected for one routed request."""
        selected = route if route in {"local", "cloud"} else self.mode
        if selected == "cloud":
            provider = self.cloud_provider
            return self.get_cloud_model(provider) or provider
        return self.model_name

    async def start_local(self) -> None:
        await self.engine.start()

    async def stop(self) -> None:
        await self._manifest_bus.stop()
        await self.engine.stop()

    def _thinking_enabled(self, reasoning_budget: int = None) -> bool:
        """Whether to enable provider/model thinking for this request.
        ``None`` follows the local runtime policy; ``0`` suppresses it for an
        explicitly lightweight internal request."""
        if reasoning_budget is not None:
            return int(reasoning_budget) != 0
        return bool(self.reasoning)

    async def count_tokens(self, text: str) -> int | None:
        """Exact token count for `text` when the local engine can provide one
        (llama-server /tokenize); None in cloud mode or on failure — callers keep
        their heuristic estimate."""
        if self.mode != "local":
            return None
        try:
            return await self.engine.count_tokens(text)
        except Exception:
            return None

    async def count_prompt_tokens(self, messages: list, *, tools: list = None,
                                  image_b64=None,
                                  reasoning_budget: int = None) -> int | None:
        """Exact local count for the same rendered chat request as inference.

        The local adapter owns message projection and native-tool schema
        serialization. Reusing it here prevents the transcript economy from
        budgeting a flattened approximation while llama.cpp actually receives
        a much larger templated request. Multimodal observations use the same
        OpenAI content blocks as generation. The engine prefers llama.cpp's
        native chat input-token endpoint for its media accounting; the legacy
        template/tokenize fallback is only a compatibility approximation and
        can differ in either direction for image-bearing prompts.
        """
        if self.mode != "local":
            return None
        try:
            rendered_messages = _render_local_messages(messages, image_b64)
            template_payload = _build_local_template_payload(
                self, rendered_messages, reasoning_budget, tools)
            return await self.engine.count_prompt_tokens(template_payload)
        except Exception:
            return None

    async def stream(self, messages: list, sampling: dict = None, json_mode: bool = False,
                     image_b64=None, reasoning_budget: int = None, reasoning_sink=None,
                     route: str | None = None, tools: list = None, tool_call_sink=None,
                     stream_diagnostics: StreamDiagnostics | None = None,
                     internal_projection: bool = False,
                     prompt_cache_key: str | None = None):
        """Yield reply tokens for the given chat messages.

        ``image_b64`` is the retained adapter name for one image or a list of
        typed transient observations. They are attached to the current user
        turn so multimodal local/cloud models see them directly.

        ``reasoning_budget`` can override the local runtime policy per request;
        pass ``0`` for an explicitly lightweight internal projection.

        ``route`` may pin a user-selected session route. A local request never
        changes to cloud, and a cloud request never changes to local, regardless
        of when an error occurs.

        `reasoning_sink`, if given, is called with each `reasoning_content` chunk a
        local thinking model emits. When a sink is provided the router never folds
        reasoning into the visible reply (so it can be shown separately, e.g. in the
        Activity Monitor); without one, legacy behaviour is preserved.

        ``tools`` is an optional list of provider function schemas
        calling. Streamed tool-call deltas go to ``tool_call_sink`` (a
        ``ToolCallAccumulator``). When tools are set, json_mode is ignored for
        the request body (providers reject tools + json_object together).
        """
        sampling = {**self.sampling, **(sampling or {})}
        prompt_cache_identity = resolve_prompt_cache_identity(prompt_cache_key)
        selected_mode = route if route in {"local", "cloud"} else self.mode
        provider = self.cloud_provider if selected_mode == "cloud" else "local"
        model_name = self.active_model_name(selected_mode) or "unselected"
        if selected_mode == "local":
            adapter = str(
                getattr(self.engine, "request_adapter", "")
                or f"{self.inference_runtime_id}.openai_chat_completions"
            )
            self.validate_model_request(
                provider="local",
                model=model_name,
                adapter=adapter,
                tools=tools,
                internal_projection=internal_projection,
            )
        try:
            from model_runtime.message_graph import build_message_graph
            vision_count = len(build_message_graph(messages, image_b64).images)
        except Exception:
            vision_count = (
                len(image_b64)
                if isinstance(image_b64, (list, tuple))
                else (1 if image_b64 else 0)
            )
        print(
            f"[model] dispatch route={selected_mode} "
            f"provider={provider} "
            f"model={model_name} "
            f"vision={'yes' if vision_count else 'no'} images={vision_count} "
            f"json={'yes' if json_mode and not tools else 'no'} "
            f"tools={len(tools) if tools else 0}",
            flush=True,
        )
        t0 = time.perf_counter()
        chunks = 0
        status = "ok"
        detail = ""
        stream_diagnostics = stream_diagnostics or StreamDiagnostics()
        diagnostic_tool_sink = counting_tool_sink(
            tool_call_sink, stream_diagnostics)
        model_call_token = begin_model_call(
            requested_route=route, selected_mode=selected_mode)
        try:
            if selected_mode == "local":
                stream_diagnostics.note_model("local", model_name)
                async with aclosing(self._call_local(
                        messages, sampling, json_mode, image_b64, reasoning_budget,
                        reasoning_sink=reasoning_sink, tools=tools,
                        tool_call_sink=diagnostic_tool_sink,
                        stream_diagnostics=stream_diagnostics,
                        prompt_cache_identity=prompt_cache_identity)) as owned_stream:
                    async for tok in owned_stream:
                        chunks += 1
                        yield tok
                return
            elif selected_mode == "cloud":
                async with aclosing(self._call_cloud(
                        messages, sampling, json_mode, image_b64, reasoning_budget,
                        reasoning_sink=reasoning_sink, tools=tools,
                        tool_call_sink=diagnostic_tool_sink,
                        stream_diagnostics=stream_diagnostics,
                        internal_projection=internal_projection,
                        prompt_cache_identity=prompt_cache_identity)) as owned_stream:
                    async for tok in owned_stream:
                        chunks += 1
                        yield tok
                return
            else:
                raise LocalEngineError(f"unknown mode: {selected_mode}")
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception as exc:
            code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
            try:
                code_i = int(code) if code is not None else 0
            except (TypeError, ValueError):
                code_i = 0
            if code_i:
                status = f"http_{code_i}"
            elif "timeout" in str(exc).lower():
                status = "timeout"
            else:
                status = "error"
            detail = str(exc).replace("\n", " ")[:160]
            raise
        finally:
            ms = int(max(0.0, time.perf_counter() - t0) * 1000)
            extra = f" detail={detail}" if detail else ""
            actual_provider = stream_diagnostics.provider or provider
            actual_model = stream_diagnostics.model or model_name
            print(
                f"[model] done route={selected_mode} provider={actual_provider} "
                f"model={actual_model} ms={ms} chunks={chunks} "
                f"tool_deltas={stream_diagnostics.tool_deltas} "
                f"finish={log_finish_reason(stream_diagnostics)} "
                f"status={status}{extra}",
                flush=True,
            )
            end_model_call(model_call_token)

    def set_local_gate(self, factory) -> None:
        """Wrap local generation in a scheduler slot. ``factory()`` returns an
        async context manager acquired around each local call (track 3). The
        cloud path never contends for the single local engine, so it's ungated."""
        self._local_gate = factory

    async def _call_local(self, messages: list, sampling: dict, json_mode: bool = False,
                          image_b64: str = None, reasoning_budget: int = None,
                          reasoning_sink=None, tools: list = None, tool_call_sink=None,
                          stream_diagnostics=None, prompt_cache_identity=None):
        async with aclosing(_local_call_local(
                self, messages, sampling, json_mode, image_b64, reasoning_budget,
                reasoning_sink=reasoning_sink, tools=tools, tool_call_sink=tool_call_sink,
                stream_diagnostics=stream_diagnostics,
                prompt_cache_identity=prompt_cache_identity)) as owned_stream:
            async for tok in owned_stream:
                yield tok
    async def _call_cloud(self, messages: list, sampling: dict, json_mode: bool = False,
                          image_b64: str = None, reasoning_budget: int = None,
                          reasoning_sink=None, tools: list = None, tool_call_sink=None,
                          stream_diagnostics=None, internal_projection: bool = False,
                          prompt_cache_identity=None):
        async with aclosing(_cloud_call_cloud(
                self, messages, sampling, json_mode, image_b64, reasoning_budget,
                reasoning_sink=reasoning_sink, tools=tools, tool_call_sink=tool_call_sink,
                stream_diagnostics=stream_diagnostics,
                internal_projection=internal_projection,
                prompt_cache_identity=prompt_cache_identity)) as owned_stream:
            async for tok in owned_stream:
                yield tok
