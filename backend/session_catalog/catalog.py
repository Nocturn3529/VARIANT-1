"""Immutable content-addressed catalog and durable mount repository."""

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from typing import Any, Iterable

from core_invariants import (
    canonical_json_bytes as canonical_bytes,
    sqlite_session_connection,
)
from .profiles import ACTION_SURFACE


CATALOG_SCHEMA = "variant1.astb.catalog.v1"
CATALOG_REPOSITORY_SCHEMA = 1
CATALOG_SCHEMA_REVISION = "6"


@dataclass(frozen=True)
class SeedBindingSpec:
    """One broker capability projected under one Python namespace alias."""

    namespace: str
    alias: str
    tool_name: str
    condition: str = ""


@dataclass(frozen=True)
class SeedSlotSpec:
    """One occupied direct-seed bundle or slot-owned mounted object."""

    bundle: str
    bindings: tuple[SeedBindingSpec, ...] = ()
    projection: str = "seeds"


@dataclass(frozen=True)
class PythonAPISpec:
    """One category-loaded Python object whose methods consume no seed slot."""

    name: str
    summary: str
    tool_name: str = ""
    bindings: tuple[SeedBindingSpec, ...] = ()


def _bindings(
    namespace: str,
    *rows: tuple[str, str] | tuple[str, str, str],
) -> tuple[SeedBindingSpec, ...]:
    return tuple(
        SeedBindingSpec(
            namespace=str(namespace),
            alias=str(row[0]),
            tool_name=str(row[1]),
            condition=str(row[2]) if len(row) == 3 else "",
        )
        for row in rows
    )


def _slot(
    bundle: str,
    *groups: tuple[SeedBindingSpec, ...],
    projection: str = "seeds",
) -> SeedSlotSpec:
    return SeedSlotSpec(
        bundle=str(bundle),
        bindings=tuple(binding for group in groups for binding in group),
        projection=str(projection or "seeds"),
    )


def _vacant() -> SeedSlotSpec:
    return SeedSlotSpec(bundle="", bindings=())


def _api(
    name: str,
    summary: str,
    *groups: tuple[SeedBindingSpec, ...],
    tool_name: str = "",
) -> PythonAPISpec:
    bindings = tuple(binding for group in groups for binding in group)
    if not bindings:
        dispatcher = str(tool_name or name)
        bindings = _bindings(str(name), (str(name), dispatcher))
    return PythonAPISpec(
        name=str(name),
        summary=str(summary),
        tool_name=str(tool_name or name),
        bindings=bindings,
    )


# Category mount modes are part of the immutable catalog contract. Base owns
# infrastructure objects that never consume or replace mutation positions.
CATEGORY_DEFINITIONS: tuple[tuple[str, str, str, str], ...] = (
    ("build", "Build", "Files, code, shell commands, application/process lifecycles, and artifacts.", "selected"),
    (
        "explore", "Explore",
        "Web, browser, desktop observation and action, and inspection.",
        "selected",
    ),
    (
        "operate", "Operate",
        "User interaction, peer and child agents, skills, and connectors.",
        "selected",
    ),
    ("base", "Base", "Immutable session and toolbelt control.", "base"),
)


# One position owns one atomic direct seed or one coherent mounted object.
# Conditions filter methods without changing immutable catalog identity.
SEED_SLOT_BLUEPRINT: dict[str, tuple[SeedSlotSpec, ...]] = {
    "build": (
        _slot("read_file", _bindings("tools", ("read_file", "read_file"))),
        _slot("glob", _bindings("tools", ("glob", "glob"))),
        _slot("grep", _bindings("tools", ("grep", "grep"))),
        _slot("apply_patch", _bindings("tools", ("apply_patch", "apply_patch"))),
        _slot("run_command", _bindings("tools", ("run_command", "run_command"))),
        _slot("artifacts", _bindings("artifacts",
            ("artifacts", "artifacts"),
        ), projection="object"),
        _vacant(),
        _vacant(),
    ),
    "explore": (
        _slot("web_search", _bindings("tools", ("web_search", "web_search"))),
        _slot("browser", _bindings("browser", ("browser", "browser")),
              projection="object"),
        _slot("computer", _bindings("computer", ("computer", "computer")),
              projection="object"),
        _vacant(),
    ),
    "operate": (
        _slot("ask_user", _bindings("tools", ("ask_user", "ask_user"))),
        _slot("children", _bindings("children",
            ("children", "children"),
        ), projection="object"),
        _slot("skills", _bindings("skills", ("skills", "skills")),
              projection="object"),
        _slot("connectors", _bindings("connectors", ("connectors", "connectors")),
              projection="object"),
        _slot("peers", _bindings("peers", ("peers", "peers")),
              projection="object"),
        _vacant(),
    ),
    "base": (),
}


# Only infrastructure roots are immutable category APIs. Every domain root is
# slot-owned so a chat can mutate it without replacing unrelated capabilities.
CATEGORY_PYTHON_API_BLUEPRINT: dict[str, tuple[PythonAPISpec, ...]] = {
    "build": (),
    "explore": (),
    "operate": (),
    "base": (
        _api("toolbelt", "Immutable category discovery, mounting, and mutation control."),
        _api("session", "Compact current chat, runtime, mount, and continuity status."),
    ),
}


def covered_broker_handler_names() -> frozenset[str]:
    """Return the low-level handlers absorbed by immutable mounted seeds."""

    return frozenset(
        binding.tool_name
        for slots in SEED_SLOT_BLUEPRINT.values()
        for slot in slots
        for binding in slot.bindings
    )


