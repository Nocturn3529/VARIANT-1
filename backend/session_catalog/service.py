"""Catalog publication, disclosure ranking, mounts, and projections."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any

from capability_broker import CapabilityRef, current_capability_invocation
from object_api import dispatch_object, register_object_tool
from prompt_builder import PromptProjection
from session_runtime import RuntimeIdentity
from tools import Tool, ToolError

from .catalog import (
    CATALOG_SCHEMA_REVISION,
    CatalogRepository,
    CatalogError,
    COVERED_BROKER_HANDLER_NAMES,
    MOUNTED_PYTHON_API_HANDLER_NAMES,
    LoadedCatalog,
    build_catalog_document,
    category_ids,
    handler_only_catalog_follow_allowed,
    iter_bindings,
)
from .children import ChildSessionManager, register_children_tool
from .mutation_authority import MutationAuthorityController
from .mutation import MutationManager, MUTATION_HANDLER
from .profiles import (
    DISCLOSURE_PROFILE,
    DISCLOSURE_REVISION,
    CHAT_GRAPH_REVISION,
    ACTION_SURFACE,
    IPYTHON_SCHEMA_REVISION,
    DISCLOSURE_WIDTH,
    WORKER_GRAPH_REVISION,
    is_action_surface,
)
from .toolbelt import register_toolbelt_tool


IPYTHON_HOST_CAPABILITY_CONTRACT = (
    "The persistent Python REPL is trusted same-user host Python, not a sandbox. "
    "Use Python to compose workflows. For launching and managing applications, "
    "prefer the provided process capability. Category mounts govern only host-integrated "
    "VARIANT-1 proxies; they do not restrict standard Python. The current project is the "
    "starting directory, not an access boundary. Follow explicit user-requested "
    "scope or isolated-environment boundaries."
)


BROWSER_SELECTION_GUIDANCE = (
    "For web tasks, use the browser capability. An explicit browser or tab in "
    "the user's request takes precedence over this chat's browser setting. "
    "Otherwise follow that setting, which defaults to VARIANT-1's built-in browser. "
    "An already-open external browser alone does not change this choice. "
    "Use another browser when the task requires it or to recover from an "
    "observed failure of the selected browser."
)


IPYTHON_PROVIDER_SPEC = {
    "name": "ipython",
    "description": (
        "Select a capability category or execute code in this chat's persistent "
        "Python REPL. "
        + IPYTHON_HOST_CAPABILITY_CONTRACT
        + " Ordinary completion, inspection, history, and code "
        "composition are native Python behavior inside the session. Category "
        "selection may be combined with execution in one call. "
        "Cells have no default wall-clock deadline; explicit interruption can stop active code. "
        "Build contains tools.run_command for managed applications/processes; "
        "Explore contains computer and browser for interaction. "
        "For web tasks, prefer browser and its built-in default; honor an explicit "
        "user browser choice, task requirement, or observed recovery need."
    ),
    "category": "infrastructure",
    "params": {
        "category": {
            "type": "string",
            "desc": "Category ID, title, or documented alias to select.",
        },
        "code": {
            "type": "string",
            "maxLength": 200_000,
            "desc": "Python code to execute in the persistent session.",
        },
    },
}


_AUTO_MOUNT_STRONG_SCORE = 52.0
_AUTO_MOUNT_EXPLICIT_SCORE = 28.0
_AUTO_MOUNT_LIVE_SCORE = 36.0
_AUTO_MOUNT_MIN_MARGIN = 10.0

# Hidden transport handlers resolve admitted refs but never appear in model
# discovery or a mounted Python namespace.
INTERNAL_BROKER_HANDLER_NAMES = frozenset({
    MUTATION_HANDLER,
    "remote_handle_dispatch",
})
PROVIDER_FRONT_DOOR_NAMES = frozenset({str(IPYTHON_PROVIDER_SPEC["name"])})

_WORDS = re.compile(r"[a-z0-9_]+")
_OPAQUE_LABELS = re.compile(r"\b([a-z][a-z0-9_]*)-([a-z0-9]{8,})\b", re.I)
_DISCOVERY_STOPWORDS = frozenset({
    "a", "an", "and", "current", "find", "for", "get", "in", "it", "my",
    "of", "on", "one", "or", "please", "server", "that", "the", "this", "to",
    "tool", "tools", "use", "using", "with",
})
_LOG = logging.getLogger(__name__)


def _tokens(value: str) -> set[str]:
    raw = set(_WORDS.findall(str(value or "").casefold()))
    expanded = set(raw)
    for token in raw:
        expanded.update(part for part in token.split("_") if part)
        if (
            len(token) > 3
            and token.endswith("s")
            and not token.endswith(("is", "ss", "us"))
        ):
            expanded.add(token[:-1])
    return expanded


def _query_tokens(value: str) -> set[str]:
    text = str(value or "")
    opaque_prefixes = {
        prefix.casefold()
        for prefix, suffix in _OPAQUE_LABELS.findall(text)
        if any(character.isdigit() for character in suffix)
    }
    # Opaque values such as CHILD-4BC0... are task data, not evidence that the
    # task needs the children capability. Keep them out of semantic disclosure.
    return _tokens(text) - _DISCOVERY_STOPWORDS - opaque_prefixes


def _extension_skills(host: Any):
    runtime = getattr(host.require_runtime(), "extensions", None)
    service = getattr(runtime, "skills", None)
    if service is None:
        raise RuntimeError("extension skill catalog is unavailable")
    return service


def _search_available_skills(
    host: Any, query: str, limit: int, *, chat_id: str = ""
) -> list[dict[str, Any]]:
    return list(
        _extension_skills(host).search(
            query, max(1, min(int(limit or 8), 50)), chat_id=chat_id
        )
    )


def _inspect_available_skill(
    host: Any, name: str, *, chat_id: str = "",
) -> dict[str, Any] | None:
    return _extension_skills(host).inspect(name, chat_id=chat_id)


class CatalogService:
    """Host-owned capability catalog shared by every VARIANT-1 session."""

    def __init__(
        self,
        *,
        database_path: str,
        artifact_store: Any,
        registry: Any,
        broker: Any,
        runtime_registry: Any,
        enabled_resolver: Any,
        mutation_allowed: Any = None,
        host: Any = None,
        defer_publication: bool = False,
    ) -> None:
        self.artifact_store = artifact_store
        self.registry = registry
        self.broker = broker
        self.runtime_registry = runtime_registry
        self.enabled_resolver = enabled_resolver
        self.host = host
        self.repository = CatalogRepository(database_path, artifact_store)
        self.mutation = MutationManager(
            database_path,
            artifact_store=artifact_store,
            catalog_repository=self.repository,
            runtime_registry=runtime_registry,
            broker=broker,
            registry=registry,
            enabled_resolver=enabled_resolver,
            worker_root=os.path.join(os.path.dirname(database_path), "mutation-workers"),
            # Composition must inject host policy explicitly. Missing policy is
            # fail-closed so a partial graph cannot silently enable mutation.
            mutation_allowed=(
                mutation_allowed if callable(mutation_allowed)
                else (lambda: False)
            ),
        )
        self.children = (
            ChildSessionManager(database_path, host, artifact_store)
            if host is not None else None
        )
        self.mutation_authority = (
            MutationAuthorityController(host, self) if host is not None else None
        )
        self.register_infrastructure_tools()
        self._current_release_id = (
            "" if defer_publication else self.reconcile_registry()
        )

    @property
    def current_release_id(self) -> str:
        if not self._current_release_id:
            raise CatalogError(
                "catalog composition is incomplete; publish after all handlers register"
            )
        return self._current_release_id

    def reconcile_registry(self, *, require_complete: bool = False) -> str:
        if require_complete:
            missing = sorted(
                name for name in COVERED_BROKER_HANDLER_NAMES
                | MOUNTED_PYTHON_API_HANDLER_NAMES
                if self.registry.get(name) is None
            )
            if missing:
                raise CatalogError(
                    "composed host is missing mounted seed/API handlers: "
                    + ", ".join(missing)
                )
            registered = getattr(self.registry, "all", None)
            if callable(registered):
                unowned = sorted(
                    str(tool.name)
                    for tool in registered()
                    if (
                        str(tool.name) not in COVERED_BROKER_HANDLER_NAMES
                        and str(tool.name) not in MOUNTED_PYTHON_API_HANDLER_NAMES
                        and str(tool.name) not in INTERNAL_BROKER_HANDLER_NAMES
                        and str(tool.name) not in PROVIDER_FRONT_DOOR_NAMES
                    )
                )
                if unowned:
                    raise CatalogError(
                        "registered methods exist outside the mounted catalog, "
                        "provider front door, or internal transport: "
                        + ", ".join(unowned)
                    )
        document = build_catalog_document(self.registry)
        self._current_release_id = self.repository.publish(
            document, make_current=True
        )
        return self._current_release_id

    def identity(
        self,
        *,
        environment_digest: str,
    ) -> RuntimeIdentity:
        return RuntimeIdentity(
            action_surface=ACTION_SURFACE,
            provider_tool_schema_revision=IPYTHON_SCHEMA_REVISION,
            graph_revision=CHAT_GRAPH_REVISION,
            catalog_release_id=self.current_release_id,
            environment_digest=str(environment_digest or ""),
            trust_profile="trusted-local.v1",
            disclosure_profile_id=DISCLOSURE_PROFILE,
            disclosure_profile_revision=DISCLOSURE_REVISION,
            discovery_state_ref="",
            mount_revision=0,
            selected_category_id="",
            overlay_revision=0,
        )

    def _record_and_catalog(self, chat_id: str) -> tuple[Any, LoadedCatalog]:
        record = self.runtime_registry.ensure_runtime(chat_id)
        loaded = self.repository.load(record.identity.catalog_release_id)
        if loaded.fallback_from:
            self.repository.recover_chat_catalog(
                chat_id,
                expected_release_id=record.identity.catalog_release_id,
                fallback_release_id=loaded.release_id,
            )
            record = self.runtime_registry.ensure_runtime(chat_id)
            loaded = self.repository.load(record.identity.catalog_release_id)
        current = self.repository.current()
        if (
            loaded.release_id != current.release_id
            and record.identity.action_surface == ACTION_SURFACE
            and self.repository.catalog_follow_eligible(chat_id)
            and handler_only_catalog_follow_allowed(
                loaded.document, current.document
            )
        ):
            try:
                self.repository.follow_handler_only_release(
                    chat_id,
                    expected_release_id=loaded.release_id,
                    target_release_id=current.release_id,
                )
            except CatalogError:
                _LOG.warning(
                    "handler-only catalog follow failed "
                    "chat_id=%s from_release=%s to_release=%s",
                    chat_id,
                    loaded.release_id,
                    current.release_id,
                    exc_info=True,
                )
            else:
                record = self.runtime_registry.ensure_runtime(chat_id)
                loaded = self.repository.load(record.identity.catalog_release_id)
        return record, loaded

    @staticmethod
    def resolve_category(document: dict[str, Any], value: str) -> str:
        clean = " ".join(str(value or "").strip().casefold().split())
        if not clean:
            raise CatalogError("category selection is empty")
        matches: list[str] = []
        for row in document.get("categories") or ():
            category_id = str(row.get("category_id") or "")
            aliases = {
                category_id.casefold(),
                str(row.get("title") or "").casefold(),
                *(str(item).casefold() for item in (row.get("aliases") or ())),
            }
            if clean in aliases:
                matches.append(category_id)
        if len(matches) != 1:
            choices = ", ".join(category_ids(document))
            raise CatalogError(
                f"unknown capability category {value!r}; choose one of: {choices}"
            )
        return matches[0]

    @staticmethod
    def _category(document: dict[str, Any], category_id: str) -> dict[str, Any] | None:
        return next((
            row for row in (document.get("categories") or ())
            if str(row.get("category_id") or "") == str(category_id or "")
        ), None)

    @staticmethod
    def _binding_condition_enabled(
        binding: dict[str, Any],
        condition_flags: dict[str, bool] | None = None,
    ) -> bool:
        condition = str(binding.get("condition") or "")
        if not condition:
            return True
        return bool((condition_flags or {}).get(condition))

    def _ref(
        self,
        binding: dict[str, Any],
        *,
        release_id: str,
        category_id: str,
        position: int,
        slot_version: int = 0,
    ) -> CapabilityRef:
        return CapabilityRef(
            capability_id=str(binding.get("capability_id") or ""),
            schema_revision=str(binding.get("schema_revision") or ""),
            handler_revision=str(binding.get("handler_revision") or ""),
            catalog_release_id=release_id,
            slot_id=f"{release_id}/{category_id}/{int(position)}",
            slot_version=int(slot_version),
        )

    @staticmethod
    def _descriptor(
        binding: dict[str, Any],
        ref: CapabilityRef,
        *,
        alias: str,
        category_id: str,
        position: int,
        mount_revision: int,
        namespace: str | None = None,
        mount_mode: str = "selected",
    ) -> dict[str, Any]:
        projected_namespace = str(
            namespace if namespace is not None else binding.get("namespace") or "tools"
        )
        return {
            "alias": str(alias),
            "namespace": projected_namespace,
            "qualified_alias": (
                str(alias)
                if projected_namespace == "tools"
                else f"{projected_namespace}.{alias}"
            ),
            "bundle": str(binding.get("bundle") or ""),
            "mount_mode": str(mount_mode or "selected"),
            "ref_id": ref.opaque_id,
            "capability_id": ref.capability_id,
            "schema_revision": ref.schema_revision,
            "handler_revision": ref.handler_revision,
            "effect_class": str(binding.get("effect_class") or "external_side_effect"),
            "description": str(binding.get("description") or ""),
            "params": dict(binding.get("params") or {}),
            "signature": str(binding.get("signature") or ""),
            "catalog_release_id": ref.catalog_release_id,
            "category_id": category_id,
            "position": int(position),
            "slot_id": ref.slot_id,
            "slot_version": int(ref.slot_version),
            "mount_revision": int(mount_revision),
        }

    @staticmethod
    def _mounted_object_descriptor(
        slot: dict[str, Any],
        transport: dict[str, Any],
        *,
        category_id: str,
        position: int,
        mount_revision: int,
        mount_mode: str,
        condition_flags: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        """Expand one broker transport into locally documented object methods."""

        name = str(slot.get("bundle") or "")
        methods: list[dict[str, Any]] = []
        for method in slot.get("methods") or ():
            if not isinstance(method, dict):
                continue
            if not CatalogService._binding_condition_enabled(
                method, condition_flags
            ):
                continue
            alias = str(method.get("alias") or "")
            descriptor = {
                **transport,
                **dict(method),
                "kind": "mounted_object_method",
                "alias": alias,
                "namespace": name,
                "qualified_alias": f"{name}.{alias}",
                "source_alias": str(transport.get("alias") or ""),
                "fixed_arguments": {
                    "operation": str(method.get("operation") or alias),
                },
            }
            methods.append(descriptor)
        if not methods:
            raise CatalogError(f"mounted object {name!r} has no admitted methods")
        return {
            "kind": "mounted_object",
            "name": name,
            "alias": name,
            "namespace": name,
            "qualified_alias": name,
            "bundle": str(slot.get("bundle") or name),
            "summary": str(slot.get("object_summary") or ""),
            "mount_mode": str(mount_mode or "selected"),
            "methods": methods,
            "method_count": len(methods),
            "catalog_release_id": str(transport.get("catalog_release_id") or ""),
            "category_id": str(category_id),
            "position": int(position),
            "slot_id": str(transport.get("slot_id") or ""),
            "slot_version": int(transport.get("slot_version") or 0),
            "mount_revision": int(mount_revision),
        }

    def _catalog_index(
        self,
        loaded: LoadedCatalog,
        *,
        condition_flags: dict[str, bool] | None = None,
    ) -> list[dict[str, Any]]:
        enabled = set(self.enabled_resolver() or ())
        rows: list[dict[str, Any]] = []
        for category_id, position, binding in iter_bindings(loaded.document):
            source_namespace = str(binding.get("namespace") or "tools")
            projection = str(binding.get("projection") or "seeds")
            if projection == "seeds":
                namespace = source_namespace
            elif projection == "object":
                namespace = str(binding.get("bundle") or "")
            else:
                raise CatalogError(f"retired slot projection: {projection!r}")
            alias = str(binding.get("alias") or "")
            condition = str(binding.get("condition") or "")
            handler_enabled = str(binding.get("tool_name") or "") in enabled
            condition_enabled = self._binding_condition_enabled(
                binding, condition_flags
            )
            rows.append({
                "category_id": category_id,
                "position": position,
                "slot_id": f"{loaded.release_id}/{category_id}/{position}",
                "namespace": namespace,
                "source_namespace": source_namespace,
                "bundle": str(binding.get("bundle") or ""),
                "alias": alias,
                "projection": projection,
                "qualified_alias": (
                    alias
                    if namespace == "tools"
                    else namespace
                ),
                "call": (
                    f"tools.{alias}(**arguments)"
                    if namespace == "tools"
                    else f"{namespace}.documentation()"
                ),
                "signature": str(binding.get("signature") or ""),
                "description": str(binding.get("description") or ""),
                "when": str(binding.get("when") or ""),
                "avoid": str(binding.get("avoid") or ""),
                "effect_class": str(binding.get("effect_class") or ""),
                "condition": condition,
                "handler_enabled": handler_enabled,
                "enabled": handler_enabled and condition_enabled,
                "baseline_version": 0,
            })
        for category in loaded.document.get("categories") or ():
            category_id = str(category.get("category_id") or "")
            for api in category.get("python_apis") or ():
                if str(api.get("status") or "") != "available":
                    continue
                name = str(api.get("name") or "")
                transport = dict(api.get("transport") or {})
                methods = [
                    method for method in (api.get("methods") or ())
                    if isinstance(method, dict)
                    and self._binding_condition_enabled(method, condition_flags)
                ]
                handler_enabled = str(api.get("tool_name") or "") in enabled
                rows.append({
                    "category_id": category_id,
                    "position": 0,
                    "slot_id": f"{loaded.release_id}/{category_id}/api-{name}",
                    "namespace": name,
                    "source_namespace": name,
                    "bundle": name,
                    "alias": name,
                    "projection": "python_api",
                    "qualified_alias": name,
                    "call": f"{name}.documentation()",
                    "signature": f"{name}.documentation()",
                    "description": str(api.get("summary") or ""),
                    "when": str(transport.get("when") or ""),
                    "avoid": str(transport.get("avoid") or ""),
                    "effect_class": str(
                        transport.get("effect_class") or "external_side_effect"
                    ),
                    "condition": str(transport.get("condition") or ""),
                    "handler_enabled": handler_enabled,
                    "enabled": handler_enabled and bool(methods),
                    "baseline_version": 0,
                })
        return rows

    @staticmethod
    def _overlay_catalog_rows(
        overlays: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Project active session overlays into the ordinary discovery index."""

        rows: list[dict[str, Any]] = []

        def append(
            raw: dict[str, Any],
            *,
            parent: dict[str, Any] | None = None,
        ) -> None:
            owner = parent or raw
            alias = str(raw.get("alias") or "").strip()
            namespace = str(
                raw.get("namespace")
                or owner.get("namespace")
                or "tools"
            ).strip() or "tools"
            category_id = str(
                raw.get("category_id") or owner.get("category_id") or ""
            )
            position = int(raw.get("position") or owner.get("position") or 0)
            if not alias or not category_id or position <= 0:
                return
            signature = str(raw.get("signature") or f"{alias}(...)")
            qualified = (
                f"tools.{alias}" if namespace == "tools"
                else f"{namespace}.{alias}"
            )
            rows.append({
                "category_id": category_id,
                "position": position,
                "slot_id": str(raw.get("slot_id") or owner.get("slot_id") or ""),
                "namespace": namespace,
                "source_namespace": str(
                    raw.get("source_namespace") or namespace
                ),
                "bundle": str(raw.get("bundle") or owner.get("bundle") or ""),
                "alias": alias,
                "projection": (
                    "object" if parent is not None else "session_overlay"
                ),
                "qualified_alias": qualified,
                "call": (
                    f"tools.{signature}" if namespace == "tools"
                    else f"{namespace}.{signature}"
                ),
                "signature": signature,
                "description": str(raw.get("description") or ""),
                "when": "An active session-local adaptation matches the work.",
                "avoid": "",
                "effect_class": str(
                    raw.get("effect_class") or "external_side_effect"
                ),
                "condition": "session_overlay",
                "handler_enabled": True,
                "enabled": True,
                "baseline_version": int(
                    raw.get("slot_version") or owner.get("slot_version") or 0
                ),
                "session_local": True,
            })

        for overlay in overlays:
            if not isinstance(overlay, dict):
                continue
            if str(overlay.get("kind") or "") == "mounted_object":
                for method in overlay.get("methods") or ():
                    if isinstance(method, dict):
                        append(method, parent=overlay)
                continue
            append(overlay)
        return rows

    def _effective_catalog_index(
        self,
        loaded: LoadedCatalog,
        overlays: list[dict[str, Any]],
        *,
        condition_flags: dict[str, bool] | None = None,
    ) -> list[dict[str, Any]]:
        """Return the immutable catalog with active slot overlays applied."""

        overlay_rows = self._overlay_catalog_rows(overlays)
        active_positions = {
            (str(row["category_id"]), int(row["position"]))
            for row in overlay_rows
        }
        rows = [
            row for row in self._catalog_index(
                loaded, condition_flags=condition_flags,
            )
            if (
                str(row.get("category_id") or ""),
                int(row.get("position") or 0),
            ) not in active_positions
        ]
        rows.extend(overlay_rows)
        order = {
            category_id: index
            for index, category_id in enumerate(category_ids(loaded.document))
        }
        rows.sort(key=lambda row: (
            order.get(str(row.get("category_id") or ""), 999),
            int(row.get("position") or 0),
            str(row.get("qualified_alias") or row.get("alias") or ""),
        ))
        return rows

    def _live_connector_index(self) -> list[dict[str, Any]]:
        """Return connected MCP manifests as disclosure hints, not new tools.

        The connector remains callable only through the mounted ``connectors``
        object.  These rows let the ordinary ASTB ranker notice what is live so
        it can suggest Operate before the model guesses a route.
        """

        if self.host is None:
            return []
        try:
            extensions = getattr(self.host.require_runtime(), "extensions", None)
            mcp = getattr(extensions, "mcp", None)
            rows = list(mcp.catalog()) if mcp is not None else []
        except Exception:
            return []
        result: list[dict[str, Any]] = []
        for raw in rows:
            if not isinstance(raw, dict) or str(raw.get("kind") or "") != "tool":
                continue
            descriptor = (
                raw.get("descriptor")
                if isinstance(raw.get("descriptor"), dict)
                else {}
            )
            name = str(raw.get("name") or descriptor.get("name") or "").strip()
            server_id = str(raw.get("server_id") or "").strip()
            if not name:
                continue
            input_schema = descriptor.get("inputSchema", descriptor.get("input_schema"))
            properties = (
                input_schema.get("properties")
                if isinstance(input_schema, dict)
                and isinstance(input_schema.get("properties"), dict)
                else {}
            )
            parameter_terms: list[str] = []
            for parameter, raw_spec in properties.items():
                parameter_terms.append(str(parameter))
                if isinstance(raw_spec, dict) and raw_spec.get("description"):
                    parameter_terms.append(str(raw_spec["description"]))
            description = " ".join(
                str(value or "").strip()
                for value in (
                    descriptor.get("title"),
                    descriptor.get("description"),
                    " ".join(parameter_terms),
                )
                if str(value or "").strip()
            )[:4_000]
            result.append({
                "category_id": "operate",
                "position": 0,
                "slot_id": f"live-mcp/{server_id}/{name}",
                "namespace": "connectors",
                "source_namespace": server_id or "mcp",
                "bundle": "live_mcp",
                "alias": name,
                "projection": "live_connector_hint",
                "qualified_alias": f"connectors.{name}",
                "call": f"connectors.search(query={name!r}).top_match",
                "signature": "connectors.search(query=..., limit=...)",
                "description": description,
                "when": "A connected MCP tool directly matches the requested work.",
                "avoid": "",
                "effect_class": "external_side_effect",
                "condition": "live_connector",
                "handler_enabled": True,
                "enabled": True,
                "baseline_version": 0,
                "server_id": server_id,
                "live_connector": True,
            })
        return result

    def top_k(
        self,
        loaded: LoadedCatalog,
        query: str,
        *,
        width: int = DISCLOSURE_WIDTH,
        condition_flags: dict[str, bool] | None = None,
        catalog_index: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        terms = _query_tokens(query)
        if not terms:
            return []
        phrase = " ".join(str(query or "").casefold().split())
        scored: list[tuple[float, int, int, str, dict[str, Any]]] = []
        order = {
            category_id: index
            for index, category_id in enumerate(category_ids(loaded.document))
        }
        candidate_rows = list(
            catalog_index
            if catalog_index is not None
            else self._catalog_index(loaded, condition_flags=condition_flags)
        ) + self._live_connector_index()
        for row in candidate_rows:
            if not row["enabled"]:
                continue
            alias = str(row.get("qualified_alias") or row["alias"])
            public_alias = str(row.get("alias") or "")
            category_id = str(row["category_id"])
            alias_tokens = _tokens(alias)
            description_tokens = _tokens(
                f"{row['bundle']} {row['namespace']} {row['source_namespace']} "
                f"{row['description']} "
                f"{row['when']}"
            )
            avoid_tokens = _tokens(str(row.get("avoid") or ""))
            score = 0.0
            if phrase and phrase in {
                alias.casefold(), public_alias.casefold(),
            }:
                score += 100.0
            if phrase and phrase in alias.casefold():
                score += 30.0
            score += 14.0 * len(terms & alias_tokens)
            score += 8.0 * len(terms & _tokens(category_id))
            score += 2.0 * len(terms & description_tokens)
            score -= 3.0 * len(terms & avoid_tokens)
            if row.get("live_connector") and terms & (
                alias_tokens | description_tokens
            ):
                # A connected manifest is stronger evidence than a generic
                # seed description, while still requiring semantic overlap.
                score += 24.0
            if score <= 0.0:
                continue
            scored.append((
                -score,
                order.get(category_id, 999),
                int(row["position"]),
                alias,
                row,
            ))
        scored.sort(key=lambda item: item[:4])
        return [
            dict(item[-1], rank=index + 1, score=round(-item[0], 3))
            for index, item in enumerate(scored[:max(1, int(width))])
        ]

    def _mount_projection(
        self,
        loaded: LoadedCatalog,
        *,
        selected_category_id: str,
        mount_revision: int,
        condition_flags: dict[str, bool] | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        category = self._category(loaded.document, selected_category_id)
        if category is None:
            return [], "No capability category is mounted."
        enabled = set(self.enabled_resolver() or ())
        mount_mode = str(category.get("mount_mode") or "selected")
        descriptors: list[dict[str, Any]] = []
        lines = [
            f"{category['title']} mounted (revision {mount_revision}).",
            "Python calls below are available in the `code` field of `ipython`:",
        ]
        if selected_category_id == "build":
            lines.append(
                "For code changes, verify the requested behavior and run the "
                "project's relevant checks before finishing."
            )
        for slot in category.get("slots") or ():
            position = int(slot.get("position") or 0)
            projection = str(slot.get("projection") or "seeds")
            bindings = [
                row for row in (slot.get("bindings") or ())
                if (
                    str(row.get("tool_name") or "") in enabled
                    and self._binding_condition_enabled(row, condition_flags)
                )
            ]
            if not bindings:
                status = str(slot.get("status") or "vacant").upper()
                lines.append(f"- slot {position}: {status}")
                continue
            operations: list[dict[str, Any]] = []
            for binding in bindings:
                ref = self._ref(
                    binding,
                    release_id=loaded.release_id,
                    category_id=selected_category_id,
                    position=position,
                )
                alias = str(binding.get("alias") or "")
                operation = self._descriptor(
                    binding,
                    ref,
                    alias=alias,
                    category_id=selected_category_id,
                    position=position,
                    mount_revision=mount_revision,
                    namespace=(
                        "tools" if projection == "seeds"
                        else str(slot.get("bundle") or "")
                    ),
                    mount_mode=mount_mode,
                )
                operation["source_namespace"] = str(binding.get("namespace") or "tools")
                operation["source_alias"] = alias
                operations.append(operation)
            if projection == "seeds":
                descriptors.extend(operations)
                calls = []
                for row in operations:
                    signature = str(row.get("signature") or "").strip()
                    alias = str(row.get("alias") or "")
                    call = signature or f"{alias}(...)"
                    calls.append(f"tools.{call}")
                lines.append(
                    f"- slot {position}: " + "; ".join(calls)
                )
                continue
            if projection == "object":
                if len(operations) != 1:
                    raise CatalogError(
                        f"mounted object {slot.get('bundle')!r} must "
                        "resolve exactly one broker transport"
                    )
                mounted_object = self._mounted_object_descriptor(
                    slot,
                    operations[0],
                    category_id=selected_category_id,
                    position=position,
                    mount_revision=mount_revision,
                    mount_mode=mount_mode,
                    condition_flags=condition_flags,
                )
                descriptors.append(mounted_object)
                calls = []
                for method in mounted_object["methods"]:
                    signature = str(method.get("signature") or "").strip()
                    alias = str(method.get("alias") or "")
                    call = signature or f"{alias}(...)"
                    calls.append(f"{mounted_object['name']}.{call}")
                lines.append(
                    f"- slot {position}: " + "; ".join(calls)
                )
                continue
            raise CatalogError(
                f"retired slot projection: {selected_category_id}/{position} "
                f"uses {projection!r}"
            )
        return descriptors, "\n".join(lines)

    @staticmethod
    def _mount_callable_contract(row: dict[str, Any], namespace: str) -> str:
        """Expose bounded constraints from the effective, post-overlay schema."""
        alias = str(row.get("alias") or "")
        qualified = f"{namespace}.{alias}"
        signature = str(row.get("signature") or f"{alias}(...)")
        call = f"{namespace}.{signature}"
        if len(call) > 1_000:
            call = f"{qualified}(...); use {qualified}.describe() for the full signature"
        hints: list[str] = []
        used = 0
        params = dict(row.get("params") or {})
        ordered = sorted(params.items(), key=lambda item: not bool((item[1] or {}).get("enum")))
        for name, spec in ordered:
            if not isinstance(spec, dict):
                continue
            enum = spec.get("enum")
            detail = ""
            if isinstance(enum, (list, tuple)) and enum:
                choices = ", ".join(repr(value) for value in enum[:13])
                detail = (f"choices {choices}" if len(enum) <= 12 and len(choices) <= 160
                          else f"{len(enum)} choices; see .describe()")
            if "default" in spec:
                value = repr(spec["default"])
                default = f"default {value}" if len(value) <= 80 else "default shown in .describe()"
                detail = "; ".join(part for part in (detail, default) if part)
            description = " ".join(str(spec.get("desc") or spec.get("description") or "").split())
            if description and (enum or spec.get("type") == "boolean") and len(description) <= 240:
                detail = "; ".join(part for part in (detail, description) if part)
            if not detail:
                continue
            hint = f"{name}: {detail}"
            if used + len(hint) + 2 > 512:
                continue
            hints.append(hint)
            used += len(hint) + 2
        return call + ("\n    " + "; ".join(hints) if hints else "")

    @staticmethod
    def _render_mount_card(
        category: dict[str, Any],
        descriptors: list[dict[str, Any]],
        *,
        mount_revision: int,
    ) -> str:
        """Render the exact post-overlay callable contract for one mount."""

        category_id = str(category.get("category_id") or "")
        mounted = [row for row in descriptors if row.get("category_id") == category_id]
        direct_calls = sorted({
            f"{row.get('namespace') or 'tools'}.{row['alias']}"
            for row in mounted if row.get("kind") != "mounted_object" and row.get("alias")
        })
        global_objects = sorted({
            str(row.get("name") or row.get("alias"))
            for row in mounted if row.get("kind") == "mounted_object"
            and (row.get("name") or row.get("alias"))
        })
        lines = [
            f"{category.get('title') or category_id} mounted (revision {mount_revision}).",
            f"Direct calls: {', '.join(direct_calls) or 'none'}. "
            f"Top-level objects: {', '.join(global_objects) or 'none'}.",
            "This is a mount receipt. Use the injected globals below; keep receipts in a separate variable.",
            "Python calls below are available in the `code` field of `ipython`:",
        ]
        if category_id == "build":
            lines.append(
                "For code changes, verify the requested behavior and run the "
                "project's relevant checks before finishing."
            )
        by_position: dict[int, list[dict[str, Any]]] = {}
        for row in descriptors:
            if str(row.get("category_id") or "") != category_id:
                continue
            by_position.setdefault(int(row.get("position") or 0), []).append(row)
        for slot in category.get("slots") or ():
            position = int(slot.get("position") or 0)
            rows = by_position.get(position, [])
            if not rows:
                status = str(slot.get("status") or "vacant").upper()
                lines.append(f"- slot {position}: {status}")
                continue
            calls: list[str] = []
            for row in rows:
                if str(row.get("kind") or "") == "mounted_object":
                    object_name = str(row.get("name") or row.get("alias") or "")
                    for method in row.get("methods") or ():
                        calls.append(CatalogService._mount_callable_contract(method, object_name))
                    continue
                namespace = str(row.get("namespace") or "tools")
                calls.append(CatalogService._mount_callable_contract(row, namespace))
            lines.append(f"- slot {position}: " + "; ".join(calls))
        if any(
            row.get("kind") == "mounted_object" and row.get("name") == "computer"
            and {str(method.get("alias")) for method in row.get("methods", ())}
                >= {"observe", "click", "type_text"}
            for row in descriptors if row.get("category_id") == category_id
        ):
            lines.append(
                "Computer actions return the resulting DesktopView; assign and reuse it. "
                "Observe again when evidence is absent from that result."
            )
        if any(
            row.get("kind") == "mounted_object" and row.get("name") == "browser"
            and {str(method.get("alias")) for method in row.get("methods", ())}
                >= {"navigate", "read", "screenshot"}
            for row in descriptors if row.get("category_id") == category_id
        ):
            lines.append(
                "Browser: use this capability for web tasks, with the built-in browser as the default. "
                "An explicit user browser/tab request takes precedence over the chat's selection. "
                "result = browser.navigate(url) navigates the current tab using this chat's selection; "
                "kind='embedded' explicitly selects VARIANT-1's in-app browser. "
                "Check result.surface and result.browser_kind for the actual surface. Continue with "
                "result.page and result.session. result.session.new_page(url) opens another tab; "
                "result.session.pages() lists existing tabs. "
                "The global session object manages the Python kernel. "
                "Observation fields support attributes, indexing and get(); element handles expose "
                "name/role and callable methods. Save original screenshots with result.image.save(path) "
                "or read them with result.image.read_bytes(); image.size is a count. "
                "Downloads may start after an action replies: not_observed_yet is not a failure. "
                "Check result.session.history('downloads', operation_id=result.operation_id) before retrying; "
                "completed rows provide row['artifact'].save(path)."
            )
        return "\n".join(lines)

    def _python_api_projection(
        self,
        loaded: LoadedCatalog,
        *,
        category_id: str,
        mount_revision: int,
        condition_flags: dict[str, bool] | None = None,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, CapabilityRef]]:
        """Project category APIs without assigning them mutation slots."""
        category = self._category(loaded.document, category_id)
        if category is None:
            return {}, {}
        enabled = set(self.enabled_resolver() or ())
        mount_mode = str(category.get("mount_mode") or "selected")
        projected: dict[str, dict[str, Any]] = {}
        refs: dict[str, CapabilityRef] = {}
        for api in category.get("python_apis") or ():
            api_name = str(api.get("name") or "")
            if not api_name:
                continue
            transport = api.get("transport") if isinstance(api.get("transport"), dict) else {}
            tool_name = str(api.get("tool_name") or transport.get("tool_name") or "")
            if tool_name and tool_name not in enabled:
                continue
            capability_id = str(
                transport.get("capability_id") or tool_name or api_name
            )
            slot_id = f"{loaded.release_id}/{category_id}/api-{api_name}"
            ref = CapabilityRef(
                capability_id=capability_id,
                schema_revision=str(transport.get("schema_revision") or ""),
                handler_revision=str(transport.get("handler_revision") or ""),
                catalog_release_id=loaded.release_id,
                slot_id=slot_id,
                slot_version=0,
            )
            methods: list[dict[str, Any]] = []
            for method in api.get("methods") or ():
                if not isinstance(method, dict):
                    continue
                if not self._binding_condition_enabled(method, condition_flags):
                    continue
                alias = str(method.get("alias") or "")
                descriptor = {
                    **dict(transport),
                    **dict(method),
                    "kind": "python_api_method",
                    "alias": alias,
                    "namespace": api_name,
                    "api_name": api_name,
                    "qualified_alias": f"{api_name}.{alias}",
                    "fixed_arguments": {
                        "operation": str(method.get("operation") or alias),
                    },
                    "ref_id": ref.opaque_id,
                    "capability_id": ref.capability_id,
                    "schema_revision": ref.schema_revision,
                    "handler_revision": ref.handler_revision,
                    "catalog_release_id": ref.catalog_release_id,
                    "category_id": category_id,
                    "position": 0,
                    "slot_id": ref.slot_id,
                    "slot_version": int(ref.slot_version),
                    "mount_revision": int(mount_revision),
                    "mount_mode": mount_mode,
                }
                methods.append(descriptor)
            if methods:
                projected[api_name] = {
                    "name": api_name,
                    "summary": str(api.get("summary") or ""),
                    "category_id": category_id,
                    "mount_revision": int(mount_revision),
                    "methods": methods,
                }
                refs[ref.opaque_id] = ref
        return projected, refs

    @staticmethod
    def _partition_descriptors(
        descriptors: list[dict[str, Any]],
    ) -> tuple[
        list[dict[str, Any]],
        dict[str, dict[str, Any]],
    ]:
        capabilities: list[dict[str, Any]] = []
        mounted_objects: dict[str, dict[str, Any]] = {}
        for descriptor in descriptors:
            if str(descriptor.get("kind") or "") == "mounted_object":
                name = str(descriptor.get("name") or "")
                if not name or name in mounted_objects:
                    raise CatalogError(
                        f"duplicate or empty mounted-object name: {name!r}"
                    )
                mounted_objects[name] = descriptor
                continue
            namespace = str(descriptor.get("namespace") or "tools")
            if namespace == "tools":
                capabilities.append(descriptor)
            else:
                raise CatalogError(
                    f"retired service namespace projection: {namespace!r}"
                )
        return capabilities, mounted_objects

    @staticmethod
    def _refs_from_descriptors(
        descriptors: list[dict[str, Any]],
    ) -> dict[str, CapabilityRef]:
        refs: dict[str, CapabilityRef] = {}
        for descriptor in descriptors:
            category_id = str(descriptor.get("category_id") or "")
            position = int(descriptor.get("position") or 0)
            slot_id = str(descriptor.get("slot_id") or "")
            if not category_id or position <= 0 or not slot_id:
                raise CatalogError(
                    "model-facing catalog descriptors must be owned by a category slot"
                )
            slot_parts = slot_id.rsplit("/", 2)
            if (
                len(slot_parts) != 3
                or slot_parts[-2] != category_id
                or slot_parts[-1] != str(position)
            ):
                raise CatalogError("projected descriptor slot ownership is inconsistent")
            operations = (
                list(descriptor.get("methods") or ())
                if str(descriptor.get("kind") or "") == "mounted_object"
                else [descriptor]
            )
            if not operations:
                raise CatalogError("projected mounted object has no admitted methods")
            for operation in operations:
                if (
                    str(operation.get("slot_id") or "") != slot_id
                    or str(operation.get("category_id") or "") != category_id
                    or int(operation.get("position") or 0) != position
                ):
                    raise CatalogError("projected method slot ownership is inconsistent")
                operation_release_id = str(
                    operation.get("catalog_release_id") or ""
                ).strip()
                if not operation_release_id:
                    raise CatalogError(
                        "projected capability has no catalog release identity"
                    )
                ref = CapabilityRef(
                    capability_id=str(operation.get("capability_id") or ""),
                    schema_revision=str(operation.get("schema_revision") or ""),
                    handler_revision=str(operation.get("handler_revision") or ""),
                    catalog_release_id=operation_release_id,
                    slot_id=slot_id,
                    slot_version=int(operation.get("slot_version") or 0),
                )
                if ref.opaque_id != str(operation.get("ref_id") or ""):
                    raise CatalogError("projected capability reference is inconsistent")
                refs[ref.opaque_id] = ref
        return refs

    def _hidden_dispatch_ref(
        self, loaded: LoadedCatalog,
    ) -> dict[str, CapabilityRef]:
        hidden = self.registry.get("remote_handle_dispatch")
        if hidden is None:
            return {}
        ref = self.broker.ref_for_name(
            "remote_handle_dispatch", catalog_release_id=loaded.release_id
        )
        return {ref.opaque_id: ref}

    def _retained_category_ids(
        self,
        chat_id: str,
        *,
        catalog_release_id: str,
        selected_category_id: str,
    ) -> list[str]:
        """Categories explicitly mounted since the last revocation boundary."""

        retained = self.repository.retained_categories(chat_id, catalog_release_id)
        selected = str(selected_category_id or "")
        if selected and selected not in retained:
            retained.append(selected)
        return retained

    def namespace_document(
        self,
        chat_id: str,
        identity: Any | None = None,
        *,
        query: str = "",
    ) -> tuple[dict[str, Any], dict[str, CapabilityRef]]:
        record, loaded = self._record_and_catalog(chat_id)
        identity = record.identity
        if not is_action_surface(identity.action_surface):
            raise CatalogError("chat is not assigned to the supported Python profile")
        mount_revision = int(identity.mount_revision or 0)
        selected = str(identity.selected_category_id or "")
        mutation_authority = self.mutation.authority_status(chat_id)
        mutation_available = bool(
            mutation_authority.get("effective_write_enabled")
        )
        creation_marker = str(record.creation_saga_state or "")
        worker_source = (
            creation_marker.split(":", 1)[1]
            if creation_marker.startswith("worker:")
            else ""
        )
        condition_flags = {"mutation_write": mutation_available}
        current_catalog = (
            str(loaded.document.get("schema_revision") or "")
            == CATALOG_SCHEMA_REVISION
        )
        if not current_catalog:
            raise CatalogError(
                "this test session pins a retired catalog revision; start a new session"
            )
        descriptors: list[dict[str, Any]] = []
        mounted_objects: dict[str, dict[str, Any]] = {}
        python_apis: dict[str, dict[str, Any]] = {}
        python_api_refs: dict[str, CapabilityRef] = {}
        refs: dict[str, CapabilityRef] = {}
        retained_category_ids: list[str] = []
        retained_ref_ids: list[str] = []
        card = ""
        if current_catalog:
            if selected:
                category = self._category(loaded.document, selected)
                if category is not None and str(
                    category.get("mount_mode") or "selected"
                ) == "selected":
                    selected_descriptors, card = self._mount_projection(
                        loaded,
                        selected_category_id=selected,
                        mount_revision=mount_revision,
                        condition_flags=condition_flags,
                    )
                    descriptors.extend(selected_descriptors)
            base_apis, base_api_refs = self._python_api_projection(
                loaded,
                category_id="base",
                mount_revision=mount_revision,
                condition_flags=condition_flags,
            )
            python_apis.update(base_apis)
            python_api_refs.update(base_api_refs)
            overlay_descriptors, _overlay_refs = self.mutation.active_overlays(
                chat_id,
                loaded,
                mount_revision=mount_revision,
                condition_flags=condition_flags,
            )
            active_categories = {selected} if selected else set()
            active_positions = {
                (str(row.get("category_id") or ""), int(row.get("position") or 0))
                for row in overlay_descriptors
                if str(row.get("category_id") or "") in active_categories
            }
            if active_positions:
                descriptors = [
                    row for row in descriptors
                    if (
                        str(row.get("category_id") or ""),
                        int(row.get("position") or 0),
                    ) not in active_positions
                ]
                descriptors.extend(
                    row for row in overlay_descriptors
                    if str(row.get("category_id") or "") in active_categories
                )
            if selected:
                selected_category = self._category(loaded.document, selected)
                if selected_category is not None:
                    card = self._render_mount_card(
                        selected_category,
                        descriptors,
                        mount_revision=mount_revision,
                    )
            capabilities, mounted_objects = self._partition_descriptors(
                descriptors
            )
            refs = self._refs_from_descriptors(descriptors)
            refs.update(python_api_refs)
            refs.update(self._hidden_dispatch_ref(loaded))
            descriptors = capabilities
            retained_category_ids = self._retained_category_ids(
                chat_id,
                catalog_release_id=loaded.release_id,
                selected_category_id=selected,
            )
            retained_overlays, _retained_overlay_refs = (
                self.mutation.active_overlays(
                    chat_id,
                    loaded,
                    mount_revision=mount_revision,
                    condition_flags=condition_flags,
                )
            )
            for retained_category in retained_category_ids:
                retained_descriptors, _retained_card = self._mount_projection(
                    loaded,
                    selected_category_id=retained_category,
                    mount_revision=mount_revision,
                    condition_flags=condition_flags,
                )
                overlay_positions = {
                    int(row.get("position") or 0): row
                    for row in retained_overlays
                    if str(row.get("category_id") or "") == retained_category
                }
                if overlay_positions:
                    retained_descriptors = [
                        row for row in retained_descriptors
                        if int(row.get("position") or 0)
                        not in overlay_positions
                    ] + list(overlay_positions.values())
                retained_refs = self._refs_from_descriptors(
                    retained_descriptors
                )
                refs.update(retained_refs)
                retained_ref_ids.extend(retained_refs)
        mutation_document = {
            "availability": (
                "available" if mutation_available
                else "frozen_by_host"
                if mutation_authority.get("write_enabled")
                else "disabled_by_chat"
            ),
            "authority": mutation_authority,
            "status": self.mutation.status(chat_id, limit=8),
        }
        catalog_index = self._effective_catalog_index(
            loaded,
            overlay_descriptors,
            condition_flags=condition_flags,
        )
        top_k = self.top_k(
            loaded,
            query,
            condition_flags=condition_flags,
            catalog_index=catalog_index,
        )
        mount_history = self.repository.history(chat_id, limit=20)
        mutation_status = dict(mutation_document.get("status") or {})
        active_mutation_slots = {
            str(row.get("slot_id") or "")
            for row in (mutation_status.get("active") or ())
            if isinstance(row, dict) and row.get("slot_id")
        }
        category_options = []
        for row in (loaded.document.get("categories") or ()):
            if str(row.get("mount_mode") or "selected") != "selected":
                continue
            category_id = str(row.get("category_id") or "")
            vacancies = [
                f"{category_id}/{int(slot.get('position') or 0)}"
                for slot in (row.get("slots") or ())
                if (
                    str(slot.get("status") or "") == "vacant"
                    and (
                        f"{loaded.release_id}/{category_id}/"
                        f"{int(slot.get('position') or 0)}"
                    ) not in active_mutation_slots
                )
            ]
            category_options.append({
                "category_id": category_id,
                "title": str(row.get("title") or ""),
                "summary": str(row.get("summary") or ""),
                "seed_slots": sum(
                    1 for slot in (row.get("slots") or ())
                    if str(slot.get("status") or "") == "seed"
                ),
                "vacant_slots": len(vacancies),
                "vacant_slot_ids": vacancies,
            })
        document = {
            "schema": "variant1.astb.namespace.v1",
            "profile": str(identity.action_surface),
            "worker_revision": WORKER_GRAPH_REVISION,
            "catalog_release_id": loaded.release_id,
            "catalog_content_sha256": loaded.content_sha256,
            "catalog_fallback_from": loaded.fallback_from or None,
            "mount_revision": mount_revision,
            "selected_category_id": selected or None,
            "retained_category_ids": list(retained_category_ids),
            "retained_capability_ref_ids": sorted(set(retained_ref_ids)),
            "base_category_ids": [
                str(row.get("category_id") or "")
                for row in (loaded.document.get("categories") or ())
                if str(row.get("mount_mode") or "selected") == "base"
            ],
            "category_options": category_options,
            "capabilities": descriptors,
            # Empty by contract: ASTB has no parallel service-descriptor layer.
            "services": {},
            "mounted_objects": mounted_objects,
            "python_apis": python_apis,
            "catalog_index": catalog_index,
            "top_k": top_k,
            "mount_card": card,
            "mount_history": mount_history,
            "session": {
                "chat_id": str(chat_id),
                "kernel_generation": int(record.kernel_generation or 0),
                "continuation_state": str(record.continuation_state),
                "action_surface": str(identity.action_surface),
                "trust_profile": str(identity.trust_profile),
                "overlay_revision": int(identity.overlay_revision or 0),
                "mutation_write_enabled": bool(
                    mutation_authority.get("write_enabled")
                ),
                "mutation_authority_revision": int(
                    mutation_authority.get("authority_revision") or 0
                ),
            },
            "mutation": mutation_document,
            "worker": {
                "source": worker_source or None,
                "restricted": False,
            },
        }
        return document, refs

    def select(
        self,
        chat_id: str,
        category: str,
        *,
        reason: str = "model_select",
        expected_mount_revision: int | None = None,
    ) -> dict[str, Any]:
        record, loaded = self._record_and_catalog(chat_id)
        if not is_action_surface(record.identity.action_surface):
            raise CatalogError("category selection requires the supported Python profile")
        category_id = self.resolve_category(loaded.document, category)
        category_row = self._category(loaded.document, category_id)
        if category_row is not None and str(
            category_row.get("mount_mode") or "selected"
        ) != "selected":
            raise CatalogError(
                f"category {category_id!r} is base-mounted and cannot be selected"
            )
        selected = self.repository.select_mount(
            chat_id,
            catalog_release_id=loaded.release_id,
            category_id=category_id,
            expected_mount_revision=expected_mount_revision,
            reason=str(reason or "model_select"),
        )
        document, _ = self.namespace_document(chat_id, query=category_id)
        return {
            **selected,
            "schema": "variant1.astb.mount-selection.v1",
            "mount_card": document["mount_card"],
            "mutation": document["mutation"],
        }

    def reset(self, chat_id: str) -> dict[str, Any]:
        record, _loaded = self._record_and_catalog(chat_id)
        if not is_action_surface(record.identity.action_surface):
            raise CatalogError("toolbelt reset requires the supported Python profile")
        authority = self.mutation.authority_status(chat_id)
        mutation_reset = (
            self.mutation.reset_all(chat_id)
            if authority.get("effective_write_enabled")
            else {
                "ok": True,
                "reset_slots": 0,
                "preserved": True,
                "reason": (
                    "frozen_by_host"
                    if authority.get("write_enabled")
                    else "disabled_by_chat"
                ),
            }
        )
        reset = self.repository.reset_mount(chat_id)
        document, _ = self.namespace_document(chat_id)
        return {
            **reset,
            "schema": "variant1.astb.mount-reset.v1",
            "mount_card": document["mount_card"],
            "mutation": document["mutation"],
            "mutation_reset": mutation_reset,
        }

    async def structural_rebase(
        self,
        chat_id: str,
        *,
        target_release_id: str = "",
        expected_release_id: str = "",
        expected_runtime_version: int | None = None,
    ) -> dict[str, Any]:
        """Explicitly move one idle runtime across a structural catalog change."""

        record = self.runtime_registry.ensure_runtime(chat_id)
        if self.runtime_registry.is_busy(chat_id):
            raise CatalogError("structural catalog rebase requires an idle runtime")
        pinned = str(expected_release_id or record.identity.catalog_release_id)
        target = str(target_release_id or self.current_release_id)
        result = self.repository.structural_rebase(
            chat_id,
            expected_release_id=pinned,
            target_release_id=target,
            expected_runtime_version=(
                int(expected_runtime_version)
                if expected_runtime_version is not None
                else int(record.version)
            ),
            environment_digest=environment_digest(
                app_version=str(getattr(self.host, "version", "") or "0.1.0"),
                catalog_release_id=target,
            ),
        )
        kernel = (
            self.host.require_runtime().kernel
            if self.host is not None
            else None
        )
        if bool(result.get("changed")) and kernel is not None:
            result["kernel_fence"] = await kernel.restart(
                chat_id, reason="catalog_structural_rebase"
            )
        document, _refs = self.namespace_document(chat_id)
        return {
            **result,
            "schema": "variant1.astb.structural-rebase.v1",
            "mount_card": document["mount_card"],
            "mutation": document["mutation"],
        }

    def rebase_disposable_test_state(self) -> dict[str, Any]:
        """Direct startup cutover used only by the explicit dev-reset mode."""

        changed = []
        skipped = []
        for chat_id in self.repository.structurally_stale_chats(
            self.current_release_id
        ):
            if self.runtime_registry.is_busy(chat_id):
                skipped.append(chat_id)
                continue
            try:
                record = self.runtime_registry.ensure_runtime(chat_id)
                changed.append(self.repository.structural_rebase(
                    chat_id,
                    expected_release_id=record.identity.catalog_release_id,
                    target_release_id=self.current_release_id,
                    expected_runtime_version=record.version,
                    environment_digest=environment_digest(
                        app_version=str(
                            getattr(self.host, "version", "") or "0.1.0"
                        ),
                        catalog_release_id=self.current_release_id,
                    ),
                    reason="dev_reset_structural_cutover",
                ))
            except (CatalogError, RuntimeError, LookupError) as exc:
                # Dev reset is a best-effort cutover over disposable state. A
                # concurrent runtime CAS cannot invalidate the independent
                # context-memory reset that already committed.
                skipped.append({"chat_id": chat_id, "error": str(exc)})
        return {
            "schema": "variant1.astb.dev-structural-cutover.v1",
            "rebased": changed,
            "skipped_busy": skipped,
        }

    @staticmethod
    def _confident_auto_mount_category(
        document: dict[str, Any], query: str
    ) -> str:
        """Choose only a clearly dominant first mount; ambiguity stays visible."""

        history = [
            dict(row) for row in (document.get("mount_history") or ())
            if isinstance(row, dict)
        ]
        # A handler-only catalog follow is host maintenance, not a prior
        # category decision. It may advance the mount revision before the
        # first user prompt and must not consume that prompt's one auto-mount.
        user_mount_history = [
            row for row in history
            if str(row.get("reason") or "") != "catalog_handler_follow"
        ]
        if (
            document.get("selected_category_id")
            or user_mount_history
            or (int(document.get("mount_revision") or 0) and not history)
        ):
            return ""
        ranked = [
            dict(row) for row in (document.get("top_k") or ())
            if str(row.get("category_id") or "")
        ]
        if not ranked:
            return ""
        category_scores: dict[str, float] = {}
        for row in ranked:
            category_id = str(row.get("category_id") or "")
            category_scores[category_id] = max(
                category_scores.get(category_id, 0.0),
                float(row.get("score") or 0.0),
            )
        ordered = sorted(
            category_scores.items(), key=lambda item: (-item[1], item[0])
        )
        category_id, top_score = ordered[0]
        second_score = ordered[1][1] if len(ordered) > 1 else 0.0
        if top_score - second_score < _AUTO_MOUNT_MIN_MARGIN:
            return ""
        top_row = next(
            row for row in ranked
            if str(row.get("category_id") or "") == category_id
            and float(row.get("score") or 0.0) == top_score
        )
        query_text = str(query or "").casefold()
        alias = str(top_row.get("alias") or "").casefold()
        qualified_alias = str(
            top_row.get("qualified_alias") or ""
        ).casefold()
        explicit_alias = bool(
            qualified_alias and qualified_alias in query_text
        ) or bool(
            alias
            and alias in query_text
            and (len(alias) >= 8 or "_" in alias)
        )
        confident = (
            top_score >= _AUTO_MOUNT_STRONG_SCORE
            or (
                explicit_alias
                and top_score >= _AUTO_MOUNT_EXPLICIT_SCORE
            )
            or (
                bool(top_row.get("live_connector"))
                and top_score >= _AUTO_MOUNT_LIVE_SCORE
            )
        )
        return category_id if confident else ""

    def runtime_prompt_projection(
        self, chat_id: str, query: str = "", *, allow_auto_mount: bool = True,
    ) -> PromptProjection:
        """Project invariant ASTB instructions separately from live turn state.

        ``runtime_prompt`` below preserves the historical combined string for
        headless callers. Interactive chat uses these two tiers directly so a
        query, mount, or mutation-authority change does not rewrite the leading
        provider instructions.
        """
        document, _ = self.namespace_document(chat_id, query=query)
        auto_category = (
            self._confident_auto_mount_category(document, query)
            if allow_auto_mount else ""
        )
        if auto_category and not str(document.get("selected_category_id") or ""):
            try:
                self.select(
                    chat_id,
                    auto_category,
                    reason="host_ranked_auto_select",
                    expected_mount_revision=int(
                        document.get("mount_revision") or 0
                    ),
                )
                document, _ = self.namespace_document(chat_id, query=query)
            except CatalogError as exc:
                # Another admitted action may have selected a category after
                # prompt assembly began. Preserve that newer state instead of
                # turning deterministic disclosure into a provider failure.
                _LOG.info(
                    "ASTB auto-mount skipped for chat_id=%s category=%s: %s",
                    chat_id,
                    auto_category,
                    exc,
                )
                document, _ = self.namespace_document(chat_id, query=query)

        query_text = str(query or "").casefold()
        query_terms = _query_tokens(query_text)
        async_requested = any(
            token in query_text
            for token in ("async", "parallel", "concurr", "fanout", "independent reads")
        )
        handles_requested = any(
            token in query_text
            for token in ("durable handle", "reconnect", "terminal", "process", "browser tabs")
        )
        mutation_requested = bool(
            {"mutation", "mutations", "mutate", "mutated", "mutating"}
            & query_terms
        ) or any(
            phrase in query_text
            for phrase in ("vacant slot", "create a tool", "custom tool")
        )
        category_lines = [
            f"- {row['category_id']}: {row['summary']}"
            for row in (document.get("category_options") or ())
        ]
        ranked_top = [
            dict(row) for row in (document.get("top_k") or ())
            if float(row.get("score") or 0.0) >= 8.0
        ]
        selected_category_id = str(
            document.get("selected_category_id") or ""
        )
        top_lines = [
            f"- {row['category_id']}/{row['position']} {row.get('call')} "
            f"[{row['effect_class']}]"
            for row in ranked_top
            if (
                selected_category_id
                and str(row.get("category_id") or "") == selected_category_id
                and float(row.get("score") or 0.0) >= 14.0
            )
        ]
        top_roots = {
            str(
                row.get("qualified_alias")
                or row.get("alias")
                or ""
            ).split(".", 1)[0]
            for row in ranked_top
        }

        def root_relevant(name: str) -> bool:
            return str(name) in top_roots or str(name).casefold() in query_terms

        process_available = any(
            row.get("enabled")
            and row.get("category_id") == "build"
            and row.get("alias") == "run_command"
            for row in document.get("catalog_index") or ()
        )
        # Discovery stopwords remove broad nouns such as "server". Lifecycle
        # disclosure needs those nouns, and "background" alone is not evidence
        # of an OS process: ordinary asyncio work uses the same word.
        process_terms = _tokens(query_text)
        process_subject = bool(process_terms & {
            "application", "app", "program", "process", "service", "server",
            "executable", "exe", "obs", "notepad", "chrome", "firefox",
        })
        process_action = bool(process_terms & {
            "start", "stop", "run", "launch", "launching", "open", "restart",
            "restarting", "terminate", "kill", "manage", "retain", "reconnect",
        }) or {"keep", "running"}.issubset(process_terms)
        process_task = bool(process_terms & {"terminal", "terminals"}) or (
            process_subject and process_action
        ) or (
            bool(process_terms & {"process", "processes"})
            and bool(process_terms & {"handle", "handles", "list"})
        )

        mounted_objects = dict(document.get("mounted_objects") or {})
        python_apis = dict(document.get("python_apis") or {})
        root_objects = set(mounted_objects) | set(python_apis)
        mounted_seed_names = sorted(
            str(row.get("alias") or "")
            for row in (document.get("capabilities") or ())
            if row.get("alias")
        )
        active_globals = ["tools"] + sorted(root_objects)
        stable_parts = [
            "Complete the user’s task fully. Treat successful tool results and "
            "assertions as evidence. Verify any requested condition that is not "
            "already established, then stop calling tools and give the final answer.",
            "## Working environment",
            IPYTHON_HOST_CAPABILITY_CONTRACT,
            BROWSER_SELECTION_GUIDANCE,
            "Use `ipython` with `category`, `code`, or both. Python state "
            "persists and standard input is unavailable. Cells have no default "
            "wall-clock deadline; explicit interruption remains available during execution. "
            "Individual capabilities retain their documented operation timeouts. "
            "Keep useful read/search "
            "results in named variables and reuse them while inputs remain unchanged. "
            "Read the execution text/error and continue the task; inspect host ledger "
            "metadata only when the task requires it.",
            "Exactly one domain category is selectable at a time; immutable "
            "`toolbelt` and `session` base objects remain mounted. "
            "Use each call exactly as printed. Only `tools.x(...)` calls live "
            "under `tools`; `computer.x(...)`, `browser.x(...)`, and other "
            "object-prefixed calls use top-level globals. "
            "`toolbelt` provides category "
            "search, mounting, and mutation controls. A category may be selected "
            "in the same cell as related ordinary Python work. Use a newly mounted "
            "seed/object in that same cell only when its exact call contract is "
            "already known; otherwise select the category first. When deterministic "
            "steps do not require a model decision between them, compose their "
            "calculation, timed loops or specified start/wait/stop sequences, "
            "file or artifact writes, validation, and concise evidence "
            "in one cell; split when an observed result must determine the next action. "
            "Use standard serializers for structured files. Return concise results "
            "and relevant errors instead of reprinting whole files.\n"
             "Selectable categories:\n" + "\n".join(category_lines),
        ]
        current_parts: list[str] = []
        if process_available and process_task:
            current_parts.append(
                "For this application/process workflow, Build provides "
                "`tools.run_command`; Explore provides visual interaction. "
                "Select Build to read the process contract, then use "
                "`mode='process'` for an application that should remain running. "
                "Resolve its executable and working directory first; the project "
                "cwd may not contain the application's required resources. "
                "Two execution cells, with `exe_path` and `exe_dir` already resolved:\n"
                "1. `ipython(category='build', code=\"app_proc = tools.run_command("
                "argv=[exe_path], cwd=exe_dir, mode='process')\")`\n"
                "2. `ipython(category='explore', code=\"windows = computer.list_windows(); "
                "print(windows)\")`\n"
                "Choose the observed application window by its process/window "
                "identity before interacting. The retained `app_proc` handle "
                "remains usable across the category switch for `inspect()`, "
                "`read()`, `wait()` and `stop()`. Check the returned state when "
                "verifying startup or shutdown."
                )
        if not selected_category_id and ranked_top:
            suggested_category = str(ranked_top[0].get("category_id") or "")
            category = next(
                (
                    row for row in document.get("category_options") or ()
                    if str(row.get("category_id") or "") == suggested_category
                ),
                None,
            )
            if category is not None:
                live_hint = ""
                if ranked_top[0].get("live_connector"):
                    live_hint = (
                        " Connected match: `"
                        + str(ranked_top[0].get("call") or "")
                        + "`."
                    )
                current_parts.append(
                    "Likely capability category: "
                    f"`{suggested_category}` — {category.get('summary')}. "
                    "Pass it through `ipython(category=...)`; when code is supplied "
                    "in the same call, the result still includes its mount card."
                    + live_hint
                )
        elif top_lines:
            current_parts.append(
                "Potentially useful operations in the current category:\n"
                + "\n".join(top_lines[:3])
            )
        if document.get("selected_category_id"):
            current_parts.append("Current capability mount:\n" + document["mount_card"])
        current_parts.append(
            "The mount and global lists in this prompt describe its construction-time state. "
            "Later mount receipts supersede these lists. "
            "Turn-start globals: "
            + ", ".join(f"`{name}`" for name in active_globals)
            + ". Only the listed VARIANT-1 host proxies are mounted; standard Python "
            "remains available independently. Use `toolbelt.search(query)` for cross-mount "
            "discovery. "
            "Use `ipython(category=...)` or `toolbelt.mount(category=...)` to "
            "change the next-cell domain mount. An official proxy saved in a "
            "named variable remains usable while its capability revision and "
            "session scope remain unchanged."
        )
        if async_requested:
            current_parts.append(
                "Every host-integrated seed/object method keeps its synchronous call and "
                "also exposes `await object.method.async_(..., _deadline_ms=None)`. Use "
                "`asyncio.gather(...)` only for independent calls; host policy bounds "
                "actual concurrency and serializes unsafe effects."
            )
        if mounted_seed_names:
            current_parts.append(
                "The current mount card lists the exact direct-seed signatures. "
                "Those `tools` calls are Python expressions available in the persistent "
                "session through `ipython(code=...)`. Mutating an occupied seed preserves its existing name; "
                "synthesizing a vacancy creates one bounded session tool. Inspect an exact seed contract locally "
                "with `tools.name.describe()` (equivalent to "
                "`tools.name.documentation()`) or "
                "`tools.documentation('name')`; use its signature and params instead "
                "of guessing a call shape."
            )
        if python_apis:
            current_parts.append(
                "Mounted Python APIs: "
                + ", ".join(f"`{name}`" for name in sorted(python_apis))
                + ". These immutable base objects consume no mutation slots. Their "
                "entry signatures are in the mount card; use `object.methods()` or "
                "`object.describe('method')` only when that card lacks a needed detail."
            )
        if mounted_objects:
            current_parts.append(
                "Mounted slot-owned objects: "
                + ", ".join(f"`{name}`" for name in sorted(mounted_objects))
                + ". Each object owns one mutation slot and one invocation handler; "
                "their callable signatures are listed in the current mount card. "
                "Use `object.describe('method')` only when parameter descriptions "
                "or other details beyond the signature are needed."
            )
        if "read_file" in mounted_seed_names:
            read_guidance = (
                "`tools.read_file(path=...)` preserves exact line endings. Use ordinary "
                "Python loops for sequential reads."
            )
            if async_requested:
                read_guidance += (
                    " For independent reads use `asyncio.gather(*["
                    "tools.read_file.async_(path=p) for p in paths])`."
                )
            current_parts.append(read_guidance)
        if "artifacts" in mounted_objects and root_relevant("artifacts"):
            current_parts.append(
                "`artifacts.list/get/create` return bound versioned-artifact handles; "
                "continue revision, history, publication, alias, and export work on "
                "that handle. `artifacts.read_text(ref=...)` is only for a known "
                "chat-scoped CAS reference."
            )
        if "skills" in root_objects and root_relevant("skills"):
            current_parts.append(
                "`skills.search(...)` and `skills.inspect(...)` load instructions, "
                "including permission-scoped app skills."
            )
        if "children" in mounted_objects and root_relevant("children"):
            current_parts.append(
                "`children.spawn(...)` starts an isolated worker and returns a bound "
                "child handle; use its `wait()`, `inspect()`, `send()`, `cancel()`, "
                "or `restart()` methods."
            )
        if "git" in root_objects and root_relevant("git"):
            current_parts.append(
                "Typed Git and managed worktrees live on `git`; durable PTY "
                "sessions on `terminal`; structured processes on `processes`. "
                "`tools.run_command(...)` remains the shell seed. Inspect "
                "`git.documentation()` inside a cell."
            )
        if "connectors" in root_objects:
            current_parts.append(
                "Resolve and compose connectors in the current cell: "
                "`found = connectors.search(...)`; `match = found.top_match`; "
                "the displayed primary match already includes its exact input "
                "schema, so use those names in `match.invoke(arguments=...)`. "
                "Call `match.schema()` only if that lease later reports stale. "
                "Mapping access such as "
                "`found['top_match']` and `found['mcp'][0]` remains equivalent. "
                "Inspect a secondary handle with `handle.schema()` before invoking it. "
                "Reuse the bound match "
                "and set `conclude=True` only on the final authoritative invocation "
                "when its result can complete the turn. Do not copy lease/package "
                "identity. If search "
                "returns no applicable connector, use desktop control only when the "
                "installed app UI can perform the task."
            )
        if handles_requested:
            current_parts.append(
                "Operations returning durable handles remain composable: save the "
                "returned object and call its documented methods. Exact revisions, "
                "scope, and stale-reference checks remain host-enforced."
            )
        availability = document.get("mutation", {}).get("availability")
        if availability == "disabled_by_chat" and mutation_requested:
            current_parts.append(
                "Session-local mutation authoring is off for this chat. Use the mounted "
                "static seeds; status remains readable through "
                "`toolbelt.mutation_status()`."
            )
        elif availability == "available":
            current_parts.append(
                "When a mounted callable blocks progress, or a working Python helper "
                "will be reused, adapt it as a normal recovery path. Use "
                "`toolbelt.mutate(toolbelt.last_failure(), using=helper, "
                "invoke={...})` to replace the failed callable, or "
                "`toolbelt.synthesize(helper, invoke={...})` to retain a new helper. "
                "The optional `invoke` arguments activate and run the adaptation in the "
                "same call; omit them when first use should happen later. Correct an "
                "ordinary argument mistake directly. VARIANT-1 infers the slot, schema, "
                "packaged imports/constants, dependencies, activation, and probation."
            )
        elif mutation_requested:
            current_parts.append(
                "Session-local mutation authoring is frozen by the host. Already activated "
                "session tools remain mounted and callable; rollback/reset remain available."
            )
        return PromptProjection(
            stable="\n\n".join(stable_parts),
            current="\n\n".join(current_parts),
        )

    def runtime_prompt(
        self, chat_id: str, query: str = "", *, allow_auto_mount: bool = True,
    ) -> str:
        """Return the legacy combined ASTB prompt for headless callers."""

        return self.runtime_prompt_projection(
            chat_id,
            query,
            allow_auto_mount=allow_auto_mount,
        ).combined()

    def register_infrastructure_tools(self) -> None:
        # The thin Build seed owns both chat-scoped CAS access and the
        # versioned artifact runtime behind one broker capability. Runtime
        # composition may not be complete yet; its handler resolves the host
        # runtime lazily when a versioned method is called.
        from artifacts.capabilities import register_artifact_tools

        register_artifact_tools(
            self.host,
            registry=self.registry,
        )
        register_children_tool(self.registry, self.children)

        if self.registry.get(MUTATION_HANDLER) is None:
            async def mutation_invoke(args: dict[str, Any]) -> Any:
                context = current_capability_invocation()
                if context is None:
                    raise ToolError("session mutation requires an active admitted Python cell")
                return await self.mutation.invoke(
                    context, dict(args.get("arguments") or {})
                )

            self.registry.register(Tool(
                MUTATION_HANDLER,
                "Invoke the exact active session-local slot version.",
                mutation_invoke,
                category="session_infrastructure",
                params={"arguments": {"type": "object", "required": True}},
                hidden=True,
                visibility="broker_only",
                effect_class="external_side_effect",
                parallel_safe=False,
                may_return_secrets=True,
                schema_revision="variant1.astb.mutation-envelope.v2",
                handler_revision="variant1.astb.mutation-worker.v2",
            ))

        register_toolbelt_tool(self.registry, self)

        from kernel_runtime.capabilities import SESSION_KERNEL_METHODS
        from session_catalog.outcomes import REPORT_OUTCOME_METHOD

        session_methods = (
            {
                "name": "status",
                "description": (
                    "Return compact durable chat, catalog, mount, kernel, budget, "
                    "and memory status."
                ),
                "effect_class": "read",
                "params": {},
            },
            *SESSION_KERNEL_METHODS,
            REPORT_OUTCOME_METHOD,
        )
        async def session_status(_args: dict[str, Any]) -> dict[str, Any]:
            context = current_capability_invocation()
            if context is None or not context.chat_id:
                raise ToolError(
                    "session.status() requires an active admitted Python cell"
                )
            record = self.runtime_registry.ensure_runtime(context.chat_id)
            identity = record.identity
            kernel_status: dict[str, Any] = {}
            if self.host is not None:
                try:
                    from kernel_runtime import control as kernel_control

                    kernel_status = kernel_control.status(
                        self.host, str(context.chat_id)
                    )
                except Exception:
                    kernel_status = {}
            return {
                "chat_id": record.chat_id,
                "work_scope": (
                    context.work_scope.to_dict()
                    if callable(getattr(context.work_scope, "to_dict", None))
                    else dict(context.work_scope or {})
                ),
                "action_surface": identity.action_surface,
                "catalog_release_id": identity.catalog_release_id,
                "mount_revision": int(identity.mount_revision or 0),
                "selected_category_id": identity.selected_category_id or None,
                "overlay_revision": int(identity.overlay_revision or 0),
                "kernel_generation": int(record.kernel_generation or 0),
                "kernel": kernel_status,
                "continuation_state": record.continuation_state,
                "budget_limits": dict(record.budget_limits),
                "budget_used": dict(record.budget_used),
            }

        if self.registry.get("session") is None:
            async def report_outcome(args):
                context = current_capability_invocation()
                if context is None or self.children is None:
                    raise ToolError('session.report_outcome requires an admitted child cell')
                return self.children.report_outcome(context.chat_id, context.run_id, **args)

            def kernel_handler(operation: str):
                async def invoke(args: dict[str, Any]):
                    from kernel_runtime.capabilities import session_kernel_operation

                    return await session_kernel_operation(
                        self.host, operation, dict(args or {})
                    )

                return invoke

            async def session(args: dict[str, Any]):
                return await dispatch_object(
                    session_methods,
                    args,
                    api_name="session",
                    handlers={
                        "status": session_status,
                        'report_outcome': report_outcome,
                        **{
                            str(method["name"]): kernel_handler(
                                str(method["name"])
                            )
                            for method in SESSION_KERNEL_METHODS
                        },
                    },
                )

            register_object_tool(
                self.registry,
                name="session",
                description=(
                    "Compact immutable session, WorkScope/goal attribution, continuity, "
                    "and recovery control."
                ),
                methods=session_methods,
                handler=session,
                category="session_infrastructure",
                schema_revision="variant1.session.v3",
                handler_revision="variant1.session-handler.v4",
                may_return_secrets=True,
            )

        host = self.host
        if host is not None and self.registry.get("skills") is None:
            skills_methods = (
                {
                    "name": "search",
                    "description": "Search compact metadata for skills contributed by enabled plugins.",
                    "effect_class": "read",
                    "params": {
                        "query": {"type": "string", "required": True},
                        "limit": {
                            "type": "integer", "required": False,
                            "minimum": 1, "maximum": 50,
                        },
                    },
                },
                {
                    "name": "inspect",
                    "description": "Load exact instructions for one versioned skill.",
                    "effect_class": "read",
                    "params": {"name": {"type": "string", "required": True}},
                },
            )

            async def skills_search(args: dict[str, Any]) -> list[dict[str, Any]]:
                context = current_capability_invocation()
                return _search_available_skills(
                    host,
                    str(args.get("query") or ""),
                    int(args.get("limit") or 8),
                    chat_id=str(context.chat_id if context is not None else ""),
                )

            async def skills_inspect(args: dict[str, Any]) -> dict[str, Any]:
                context = current_capability_invocation()
                chat_id = str(context.chat_id if context is not None else "")
                result = _inspect_available_skill(
                    host, str(args.get("name") or ""),
                    chat_id=chat_id,
                )
                if result is None:
                    raise ToolError("skill is unavailable")
                return result

            skills_handlers = {
                "search": skills_search,
                "inspect": skills_inspect,
            }

            async def skills(args: dict[str, Any]):
                return await dispatch_object(
                    skills_methods, args, api_name="skills",
                    handlers=skills_handlers,
                )

            register_object_tool(
                self.registry,
                name="skills",
                description="Search and inspect skills contributed by enabled plugins.",
                methods=skills_methods,
                handler=skills,
                category="session_infrastructure",
                schema_revision="variant1.skills.v1",
                handler_revision="variant1.skills-handler.v1",
            )

def environment_digest(*, app_version: str, catalog_release_id: str) -> str:
    return hashlib.sha256(
        f"{app_version}\0{catalog_release_id}\0{WORKER_GRAPH_REVISION}".encode("utf-8")
    ).hexdigest()