COVERED_BROKER_HANDLER_NAMES = covered_broker_handler_names()


ALL_MOUNTED_PYTHON_API_BINDINGS = tuple(
    (category_id, api.name, binding)
    for category_id, apis in CATEGORY_PYTHON_API_BLUEPRINT.items()
    for api in apis
    for binding in api.bindings
)
MOUNTED_PYTHON_API_BINDINGS = tuple(
    row for row in ALL_MOUNTED_PYTHON_API_BINDINGS
    if row[2].condition != "mutation_write"
)
CONDITIONAL_PYTHON_API_BINDINGS = tuple(
    row for row in ALL_MOUNTED_PYTHON_API_BINDINGS
    if row[2].condition == "mutation_write"
)
MOUNTED_PYTHON_API_HANDLER_NAMES = frozenset(
    binding.tool_name
    for _category_id, _api_name, binding in ALL_MOUNTED_PYTHON_API_BINDINGS
)


MOUNTED_OBJECT_BINDINGS = tuple(
    (category_id, position, slot.bundle, binding)
    for category_id, slots in SEED_SLOT_BLUEPRINT.items()
    for position, slot in enumerate(slots, start=1)
    for binding in slot.bindings
    if (
        binding.namespace != "tools"
        and binding.condition != "mutation_write"
    )
)
MOUNTED_OBJECT_HANDLER_NAMES = frozenset(
    binding.tool_name for _category, _position, _bundle, binding
    in MOUNTED_OBJECT_BINDINGS
)

_ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CatalogError(RuntimeError):
    pass


class CatalogCorruption(CatalogError):
    pass


class MountConflict(CatalogError):
    pass


@dataclass(frozen=True)
class LoadedCatalog:
    release_id: str
    content_sha256: str
    artifact_ref: str
    document: dict[str, Any]
    fallback_from: str = ""


def binding_signature(alias: str, params: dict[str, Any]) -> str:
    required: list[str] = []
    optional: list[str] = []
    for name, spec in (params or {}).items():
        target = required if bool((spec or {}).get("required")) else optional
        target.append(str(name) if target is required else f"{name}={(spec or {}).get('default')!r}")
    return f"{alias}({', '.join(required + optional)})"


def _binding(
    tool: Any,
    *,
    alias: str | None = None,
    namespace: str = "tools",
    bundle: str = "",
    condition: str = "",
    projection: str = "seeds",
) -> dict[str, Any]:
    alias = str(alias if alias is not None else getattr(tool, "name", "") or "")
    if not _ALIAS_RE.fullmatch(alias.replace("-", "_")):
        raise CatalogError(f"catalog alias is not a Python identifier: {alias!r}")
    alias = alias.replace("-", "_")
    clean_namespace = str(namespace or "tools").replace("-", "_")
    if not _ALIAS_RE.fullmatch(clean_namespace):
        raise CatalogError(
            f"catalog namespace is not a Python identifier: {namespace!r}"
        )
    metadata = (
        tool.broker_metadata()
        if callable(getattr(tool, "broker_metadata", None))
        else {}
    )
    params = json.loads(json.dumps(getattr(tool, "params", {}) or {}, default=str))
    return {
        "alias": alias,
        "namespace": clean_namespace,
        "projection": str(projection or "seeds"),
        "bundle": str(bundle or ""),
        "condition": str(condition or ""),
        "tool_name": str(getattr(tool, "name", "") or ""),
        "capability_id": str(metadata.get("capability_id") or alias),
        "schema_revision": str(metadata.get("schema_revision") or "unversioned"),
        "handler_revision": str(metadata.get("handler_revision") or "unversioned"),
        "effect_class": str(metadata.get("effect_class") or "external_side_effect"),
        "parallel_safe": bool(metadata.get("parallel_safe")),
        "idempotency": str(metadata.get("idempotency") or "none"),
        "touches_desktop": bool(metadata.get("touches_desktop")),
        "may_return_secrets": bool(metadata.get("may_return_secrets")),
        "default_deadline_ms": int(metadata.get("default_deadline_ms") or 0),
        "result_projection": str(metadata.get("result_projection") or "typed-content-v1"),
        "description": str(getattr(tool, "description", "") or "").strip(),
        "when": str(getattr(tool, "when", "") or "").strip(),
        "avoid": str(getattr(tool, "avoid", "") or "").strip(),
        "params": params,
        "signature": binding_signature(alias, params),
    }


def _object_methods_from_tool(tool: Any, *, api_name: str) -> list[dict[str, Any]]:
    methods: list[dict[str, Any]] = []
    method_aliases: set[str] = set()
    for raw_method in getattr(tool, "object_methods", ()):
        if not isinstance(raw_method, dict):
            raise CatalogError(
                f"mounted object {api_name!r} has a non-object method declaration"
            )
        alias = str(raw_method.get("name") or "").replace("-", "_")
        if (
            not _ALIAS_RE.fullmatch(alias)
            or alias in {"documentation", "describe", "methods"}
            or alias in method_aliases
        ):
            raise CatalogError(
                f"invalid or duplicate mounted-object method: {api_name}.{alias}"
            )
        method_aliases.add(alias)
        params = json.loads(json.dumps(raw_method.get("params") or {}, default=str))
        effect_class = str(raw_method.get("effect_class") or "external_side_effect")
        methods.append({
            "alias": alias,
            "operation": str(raw_method.get("operation") or alias),
            "condition": str(raw_method.get("condition") or ""),
            "description": str(raw_method.get("description") or ""),
            "params": params,
            "signature": binding_signature(alias, params),
            "effect_class": effect_class,
            "parallel_safe": bool(raw_method.get(
                "parallel_safe", effect_class in {"pure", "read"}
            )),
            "idempotency": str(
                raw_method.get("idempotency")
                or (
                    "naturally_idempotent"
                    if effect_class in {"pure", "read"}
                    else "caller_key"
                )
            ),
            "may_return_secrets": bool(raw_method.get("may_return_secrets", True)),
        })
    if not methods:
        raise CatalogError(f"mounted object {api_name!r} has no methods")
    return methods


def build_catalog_document(registry: Any) -> dict[str, Any]:
    """Snapshot every direct seed, thin object, and slotless Python API."""
    categories: list[dict[str, Any]] = []
    tool_owners: dict[str, tuple[str, int | str]] = {}
    source_rows: list[dict[str, Any]] = []
    for category_id, title, summary, mount_mode in CATEGORY_DEFINITIONS:
        slots: list[dict[str, Any]] = []
        python_apis: list[dict[str, Any]] = []
        blueprint = SEED_SLOT_BLUEPRINT[category_id]
        if not blueprint and mount_mode != "base":
            raise CatalogError(f"category {category_id!r} has no declared positions")
        for position, slot_spec in enumerate(blueprint, start=1):
            bindings: list[dict[str, Any]] = []
            object_methods: list[dict[str, Any]] = []
            object_summary = ""
            operation_aliases: set[str] = set()
            if slot_spec.bindings:
                if slot_spec.projection not in {"seeds", "object"}:
                    raise CatalogError(
                        f"unsupported catalog projection: {slot_spec.projection!r}"
                    )
                if slot_spec.projection == "object" and len(slot_spec.bindings) != 1:
                    raise CatalogError(
                        "a mounted-object slot must own exactly one broker handler"
                    )
                if not _ALIAS_RE.fullmatch(slot_spec.bundle):
                    raise CatalogError(
                        "catalog slot bundle is not a Python identifier: "
                        f"{slot_spec.bundle!r}"
                    )
            for binding_spec in slot_spec.bindings:
                existing_owner = tool_owners.get(binding_spec.tool_name)
                if existing_owner is not None:
                    raise CatalogError(
                        f"catalog tool {binding_spec.tool_name!r} is declared in both "
                        f"{existing_owner[0]}/{existing_owner[1]} and "
                        f"{category_id}/{position}"
                    )
                tool_owners[binding_spec.tool_name] = (category_id, position)
                declaration = {
                    "category_id": category_id,
                    "position": position,
                    "bundle": slot_spec.bundle,
                    "namespace": binding_spec.namespace,
                    "alias": binding_spec.alias,
                    "tool_name": binding_spec.tool_name,
                    "condition": binding_spec.condition,
                    "projection": slot_spec.projection,
                }
                tool = registry.get(binding_spec.tool_name)
                if tool is None:
                    source_rows.append({**declaration, "available": False})
                    continue
                binding = _binding(
                    tool,
                    alias=binding_spec.alias,
                    namespace=binding_spec.namespace,
                    bundle=slot_spec.bundle,
                    condition=binding_spec.condition,
                    projection=slot_spec.projection,
                )
                alias = binding["alias"]
                if alias in operation_aliases:
                    raise CatalogError(
                        "duplicate operation alias in catalog slot: "
                        f"{slot_spec.bundle}.{alias}"
                    )
                operation_aliases.add(alias)
                bindings.append(binding)
                source_rows.append({
                    **declaration,
                    "available": True,
                    "capability_id": binding["capability_id"],
                    "schema_revision": binding["schema_revision"],
                    "handler_revision": binding["handler_revision"],
                })
            if slot_spec.projection == "object" and bindings:
                object_tool = registry.get(slot_spec.bindings[0].tool_name)
                object_summary = str(
                    getattr(object_tool, "description", "") or ""
                ).strip()
                object_methods = _object_methods_from_tool(
                    object_tool, api_name=slot_spec.bundle
                )
            declared = bool(slot_spec.bindings)
            slots.append({
                "position": position,
                "bundle": slot_spec.bundle,
                "projection": slot_spec.projection,
                "object_summary": object_summary,
                "methods": object_methods,
                "baseline_version": 0,
                "status": (
                    "seed" if bindings else "unavailable" if declared else "vacant"
                ),
                "primary_alias": (
                    str(bindings[0].get("alias") or "")
                    if bindings and slot_spec.projection == "seeds"
                    else slot_spec.bundle
                    if bindings and slot_spec.projection == "object"
                    else None
                ),
                "primary_namespace": (
                    str(bindings[0].get("namespace") or "tools")
                    if bindings and slot_spec.projection == "seeds"
                    else slot_spec.bundle if bindings else None
                ),
                "bindings": bindings,
            })
        api_names: set[str] = set()
        for api_spec in CATEGORY_PYTHON_API_BLUEPRINT.get(category_id, ()):
            if not _ALIAS_RE.fullmatch(api_spec.name):
                raise CatalogError(
                    f"Python API name is not an identifier: {api_spec.name!r}")
            if api_spec.name in api_names:
                raise CatalogError(
                    f"duplicate Python API in {category_id}: {api_spec.name}")
            api_names.add(api_spec.name)
            if len(api_spec.bindings) != 1:
                raise CatalogError(
                    f"Python API {api_spec.name!r} must own exactly one broker handler"
                )
            binding_spec = api_spec.bindings[0]
            existing_owner = tool_owners.get(binding_spec.tool_name)
            if existing_owner is not None:
                raise CatalogError(
                    f"catalog tool {binding_spec.tool_name!r} is declared in both "
                    f"{existing_owner[0]}/{existing_owner[1]} and "
                    f"{category_id}/api:{api_spec.name}"
                )
            tool_owners[binding_spec.tool_name] = (
                category_id, f"api:{api_spec.name}")
            declaration = {
                "surface": "python_api",
                "category_id": category_id,
                "api_name": api_spec.name,
                "source_namespace": binding_spec.namespace,
                "alias": binding_spec.alias,
                "tool_name": binding_spec.tool_name,
                "condition": binding_spec.condition,
            }
            tool = registry.get(binding_spec.tool_name)
            if tool is None:
                source_rows.append({**declaration, "available": False})
                python_apis.append({
                    "name": api_spec.name,
                    "summary": api_spec.summary,
                    "tool_name": binding_spec.tool_name,
                    "status": "unavailable",
                    "methods": [],
                })
                continue
            transport = _binding(
                tool,
                alias=api_spec.name,
                namespace=api_spec.name,
                bundle=api_spec.name,
                condition=binding_spec.condition,
                projection="python_api",
            )
            methods = _object_methods_from_tool(tool, api_name=api_spec.name)
            source_rows.append({
                **declaration,
                "available": True,
                "capability_id": transport["capability_id"],
                "schema_revision": transport["schema_revision"],
                "handler_revision": transport["handler_revision"],
            })
            python_apis.append({
                "name": api_spec.name,
                "summary": api_spec.summary,
                "tool_name": binding_spec.tool_name,
                "transport": transport,
                "status": "available" if methods else "unavailable",
                "methods": methods,
            })
        categories.append({
            "category_id": category_id,
            "title": title,
            "summary": summary,
            "mount_mode": mount_mode,
            "aliases": [category_id, title.casefold()],
            "slots": slots,
            "python_apis": python_apis,
        })
    source_digest = hashlib.sha256(canonical_bytes(source_rows)).hexdigest()
    return {
        "schema": CATALOG_SCHEMA,
        "schema_revision": CATALOG_SCHEMA_REVISION,
        "source_digest": source_digest,
        "category_order": [row[0] for row in CATEGORY_DEFINITIONS],
        "categories": categories,
        "disclosure": {
            "profile": "disclosure.topk.v1",
            "revision": "1",
            "width": 5,
        },
        "mutation": {"availability": "disabled_in_profile"},
    }


def handler_only_catalog_follow_allowed(
    pinned: dict[str, Any], current: dict[str, Any]
) -> bool:
    """Whether a static chat can follow without changing its tool contract."""
    left = copy.deepcopy(pinned)
    right = copy.deepcopy(current)
    left.pop("source_digest", None)
    right.pop("source_digest", None)
    changed = False
    categories = left.get("categories") or []
    peer_categories = right.get("categories") or []
    if len(categories) != len(peer_categories):
        return False
    for category, peer_category in zip(categories, peer_categories):
        slots = category.get("slots") or []
        peer_slots = peer_category.get("slots") or []
        if len(slots) != len(peer_slots):
            return False
        for slot, peer_slot in zip(slots, peer_slots):
            bindings = slot.get("bindings") or []
            peer_bindings = peer_slot.get("bindings") or []
            if len(bindings) != len(peer_bindings):
                return False
            for binding, peer_binding in zip(bindings, peer_bindings):
                if str(binding.get("handler_revision") or "") != str(
                    peer_binding.get("handler_revision") or ""
                ):
                    changed = True
                binding.pop("handler_revision", None)
                peer_binding.pop("handler_revision", None)
        apis = category.get("python_apis") or []
        peer_apis = peer_category.get("python_apis") or []
        if len(apis) != len(peer_apis):
            return False
        for api, peer_api in zip(apis, peer_apis):
            if str(api.get("name") or "") != str(peer_api.get("name") or ""):
                return False
            transport = api.get("transport") or {}
            peer_transport = peer_api.get("transport") or {}
            if str(transport.get("handler_revision") or "") != str(
                peer_transport.get("handler_revision") or ""
            ):
                changed = True
            transport.pop("handler_revision", None)
            peer_transport.pop("handler_revision", None)
            methods = api.get("methods") or []
            peer_methods = peer_api.get("methods") or []
            if len(methods) != len(peer_methods):
                return False
            for method, peer_method in zip(methods, peer_methods):
                if str(method.get("handler_revision") or "") != str(
                    peer_method.get("handler_revision") or ""
                ):
                    changed = True
                method.pop("handler_revision", None)
                peer_method.pop("handler_revision", None)
    return changed and canonical_bytes(left) == canonical_bytes(right)


def catalog_follow_authority_allowed(
    runtime: Any,
    *,
    has_mutation_artifacts: bool = False,
) -> bool:
    """Whether mutable session state permits automatic catalog following."""

    try:
        row = dict(runtime or {})
    except (TypeError, ValueError):
        row = {}
    return bool(
        str(row.get("action_surface") or "") == ACTION_SURFACE
        and not bool(row.get("mutation_write_enabled"))
        and not bool(has_mutation_artifacts)
    )


class CatalogRepository:
    """SQLite pointers and CAS state around immutable catalog artifacts."""

    def __init__(self, path: str, artifact_store: Any) -> None:
        self.path = os.path.abspath(path)
        self.artifact_store = artifact_store
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return sqlite_session_connection(self.path)

    def _initialize(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS session_catalog_release (
                        release_id TEXT PRIMARY KEY,
                        schema_revision TEXT NOT NULL,
                        content_sha256 TEXT NOT NULL UNIQUE,
                        artifact_ref TEXT NOT NULL,
                        document_json TEXT NOT NULL,
                        source_digest TEXT NOT NULL,
                        status TEXT NOT NULL,
                        last_known_good INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        quarantined_at REAL,
                        quarantine_reason TEXT NOT NULL DEFAULT ''
                    );

                    CREATE TABLE IF NOT EXISTS session_catalog_slot (
                        release_id TEXT NOT NULL,
                        category_id TEXT NOT NULL,
                        position INTEGER NOT NULL,
                        baseline_version INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        alias TEXT NOT NULL,
                        capability_id TEXT NOT NULL,
                        schema_revision TEXT NOT NULL,
                        handler_revision TEXT NOT NULL,
                        binding_json TEXT NOT NULL,
                        PRIMARY KEY(release_id, category_id, position, alias),
                        FOREIGN KEY(release_id) REFERENCES session_catalog_release(release_id)
                    );

                    CREATE TABLE IF NOT EXISTS session_catalog_pointer (
                        name TEXT PRIMARY KEY,
                        release_id TEXT NOT NULL,
                        updated_at REAL NOT NULL,
                        FOREIGN KEY(release_id) REFERENCES session_catalog_release(release_id)
                    );

                    CREATE TABLE IF NOT EXISTS astb_mount_history (
                        chat_id TEXT NOT NULL,
                        mount_revision INTEGER NOT NULL,
                        catalog_release_id TEXT NOT NULL,
                        category_id TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        PRIMARY KEY(chat_id, mount_revision)
                    );

                    CREATE TABLE IF NOT EXISTS session_catalog_schema (
                        version INTEGER PRIMARY KEY,
                        applied_at REAL NOT NULL
                    );
                    """
                )
                conn.execute(
                    "INSERT OR IGNORE INTO session_catalog_schema(version, applied_at) VALUES (?, ?)",
                    (CATALOG_REPOSITORY_SCHEMA, time.time()),
                )
            finally:
                conn.close()

    def publish(self, document: dict[str, Any], *, make_current: bool = True) -> str:
        if str(document.get("schema") or "") != CATALOG_SCHEMA:
            raise CatalogError("catalog source schema is unsupported")
        raw = canonical_bytes(document)
        digest = hashlib.sha256(raw).hexdigest()
        release_id = f"astb.catalog.{digest[:24]}.v0"
        artifact = self.artifact_store.put_bytes(
            raw,
            media_type="application/json",
            kind="session_catalog_source",
            scope="astb.catalog",
        )
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT * FROM session_catalog_release WHERE release_id=?",
                    (release_id,),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["content_sha256"] != digest
                        or existing["artifact_ref"] != artifact.ref
                        or existing["document_json"] != raw.decode("utf-8")
                    ):
                        raise CatalogError("immutable catalog release collision")
                else:
                    conn.execute(
                        """
                        INSERT INTO session_catalog_release(
                            release_id, schema_revision, content_sha256,
                            artifact_ref, document_json, source_digest, status,
                            last_known_good, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
                        """,
                        (
                            release_id,
                            str(document.get("schema_revision") or "1"),
                            digest,
                            artifact.ref,
                            raw.decode("utf-8"),
                            str(document.get("source_digest") or ""),
                            1 if make_current else 0,
                            now,
                        ),
                    )
                    for category in document.get("categories") or ():
                        category_id = str(category.get("category_id") or "")
                        for slot in category.get("slots") or ():
                            bindings = list(slot.get("bindings") or ())
                            if not bindings:
                                bindings = [{
                                    "alias": "",
                                    "capability_id": "",
                                    "schema_revision": "",
                                    "handler_revision": "",
                                }]
                            for binding in bindings:
                                conn.execute(
                                    """
                                    INSERT INTO session_catalog_slot(
                                        release_id, category_id, position,
                                        baseline_version, status, alias,
                                        capability_id, schema_revision,
                                        handler_revision, binding_json
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                    """,
                                    (
                                        release_id,
                                        category_id,
                                        int(slot.get("position") or 0),
                                        int(slot.get("baseline_version") or 0),
                                        str(slot.get("status") or "vacant"),
                                        str(binding.get("alias") or ""),
                                        str(binding.get("capability_id") or ""),
                                        str(binding.get("schema_revision") or ""),
                                        str(binding.get("handler_revision") or ""),
                                        canonical_bytes(binding).decode("utf-8"),
                                    ),
                                )
                if make_current:
                    conn.execute(
                        "UPDATE session_catalog_release SET status='active', "
                        "last_known_good=1, quarantined_at=NULL, "
                        "quarantine_reason='' WHERE release_id=?",
                        (release_id,),
                    )
                    conn.execute(
                        """
                        INSERT INTO session_catalog_pointer(name, release_id, updated_at)
                        VALUES ('current', ?, ?)
                        ON CONFLICT(name) DO UPDATE SET
                            release_id=excluded.release_id,
                            updated_at=excluded.updated_at
                        """,
                        (release_id, now),
                    )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
        return release_id

    def _row(self, release_id: str) -> sqlite3.Row | None:
        with self._lock:
            conn = self._connect()
            try:
                return conn.execute(
                    "SELECT * FROM session_catalog_release WHERE release_id=?",
                    (str(release_id or ""),),
                ).fetchone()
            finally:
                conn.close()

    def _quarantine(self, release_id: str, reason: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE session_catalog_release SET status='quarantined', "
                    "last_known_good=0, quarantined_at=?, quarantine_reason=? "
                    "WHERE release_id=?",
                    (time.time(), str(reason or "")[:500], release_id),
                )
            finally:
                conn.close()

    def load(self, release_id: str, *, _fallback_from: str = "") -> LoadedCatalog:
        row = self._row(release_id)
        if row is None:
            raise CatalogError(f"unknown catalog release: {release_id}")
        try:
            raw = self.artifact_store.read_bytes(row["artifact_ref"])
            digest = hashlib.sha256(raw).hexdigest()
            if digest != row["content_sha256"]:
                raise CatalogCorruption("catalog source digest mismatch")
            document = json.loads(raw.decode("utf-8"))
            if (
                not isinstance(document, dict)
                or document.get("schema") != CATALOG_SCHEMA
                or canonical_bytes(document) != raw
            ):
                raise CatalogCorruption("catalog source is not canonical")
            if row["status"] == "quarantined":
                raise CatalogCorruption("catalog release is quarantined")
            return LoadedCatalog(
                release_id=row["release_id"],
                content_sha256=digest,
                artifact_ref=row["artifact_ref"],
                document=document,
                fallback_from=_fallback_from,
            )
        except Exception as exc:
            self._quarantine(str(release_id), str(exc))
            with self._lock:
                conn = self._connect()
                try:
                    fallback = conn.execute(
                        """
                        SELECT release_id FROM session_catalog_release
                        WHERE last_known_good=1 AND status='active'
                          AND release_id != ?
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (str(release_id),),
                    ).fetchone()
                finally:
                    conn.close()
            if fallback is None or _fallback_from:
                raise CatalogCorruption(
                    f"catalog release {release_id!r} is corrupt and no LKG is available: {exc}"
                ) from exc
            return self.load(
                str(fallback["release_id"]),
                _fallback_from=str(release_id),
            )

    def current(self) -> LoadedCatalog:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT release_id FROM session_catalog_pointer WHERE name='current'"
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            raise CatalogError("current catalog pointer is absent")
        return self.load(str(row["release_id"]))

    def select_mount(
        self,
        chat_id: str,
        *,
        catalog_release_id: str,
        category_id: str,
        expected_mount_revision: int | None = None,
        reason: str = "model_select",
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM astb_chat_runtime WHERE chat_id=?",
                    (str(chat_id),),
                ).fetchone()
                if row is None or row["lifecycle_state"] != "active":
                    raise CatalogError(f"chat runtime is not active: {chat_id}")
                if row["catalog_release_id"] != catalog_release_id:
                    raise MountConflict("chat catalog release does not match selection")
                current = int(row["mount_revision"] or 0)
                if (
                    expected_mount_revision is not None
                    and current != int(expected_mount_revision)
                ):
                    raise MountConflict(
                        f"mount CAS failed ({current} != {expected_mount_revision})"
                    )
                if str(row["selected_category_id"] or "") == str(category_id):
                    # Provider calls may repeat the current category on every
                    # IPython turn.  With no category transition there is no new
                    # namespace identity to publish, so advancing the revision
                    # would only invalidate perfectly current persistent-kernel
                    # proxies.
                    conn.commit()
                    return {
                        "chat_id": str(chat_id),
                        "catalog_release_id": catalog_release_id,
                        "category_id": category_id,
                        "mount_revision": current,
                        "unchanged": True,
                    }
                next_revision = current + 1
                conn.execute(
                    """
                    UPDATE astb_chat_runtime
                    SET selected_category_id=?, mount_revision=?,
                        updated_at=?, version=version+1
                    WHERE chat_id=?
                    """,
                    (category_id, next_revision, now, str(chat_id)),
                )
                conn.execute(
                    """
                    INSERT INTO astb_mount_history(
                        chat_id, mount_revision, catalog_release_id,
                        category_id, reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(chat_id),
                        next_revision,
                        catalog_release_id,
                        category_id,
                        str(reason or "model_select"),
                        now,
                    ),
                )
                conn.commit()
                return {
                    "chat_id": str(chat_id),
                    "catalog_release_id": catalog_release_id,
                    "category_id": category_id,
                    "mount_revision": next_revision,
                    "unchanged": False,
                }
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    def retained_categories(self, chat_id: str, catalog_release_id: str) -> list[str]:
        """Authority since the last reset/release boundary, independent of UI pagination."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT category_id, MIN(mount_revision) AS first_mount
                    FROM astb_mount_history
                    WHERE chat_id=? AND catalog_release_id=?
                      AND COALESCE(category_id, '')<>''
                      AND mount_revision > COALESCE((
                        SELECT MAX(mount_revision) FROM astb_mount_history
                        WHERE chat_id=? AND (
                            catalog_release_id<>? OR COALESCE(category_id, '')=''
                        )
                      ), -1)
                    GROUP BY category_id ORDER BY first_mount
                    """,
                    (str(chat_id), str(catalog_release_id), str(chat_id), str(catalog_release_id)),
                ).fetchall()
            finally:
                conn.close()
        return [str(row["category_id"]) for row in rows]

    def history(self, chat_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        cap = max(1, min(int(limit), 200))
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT mount_revision, catalog_release_id, category_id,
                           reason, created_at
                    FROM astb_mount_history WHERE chat_id=?
                    ORDER BY mount_revision DESC LIMIT ?
                    """,
                    (str(chat_id), cap),
                ).fetchall()
            finally:
                conn.close()
        return [dict(row) for row in reversed(rows)]

    def reset_mount(
        self,
        chat_id: str,
        *,
        expected_mount_revision: int | None = None,
        reason: str = "explicit_reset",
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM astb_chat_runtime WHERE chat_id=?",
                    (str(chat_id),),
                ).fetchone()
                if row is None or row["lifecycle_state"] != "active":
                    raise CatalogError(f"chat runtime is not active: {chat_id}")
                current = int(row["mount_revision"] or 0)
                if (
                    expected_mount_revision is not None
                    and current != int(expected_mount_revision)
                ):
                    raise MountConflict(
                        f"mount reset CAS failed ({current} != {expected_mount_revision})"
                    )
                next_revision = current + 1
                release_id = str(row["catalog_release_id"])
                conn.execute(
                    "UPDATE astb_chat_runtime SET selected_category_id='', "
                    "mount_revision=?, overlay_revision=0, updated_at=?, version=version+1 "
                    "WHERE chat_id=?",
                    (next_revision, now, str(chat_id)),
                )
                conn.execute(
                    "INSERT INTO astb_mount_history(chat_id, mount_revision, "
                    "catalog_release_id, category_id, reason, created_at) "
                    "VALUES (?, ?, ?, '', ?, ?)",
                    (str(chat_id), next_revision, release_id, str(reason), now),
                )
                conn.commit()
                return {
                    "chat_id": str(chat_id),
                    "catalog_release_id": release_id,
                    "category_id": "",
                    "mount_revision": next_revision,
                }
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    def recover_chat_catalog(
        self,
        chat_id: str,
        *,
        expected_release_id: str,
        fallback_release_id: str,
    ) -> dict[str, Any]:
        """CAS a corrupt pinned release to a verified LKG and revoke mounts."""
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM astb_chat_runtime WHERE chat_id=?",
                    (str(chat_id),),
                ).fetchone()
                if row is None or row["lifecycle_state"] != "active":
                    raise CatalogError(f"chat runtime is not active: {chat_id}")
                if row["catalog_release_id"] != expected_release_id:
                    raise MountConflict("catalog recovery CAS failed")
                next_revision = int(row["mount_revision"] or 0) + 1
                conn.execute(
                    """
                    UPDATE astb_chat_runtime
                    SET catalog_release_id=?, selected_category_id='',
                        mount_revision=?, overlay_revision=0,
                        updated_at=?, version=version+1
                    WHERE chat_id=? AND catalog_release_id=?
                    """,
                    (
                        fallback_release_id,
                        next_revision,
                        now,
                        str(chat_id),
                        expected_release_id,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO astb_mount_history(
                        chat_id, mount_revision, catalog_release_id,
                        category_id, reason, created_at
                    ) VALUES (?, ?, ?, '', 'catalog_lkg_recovery', ?)
                    """,
                    (str(chat_id), next_revision, fallback_release_id, now),
                )
                conn.commit()
                return {
                    "chat_id": str(chat_id),
                    "catalog_release_id": fallback_release_id,
                    "mount_revision": next_revision,
                    "recovered_from": expected_release_id,
                }
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    def follow_handler_only_release(
        self,
        chat_id: str,
        *,
        expected_release_id: str,
        target_release_id: str,
    ) -> dict[str, Any]:
        """CAS a static chat to a handler-only-compatible catalog release."""
        pinned = self.load(expected_release_id)
        target = self.load(target_release_id)
        if pinned.fallback_from or target.fallback_from:
            raise CatalogError("catalog follow requires two verified releases")
        if not handler_only_catalog_follow_allowed(pinned.document, target.document):
            raise CatalogError("catalog releases are not handler-only compatible")
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM astb_chat_runtime WHERE chat_id=?",
                    (str(chat_id),),
                ).fetchone()
                if row is None or row["lifecycle_state"] != "active":
                    raise CatalogError(f"chat runtime is not active: {chat_id}")
                if row["catalog_release_id"] != expected_release_id:
                    raise MountConflict("catalog follow CAS failed")
                has_mutation_artifacts = self._has_mutation_artifacts_in_conn(
                    conn, str(chat_id)
                )
                if not catalog_follow_authority_allowed(
                    row,
                    has_mutation_artifacts=has_mutation_artifacts,
                ):
                    raise CatalogError(
                        "automatic catalog follow requires mutation off with no "
                        "mutation authority or artifacts"
                    )
                next_revision = int(row["mount_revision"] or 0) + 1
                category_id = str(row["selected_category_id"] or "")
                conn.execute(
                    "UPDATE astb_chat_runtime SET catalog_release_id=?, "
                    "mount_revision=?, updated_at=?, version=version+1 "
                    "WHERE chat_id=? AND catalog_release_id=?",
                    (
                        target_release_id, next_revision, now, str(chat_id),
                        expected_release_id,
                    ),
                )
                conn.execute(
                    "INSERT INTO astb_mount_history(chat_id, mount_revision, "
                    "catalog_release_id, category_id, reason, created_at) "
                    "VALUES (?, ?, ?, ?, 'catalog_handler_follow', ?)",
                    (
                        str(chat_id), next_revision, target_release_id,
                        category_id, now,
                    ),
                )
                conn.commit()
                return {
                    "chat_id": str(chat_id),
                    "catalog_release_id": target_release_id,
                    "mount_revision": next_revision,
                    "followed_from": expected_release_id,
                    "category_id": category_id,
                }
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    def structural_rebase(
        self,
        chat_id: str,
        *,
        expected_release_id: str,
        target_release_id: str,
        expected_runtime_version: int | None = None,
        environment_digest: str = "",
        reason: str = "explicit_structural_rebase",
    ) -> dict[str, Any]:
        """CAS one idle runtime to a structurally different verified release.

        Structural changes invalidate selected mounts and every mutation whose
        base slot belonged to the old release. Mutation authority itself is
        preserved, so ON/OFF behavior does not change after the rebase.
        """

        target = self.load(target_release_id)
        if target.fallback_from:
            raise CatalogError("structural rebase target must be a verified release")
        now = time.time()
        chat = str(chat_id)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM astb_chat_runtime WHERE chat_id=?", (chat,)
                ).fetchone()
                if row is None or row["lifecycle_state"] != "active":
                    raise CatalogError(f"chat runtime is not active: {chat_id}")
                if str(row["catalog_release_id"]) != str(expected_release_id):
                    raise MountConflict("structural catalog rebase release CAS failed")
                if (
                    expected_runtime_version is not None
                    and int(row["version"]) != int(expected_runtime_version)
                ):
                    raise MountConflict("structural catalog rebase runtime CAS failed")
                if str(expected_release_id) == str(target_release_id):
                    conn.rollback()
                    return {
                        "chat_id": chat,
                        "catalog_release_id": str(target_release_id),
                        "mount_revision": int(row["mount_revision"] or 0),
                        "rebased_from": str(expected_release_id),
                        "changed": False,
                    }
                existing_tables = {
                    str(item[0]) for item in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                for table in (
                    "mutation_invocation",
                    "mutation_failure",
                    "mutation_receipt",
                    "mutation_probation",
                    "astb_activation",
                    "astb_slot_version",
                    "mutation_draft",
                ):
                    if table in existing_tables:
                        conn.execute(f"DELETE FROM {table} WHERE chat_id=?", (chat,))
                next_revision = int(row["mount_revision"] or 0) + 1
                changed = conn.execute(
                    "UPDATE astb_chat_runtime SET catalog_release_id=?,"
                    "selected_category_id='',mount_revision=?,overlay_revision=0,"
                    "discovery_state_ref='',environment_digest=CASE WHEN ?<>'' "
                    "THEN ? ELSE environment_digest END,"
                    "kernel_generation=kernel_generation+1,updated_at=?,"
                    "version=version+1 "
                    "WHERE chat_id=? AND catalog_release_id=? AND version=?",
                    (
                        str(target_release_id), next_revision,
                        str(environment_digest), str(environment_digest), now, chat,
                        str(expected_release_id), int(row["version"]),
                    ),
                )
                if changed.rowcount != 1:
                    raise MountConflict("structural catalog rebase CAS failed")
                conn.execute(
                    "INSERT INTO astb_mount_history(chat_id,mount_revision,"
                    "catalog_release_id,category_id,reason,created_at) "
                    "VALUES (?,?,?,'',?,?)",
                    (chat, next_revision, str(target_release_id), str(reason), now),
                )
                updated = conn.execute(
                    "SELECT kernel_generation FROM astb_chat_runtime WHERE chat_id=?",
                    (chat,),
                ).fetchone()
                conn.commit()
                return {
                    "chat_id": chat,
                    "catalog_release_id": str(target_release_id),
                    "mount_revision": next_revision,
                    "rebased_from": str(expected_release_id),
                    "changed": True,
                    "mutation_state_discarded": True,
                    "kernel_generation": int(updated["kernel_generation"] or 0),
                }
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def structurally_stale_chats(self, target_release_id: str) -> list[str]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT chat_id FROM astb_chat_runtime WHERE lifecycle_state='active' "
                    "AND catalog_release_id<>? ORDER BY chat_id",
                    (str(target_release_id),),
                ).fetchall()
            finally:
                conn.close()
        return [str(row["chat_id"]) for row in rows]

    @staticmethod
    def _has_mutation_artifacts_in_conn(
        conn: sqlite3.Connection,
        chat_id: str,
    ) -> bool:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='mutation_draft'"
        ).fetchone()
        if table is None:
            return False
        return conn.execute(
            "SELECT 1 FROM mutation_draft WHERE chat_id=? LIMIT 1",
            (str(chat_id),),
        ).fetchone() is not None

    def catalog_follow_eligible(self, chat_id: str) -> bool:
        """Read-only preflight used to avoid noisy rejected follow attempts."""

        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM astb_chat_runtime WHERE chat_id=?",
                    (str(chat_id),),
                ).fetchone()
                if row is None:
                    return False
                return catalog_follow_authority_allowed(
                    row,
                    has_mutation_artifacts=self._has_mutation_artifacts_in_conn(
                        conn, str(chat_id)
                    ),
                )
            finally:
                conn.close()

    def delete_chat_state(self, chat_id: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "DELETE FROM astb_mount_history WHERE chat_id=?",
                    (str(chat_id),),
                )
            finally:
                conn.close()


def category_ids(document: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(row.get("category_id") or "")
        for row in (document.get("categories") or ())
        if str(row.get("category_id") or "")
    )


def iter_bindings(document: dict[str, Any]) -> Iterable[tuple[str, int, dict[str, Any]]]:
    for category in document.get("categories") or ():
        category_id = str(category.get("category_id") or "")
        for slot in category.get("slots") or ():
            position = int(slot.get("position") or 0)
            for binding in slot.get("bindings") or ():
                if isinstance(binding, dict):
                    yield category_id, position, binding
