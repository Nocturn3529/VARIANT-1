"""Session-local mutation lifecycle with same-user worker execution."""

from __future__ import annotations

import ast
from dataclasses import replace
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable, Mapping, Sequence

from capability_broker import (
    CapabilityRef,
    InvocationContext,
    current_capability_invocation,
)
from core_invariants import sqlite_session_connection
from observability.operational_log import emit as operational_log
from tool_core import json_safe
from tools import ToolError, _normalize_schema_value

from .catalog import (
    LoadedCatalog,
    MountConflict,
    binding_signature,
    canonical_bytes,
)
from .mutation_contracts import (
    MUTATION_REMOTE_HANDLE_PROXY,
    MUTATION_REMOTE_HANDLE_ROLE,
    MutationAuthorityLease,
    MutationError,
    MutationWorkerError,
    stable_digest as _digest,
    stable_json as _stable,
    utc_timestamp as _now,
)
from .mutation_worker_client import MutationWorkerClient


MUTATION_SCHEMA = "variant1.astb.session-mutation.v2"
MUTATION_HANDLER = "mutation_invoke"
_ALIAS = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_EFFECT_RANK = {
    "pure": 0,
    "read": 1,
    "write": 2,
    "external_side_effect": 3,
    "interactive": 4,
}
MAX_FAILED_ACTIVATIONS = 8
IDENTICAL_FAILURE_BREAKER = 3
MAX_PROBATION_CALLS = 6


def _effective_workspace_roots(explicit: Sequence[str] = ()) -> list[str]:
    roots = [
        os.path.abspath(str(path))
        for path in explicit
        if str(path or "").strip()
    ]
    if roots:
        return roots
    context = current_capability_invocation()
    return [
        os.path.abspath(str(path))
        for path in (
            getattr(context, "workspace_root_ids", ()) if context else ()
        )
        if str(path or "").strip()
    ]


def _public_schema(schema: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = json.loads(json.dumps(schema, ensure_ascii=False))
    if not isinstance(raw, dict) or raw.get("type") != "object":
        raise MutationError(
            "invalid_schema", "mutation schema must be a JSON object schema"
        )
    properties = raw.get("properties")
    if not isinstance(properties, dict):
        raise MutationError(
            "invalid_schema", "mutation schema properties must be an object"
        )
    if len(properties) > 32:
        raise MutationError("schema_quota", "mutation schema exceeds 32 properties")
    required = {str(item) for item in raw.get("required") or ()}
    if not required <= set(properties):
        raise MutationError("invalid_schema", "schema required names must exist")
    supported = {
        "any", "string", "integer", "number", "boolean", "array", "object",
    }
    params: dict[str, Any] = {}
    for name, value in properties.items():
        clean = str(name)
        if not _ALIAS.fullmatch(clean):
            raise MutationError("invalid_schema", f"invalid parameter name: {clean!r}")
        spec = dict(value) if isinstance(value, dict) else {}
        if str(spec.get("type") or "") not in supported:
            raise MutationError(
                "invalid_schema", f"unsupported type for {clean!r}: {spec.get('type')!r}"
            )
        params[clean] = {**spec, "required": clean in required}
    raw["additionalProperties"] = False
    raw["required"] = sorted(required)
    raw["properties"] = properties
    if len(canonical_bytes(raw)) > 16 * 1024:
        raise MutationError("schema_quota", "mutation schema exceeds 16384 bytes")
    return raw, params


def _atomic_schema_from_params(params: Any) -> dict[str, Any]:
    """Project one existing direct seed contract into mutation JSON Schema."""

    type_names = {
        "str": "string", "int": "integer", "float": "number",
        "bool": "boolean", "dict": "object", "list": "array",
    }

    def normalize(raw: Any) -> dict[str, Any]:
        spec = dict(raw) if isinstance(raw, dict) else {}
        typ = str(spec.get("type") or "string")
        spec["type"] = type_names.get(typ, typ)
        spec.pop("required", None)
        if isinstance(spec.get("items"), dict):
            spec["items"] = normalize(spec["items"])
        if isinstance(spec.get("properties"), dict):
            spec["properties"] = {
                str(name): normalize(child)
                for name, child in spec["properties"].items()
            }
        return spec

    rows = dict(params or {})
    required = sorted(
        str(name) for name, spec in rows.items()
        if isinstance(spec, dict) and bool(spec.get("required"))
    )
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": {
            str(name): normalize(spec) for name, spec in rows.items()
        },
    }


def _slot(loaded: LoadedCatalog, reference: str) -> tuple[str, int, dict[str, Any]]:
    raw = str(reference or "").strip()
    prefix = loaded.release_id + "/"
    if raw.startswith(prefix):
        raw = raw[len(prefix):]
    match = re.fullmatch(r"([a-z][a-z0-9_]*)[/.:]([1-9][0-9]*)", raw.casefold())
    if match:
        category_id, position_text = match.groups()
        position = int(position_text)
        for category in loaded.document.get("categories") or ():
            if category.get("category_id") != category_id:
                continue
            for candidate in category.get("slots") or ():
                if int(candidate.get("position") or 0) == position:
                    return category_id, position, dict(candidate)
    matches: list[tuple[str, int, dict[str, Any]]] = []
    for category in loaded.document.get("categories") or ():
        for candidate in category.get("slots") or ():
            aliases = {
                str(binding.get("alias") or "").casefold()
                for binding in candidate.get("bindings") or ()
            }
            aliases.update({
                str(candidate.get("bundle") or "").casefold(),
            })
            if raw.casefold() in aliases:
                matches.append((
                    str(category.get("category_id") or ""),
                    int(candidate.get("position") or 0),
                    dict(candidate),
                ))
    if len(matches) == 1:
        return matches[0]
    raise MutationError("invalid_slot", f"unknown or ambiguous mutation slot: {reference!r}")


def _selected_slot_reference(record: Any, reference: Any) -> str:
    """Resolve a bare numeric position against the chat's selected category."""

    raw = str(reference or "").strip()
    if not re.fullmatch(r"[1-9][0-9]*", raw):
        return raw
    selected = str(
        getattr(getattr(record, "identity", None), "selected_category_id", "")
        or ""
    ).strip()
    if not selected:
        raise MutationError(
            "invalid_slot",
            f"numeric mutation slot {raw!r} requires a selected category",
        )
    return f"{selected}/{int(raw)}"


class MutationManager:
    """SQLite-authoritative draft/version/activation lifecycle."""

    def __init__(
        self,
        database_path: str,
        *,
        artifact_store: Any,
        catalog_repository: Any,
        runtime_registry: Any,
        broker: Any,
        registry: Any,
        enabled_resolver: Callable[[], set[str]],
        worker_root: str,
        mutation_allowed: Callable[[], bool] | None = None,
    ) -> None:
        self.path = os.path.abspath(database_path)
        self.artifact_store = artifact_store
        self.catalog_repository = catalog_repository
        self.runtime_registry = runtime_registry
        self.broker = broker
        self.registry = registry
        self.enabled_resolver = enabled_resolver
        self.mutation_allowed = mutation_allowed or (lambda: True)
        self.worker = MutationWorkerClient(worker_root)
        self._lock = threading.RLock()
        self._initialize()

    def set_worker_executable(self, path: str) -> None:
        self.worker.set_worker_executable(path)

    def configure_worker(
        self,
        *,
        worker_executable: str = "",
    ) -> None:
        """Configure the host-owned same-user mutation worker."""
        self.worker.set_worker_executable(worker_executable)

    def _connect(self) -> sqlite3.Connection:
        return sqlite_session_connection(self.path)

    def _initialize(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS mutation_draft (
                    draft_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    catalog_release_id TEXT NOT NULL,
                    category_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    slot_id TEXT NOT NULL,
                    declared_kind TEXT NOT NULL,
                    parent_slot_id TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    schema_json TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    proposal_fingerprint TEXT NOT NULL DEFAULT '',
                    dependencies_json TEXT NOT NULL,
                    tests_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    validation_json TEXT NOT NULL DEFAULT '',
                    host_test_json TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS mutation_draft_chat_idx
                ON mutation_draft(chat_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS astb_slot_version (
                    chat_id TEXT NOT NULL,
                    slot_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    draft_id TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    previous_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(chat_id, slot_id, version),
                    UNIQUE(chat_id, slot_id, draft_id)
                );
                CREATE TABLE IF NOT EXISTS astb_activation (
                    activation_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    slot_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    draft_id TEXT NOT NULL,
                    active INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    mount_revision INTEGER NOT NULL,
                    receipt_digest TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS astb_activation_one_active
                ON astb_activation(chat_id, slot_id) WHERE active=1;
                CREATE TABLE IF NOT EXISTS mutation_probation (
                    chat_id TEXT NOT NULL,
                    slot_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    draft_id TEXT NOT NULL,
                    previous_version INTEGER NOT NULL,
                    lkg_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    calls INTEGER NOT NULL,
                    successful_calls INTEGER NOT NULL,
                    semantic_errors INTEGER NOT NULL,
                    mechanical_errors INTEGER NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(chat_id, slot_id)
                );
                CREATE TABLE IF NOT EXISTS mutation_receipt (
                    receipt_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    draft_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    receipt_digest TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mutation_invocation (
                    invocation_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    outer_tool_call_id TEXT NOT NULL,
                    cell_execution_id TEXT NOT NULL,
                    nested_call_id TEXT NOT NULL,
                    kernel_generation TEXT NOT NULL,
                    slot_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    draft_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    observed_calls_json TEXT NOT NULL,
                    result_sha256 TEXT NOT NULL,
                    duration_ms REAL NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mutation_failure (
                    failure_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    draft_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    proposal_fingerprint TEXT NOT NULL,
                    code TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS mutation_failure_chat_idx
                ON mutation_failure(chat_id, stage, created_at DESC);
                """
            )

            columns = {
                str(row[1]) for row in conn.execute(
                    "PRAGMA table_info(mutation_draft)"
                ).fetchall()
            }
            if "proposal_fingerprint" not in columns:
                conn.execute(
                    "ALTER TABLE mutation_draft ADD COLUMN "
                    "proposal_fingerprint TEXT NOT NULL DEFAULT ''"
                )
            if "dependencies_json" not in columns and "capabilities_json" in columns:
                conn.execute(
                    "ALTER TABLE mutation_draft RENAME COLUMN "
                    "capabilities_json TO dependencies_json"
                )
                columns.discard("capabilities_json")
                columns.add("dependencies_json")
            if "effects_json" in columns:
                conn.execute("ALTER TABLE mutation_draft DROP COLUMN effects_json")
            invocation_columns = {
                str(row[1]) for row in conn.execute(
                    "PRAGMA table_info(mutation_invocation)"
                ).fetchall()
            }
            for name in (
                "run_id",
                "outer_tool_call_id",
                "cell_execution_id",
                "nested_call_id",
                "kernel_generation",
            ):
                if name not in invocation_columns:
                    conn.execute(
                        f"ALTER TABLE mutation_invocation ADD COLUMN {name} "
                        "TEXT NOT NULL DEFAULT ''"
                    )
            self._recover_state_in_conn(conn)

    def _recover_state_in_conn(self, conn: sqlite3.Connection) -> None:
        """Repair only incomplete derived rows; committed activation CAS is authoritative."""
        conn.execute(
            "UPDATE astb_activation SET active=0 WHERE active=1 AND NOT EXISTS ("
            "SELECT 1 FROM astb_slot_version v WHERE v.chat_id=astb_activation.chat_id "
            "AND v.slot_id=astb_activation.slot_id AND v.version=astb_activation.version "
            "AND v.draft_id=astb_activation.draft_id)"
        )
        conn.execute(
            "DELETE FROM mutation_probation WHERE NOT EXISTS ("
            "SELECT 1 FROM astb_activation a WHERE a.chat_id=mutation_probation.chat_id "
            "AND a.slot_id=mutation_probation.slot_id "
            "AND a.version=mutation_probation.version AND a.active=1)"
        )

    @staticmethod
    def _record_authority(record: Any) -> MutationAuthorityLease:
        """Read the durable per-chat authority; profiles are not permission."""

        return MutationAuthorityLease(
            write_enabled=bool(record.mutation_write_enabled),
            revision=int(record.mutation_authority_revision or 0),
        )

    def authority_status(self, chat_id: str) -> dict[str, Any]:
        record = self.runtime_registry.ensure_runtime(str(chat_id))
        lease = self._record_authority(record)
        operator_allowed = bool(self.mutation_allowed())
        return {
            "write_enabled": lease.write_enabled,
            "authority_revision": lease.revision,
            "operator_allowed": operator_allowed,
            "effective_write_enabled": lease.write_enabled and operator_allowed,
        }

    def _require_write_authority(
        self,
        chat_id: str,
        *,
        expected_revision: int | None = None,
    ) -> tuple[Any, LoadedCatalog, MutationAuthorityLease]:
        record = self.runtime_registry.ensure_runtime(str(chat_id))
        lease = self._record_authority(record)
        if expected_revision is not None and lease.revision != int(expected_revision):
            raise MutationError(
                "mutation_authority_changed",
                "session mutation authority changed while the operation was running",
                expected_revision=int(expected_revision),
                authority_revision=lease.revision,
            )
        if not lease.write_enabled:
            raise MutationError(
                "mutation_write_disabled",
                "session mutation write authority is off for this chat",
                authority_revision=lease.revision,
            )
        if not self.mutation_allowed():
            raise MutationError(
                "mutation_frozen",
                "session mutation is frozen by the host",
                authority_revision=lease.revision,
            )
        loaded = self.catalog_repository.load(record.identity.catalog_release_id)
        return record, loaded, lease

    def _require_recovery_authority(
        self,
        chat_id: str,
        *,
        expected_revision: int | None = None,
    ) -> tuple[Any, LoadedCatalog, MutationAuthorityLease]:
        """Fence rollback/reset without requiring authoring to be enabled."""

        record = self.runtime_registry.ensure_runtime(str(chat_id))
        lease = self._record_authority(record)
        if expected_revision is not None and lease.revision != int(expected_revision):
            raise MutationError(
                "mutation_authority_changed",
                "session mutation authority changed while recovery was running",
                expected_revision=int(expected_revision),
                authority_revision=lease.revision,
            )
        loaded = self.catalog_repository.load(record.identity.catalog_release_id)
        return record, loaded, lease

    def _assert_write_authority_in_conn(
        self,
        conn: sqlite3.Connection,
        chat_id: str,
        *,
        expected_revision: int,
    ) -> MutationAuthorityLease:
        """Fence an authoring commit in the same SQLite transaction.

        Mutation state and durable chat authority share the session database, so
        this read is atomic with the draft/activation write.
        """

        row = conn.execute(
            "SELECT mutation_write_enabled, mutation_authority_revision "
            "FROM astb_chat_runtime WHERE chat_id=?",
            (str(chat_id),),
        ).fetchone()
        if row is None:
            raise MutationError("runtime_unavailable", "chat runtime is unavailable")
        lease = MutationAuthorityLease(
            write_enabled=bool(row["mutation_write_enabled"]),
            revision=int(row["mutation_authority_revision"] or 0),
        )
        if lease.revision != int(expected_revision):
            raise MutationError(
                "mutation_authority_changed",
                "session mutation authority changed while the operation was running",
                expected_revision=int(expected_revision),
                authority_revision=lease.revision,
            )
        if not lease.write_enabled:
            raise MutationError(
                "mutation_write_disabled",
                "session mutation write authority is off for this chat",
                authority_revision=lease.revision,
            )
        if not self.mutation_allowed():
            raise MutationError(
                "mutation_frozen",
                "session mutation is frozen by the host",
                authority_revision=lease.revision,
            )
        return lease

    @staticmethod
    def _assert_recovery_authority_in_conn(
        conn: sqlite3.Connection,
        chat_id: str,
        *,
        expected_revision: int,
    ) -> MutationAuthorityLease:
        row = conn.execute(
            "SELECT mutation_write_enabled, mutation_authority_revision "
            "FROM astb_chat_runtime WHERE chat_id=?",
            (str(chat_id),),
        ).fetchone()
        if row is None:
            raise MutationError("runtime_unavailable", "chat runtime is unavailable")
        lease = MutationAuthorityLease(
            write_enabled=bool(row["mutation_write_enabled"]),
            revision=int(row["mutation_authority_revision"] or 0),
        )
        if lease.revision != int(expected_revision):
            raise MutationError(
                "mutation_authority_changed",
                "session mutation authority changed while recovery was running",
                expected_revision=int(expected_revision),
                authority_revision=lease.revision,
            )
        return lease

    def _record_failure(
        self,
        chat_id: str,
        draft_id: str,
        *,
        stage: str,
        code: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        try:
            draft = self._draft(chat_id, draft_id)
            fingerprint = str(draft.get("proposal_fingerprint") or "")
        except Exception:
            fingerprint = ""
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO mutation_failure(failure_id, chat_id, draft_id, stage, "
                "proposal_fingerprint, code, details_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "mfail_" + uuid.uuid4().hex, str(chat_id), str(draft_id),
                    str(stage), fingerprint, str(code), _stable(details or {}), _now(),
                ),
            )

    def _assert_activation_allowed(self, chat_id: str) -> None:
        with self._lock, self._connect() as conn:
            failed = int(conn.execute(
                "SELECT COUNT(*) FROM mutation_failure WHERE chat_id=? "
                "AND stage='activation'", (str(chat_id),)
            ).fetchone()[0])
        if failed >= MAX_FAILED_ACTIVATIONS:
            raise MutationError(
                "failed_activation_quota",
                f"session reached the failed activation quota ({MAX_FAILED_ACTIVATIONS})",
            )

    def _draft(self, chat_id: str, draft_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM mutation_draft WHERE chat_id=? AND draft_id=?",
                (str(chat_id), str(draft_id)),
            ).fetchone()
        if row is None:
            raise MutationError("unknown_draft", "unknown session mutation draft")
        result = dict(row)
        for key in (
            "schema_json", "params_json", "dependencies_json",
            "tests_json", "validation_json", "host_test_json",
        ):
            result[key[:-5] if key.endswith("_json") else key] = (
                json.loads(result[key]) if result[key] else None
            )
        return result

    def _active_slot_draft(
        self,
        chat_id: str,
        slot_id: str,
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT version, draft_id FROM astb_activation WHERE chat_id=? "
                "AND slot_id=? AND active=1",
                (str(chat_id), str(slot_id)),
            ).fetchone()
        if row is None:
            return None
        draft = self._draft(chat_id, str(row["draft_id"]))
        draft["active_version"] = int(row["version"])
        return draft

    def _assert_projected_namespace_in_conn(
        self,
        conn: sqlite3.Connection,
        chat_id: str,
        loaded: LoadedCatalog,
        *,
        target_slot_id: str,
        replacement: Mapping[str, Any] | None,
    ) -> None:
        """Reject direct-seed and mounted-object collisions before activation."""

        rows = conn.execute(
            "SELECT d.slot_id, d.catalog_release_id, d.category_id, d.position, "
            "d.declared_kind, d.alias FROM astb_activation a "
            "JOIN mutation_draft d ON d.draft_id=a.draft_id "
            "WHERE a.chat_id=? AND a.active=1",
            (str(chat_id),),
        ).fetchall()
        active = {
            str(row["slot_id"]): dict(row)
            for row in rows
            if str(row["catalog_release_id"]) == loaded.release_id
        }
        active.pop(str(target_slot_id), None)
        if replacement is not None:
            active[str(target_slot_id)] = dict(replacement)

        enabled = set(self.enabled_resolver() or ())
        for category in loaded.document.get("categories") or ():
            category_id = str(category.get("category_id") or "")
            owners: dict[str, str] = {}
            for slot in category.get("slots") or ():
                position = int(slot.get("position") or 0)
                slot_id = f"{loaded.release_id}/{category_id}/{position}"
                overlay = active.get(slot_id)
                projections: list[tuple[str, str]] = []
                if overlay is not None:
                    if str(overlay.get("declared_kind") or "") in {
                        "mutate", "method",
                    }:
                        slot_projection = str(
                            slot.get("projection") or "seeds"
                        )
                        if slot_projection == "seeds":
                            projections.append((
                                "tools", str(overlay.get("alias") or ""),
                            ))
                        elif slot_projection == "object":
                            projections.append((
                                "globals",
                                str(slot.get("bundle") or ""),
                            ))
                        else:
                            raise MutationError(
                                "retired_projection",
                                f"slot uses retired projection: {slot_projection!r}",
                            )
                    else:
                        projections.append(("tools", str(overlay.get("alias") or "")))
                else:
                    admitted = [
                        binding for binding in (slot.get("bindings") or ())
                        if str(binding.get("tool_name") or "") in enabled
                    ]
                    if admitted and str(
                        slot.get("projection") or "seeds"
                    ) == "seeds":
                        projections.extend(
                            (
                                str(binding.get("namespace") or "tools"),
                                str(binding.get("alias") or ""),
                            )
                            for binding in admitted
                        )
                    elif admitted and str(
                        slot.get("projection") or "seeds"
                    ) == "object":
                        projections.append((
                            "globals",
                            str(slot.get("bundle") or ""),
                        ))
                    elif admitted:
                        raise MutationError(
                            "retired_projection",
                            "catalog slot uses a retired projection",
                        )

                for namespace, alias in dict.fromkeys(
                    item for item in projections if item[0] and item[1]
                ):
                    if not _ALIAS.fullmatch(namespace) or not _ALIAS.fullmatch(alias):
                        raise MutationError(
                            "invalid_projected_alias",
                            f"projected name is invalid: {namespace}.{alias}",
                            alias=f"{namespace}.{alias}",
                            slot_id=slot_id,
                        )
                    qualified = f"{namespace}.{alias}"
                    prior = owners.get(qualified)
                    if prior is not None and prior != slot_id:
                        raise MutationError(
                            "alias_conflict",
                            (
                                f"projected name {qualified!r} conflicts "
                                f"between {prior!r} and {slot_id!r}"
                            ),
                            alias=qualified,
                            slot_ids=[prior, slot_id],
                            category_id=category_id,
                        )
                    owners[qualified] = slot_id

    def _receipt_in_conn(
        self, conn: sqlite3.Connection, chat_id: str, draft_id: str,
        kind: str, payload: dict[str, Any],
    ) -> dict[str, Any]:
        body = {"schema": MUTATION_SCHEMA, "kind": kind, **json_safe(payload)}
        digest = _digest(body)
        receipt_id = "mrcpt_" + uuid.uuid4().hex
        conn.execute(
            "INSERT INTO mutation_receipt(receipt_id, chat_id, draft_id, kind, "
            "payload_json, receipt_digest, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (receipt_id, str(chat_id), str(draft_id), kind, _stable(body), digest, _now()),
        )
        return {**body, "receipt_id": receipt_id, "receipt_digest": digest}

    def _proxy_contracts(
        self,
        loaded: LoadedCatalog,
        chat_id: str,
        *,
        exclude_slot_id: str = "",
        exclude_proxy: str = "",
    ) -> dict[str, dict[str, Any]]:
        """Build the ordinary Python proxy surface available to mutations.

        Category mounts remain a disclosure mechanism. Candidate code may use
        any currently installed catalog proxy by its normal Python name; the
        host derives dependencies from source and observed receipts.
        """

        enabled = set(self.enabled_resolver() or ())
        contracts: dict[str, dict[str, Any]] = {}

        def add(
            qualified_name: str,
            *,
            binding: Mapping[str, Any],
            params: Mapping[str, Any] | None = None,
            fixed_arguments: Mapping[str, Any] | None = None,
            argument_envelope: bool = False,
            slot_id: str = "",
            slot_version: int = 0,
            effect_class: str = "",
        ) -> None:
            name = str(qualified_name or "").strip()
            if not name or name in contracts:
                return
            ref = CapabilityRef(
                capability_id=str(binding.get("capability_id") or ""),
                schema_revision=str(binding.get("schema_revision") or ""),
                handler_revision=str(binding.get("handler_revision") or ""),
                catalog_release_id=loaded.release_id,
                slot_id=str(slot_id or ""),
                slot_version=max(0, int(slot_version or 0)),
            )
            contract_params = dict(params or binding.get("params") or {})
            contracts[name] = {
                "proxy": name,
                "ref": ref.to_dict(),
                "effect_class": str(
                    effect_class
                    or binding.get("effect_class")
                    or "external_side_effect"
                ),
                "parameters": [str(name) for name in contract_params],
                "fixed_arguments": dict(fixed_arguments or {}),
                "argument_envelope": bool(argument_envelope),
            }

        for category in loaded.document.get("categories") or ():
            category_id = str(category.get("category_id") or "")
            for slot in category.get("slots") or ():
                position = int(slot.get("position") or 0)
                slot_id = f"{loaded.release_id}/{category_id}/{position}"
                bindings = [
                    dict(row) for row in (slot.get("bindings") or ())
                    if str(row.get("tool_name") or "") in enabled
                ]
                if not bindings:
                    continue
                projection = str(slot.get("projection") or "seeds")
                if projection == "seeds":
                    for binding in bindings:
                        add(
                            f"tools.{binding.get('alias')}",
                            binding=binding,
                            slot_id=slot_id,
                        )
                    continue
                if projection != "object" or len(bindings) != 1:
                    continue
                binding = bindings[0]
                root = str(slot.get("bundle") or "")
                for method in slot.get("methods") or ():
                    if not isinstance(method, Mapping):
                        continue
                    alias = str(method.get("alias") or "")
                    add(
                        f"{root}.{alias}",
                        binding=binding,
                        params=dict(method.get("params") or {}),
                        fixed_arguments={
                            "operation": str(method.get("operation") or alias)
                        },
                        slot_id=slot_id,
                        effect_class=str(method.get("effect_class") or ""),
                    )

            for api in category.get("python_apis") or ():
                if not isinstance(api, Mapping):
                    continue
                root = str(api.get("name") or "")
                if not root or root == "toolbelt":
                    continue
                transport = dict(api.get("transport") or {})
                if str(api.get("tool_name") or transport.get("tool_name") or "") not in enabled:
                    continue
                binding = {
                    **transport,
                    "capability_id": str(
                        transport.get("capability_id")
                        or api.get("tool_name")
                        or root
                    ),
                }
                slot_id = f"{loaded.release_id}/{category_id}/api-{root}"
                for method in api.get("methods") or ():
                    if not isinstance(method, Mapping):
                        continue
                    alias = str(method.get("alias") or "")
                    add(
                        f"{root}.{alias}",
                        binding=binding,
                        params=dict(method.get("params") or {}),
                        fixed_arguments={
                            "operation": str(method.get("operation") or alias)
                        },
                        slot_id=slot_id,
                        effect_class=str(method.get("effect_class") or ""),
                    )

        # Activated session tools are finite slot versions, not a growing
        # script list. They can be composed by later session mutations.
        try:
            record = self.runtime_registry.ensure_runtime(str(chat_id))
            overlays, _refs = self.active_overlays(
                str(chat_id),
                loaded,
                mount_revision=int(record.identity.mount_revision or 0),
            )
        except Exception:
            overlays = []
        for descriptor in overlays:
            same_slot = str(descriptor.get("slot_id") or "") == str(
                exclude_slot_id or ""
            )
            is_object = str(descriptor.get("kind") or "") == "mounted_object"
            if same_slot and not (is_object and exclude_proxy):
                continue
            if is_object:
                root = str(descriptor.get("name") or "")
                for method in descriptor.get("methods") or ():
                    if not isinstance(method, Mapping):
                        continue
                    qualified = f"{root}.{method.get('alias')}"
                    if same_slot and qualified == str(exclude_proxy or ""):
                        continue
                    contracts.pop(qualified, None)
                    add(
                        qualified,
                        binding=method,
                        params=dict(method.get("params") or {}),
                        fixed_arguments=dict(method.get("fixed_arguments") or {}),
                        argument_envelope=bool(method.get("argument_envelope")),
                        slot_id=str(method.get("slot_id") or ""),
                        slot_version=int(method.get("slot_version") or 0),
                        effect_class=str(method.get("effect_class") or ""),
                    )
            else:
                contracts.pop(f"tools.{descriptor.get('alias')}", None)
                add(
                    f"tools.{descriptor.get('alias')}",
                    binding=descriptor,
                    params=dict(descriptor.get("params") or {}),
                    fixed_arguments=dict(descriptor.get("fixed_arguments") or {}),
                    argument_envelope=bool(descriptor.get("argument_envelope")),
                    slot_id=str(descriptor.get("slot_id") or ""),
                    slot_version=int(descriptor.get("slot_version") or 0),
                    effect_class=str(descriptor.get("effect_class") or ""),
                )

        # Remote handles remain host-owned and opaque inside the disposable
        # mutation worker.  Give the worker one private broker route so a
        # handle returned by any normal proxy can dispatch its declared
        # methods without exposing connector credentials or live leases.
        hidden_dispatch = self.registry.get("remote_handle_dispatch")
        if (
            hidden_dispatch is not None
            and "remote_handle_dispatch" in enabled
        ):
            add(
                MUTATION_REMOTE_HANDLE_PROXY,
                binding=hidden_dispatch.broker_metadata(),
                params=dict(hidden_dispatch.params or {}),
                effect_class="external_side_effect",
            )
            contracts[MUTATION_REMOTE_HANDLE_PROXY]["internal_role"] = (
                MUTATION_REMOTE_HANDLE_ROLE
            )
        return contracts

    @staticmethod
    def _draft_target_proxy(
        loaded: LoadedCatalog, draft: Mapping[str, Any],
    ) -> str:
        if str(draft.get("declared_kind") or "") != "method":
            return ""
        _category, _position, slot = _slot(
            loaded, str(draft.get("slot_id") or "")
        )
        return (
            f"{slot.get('bundle')}.{draft.get('alias')}"
            if slot.get("bundle") and draft.get("alias")
            else ""
        )

    @staticmethod
    def _worker_proxy_contracts(
        contracts: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        return {
            str(name): {
                "parameters": list(dict(contract).get("parameters") or ()),
                **(
                    {"internal_role": str(dict(contract).get("internal_role"))}
                    if dict(contract).get("internal_role")
                    else {}
                ),
            }
            for name, contract in contracts.items()
        }

    @staticmethod
    def _source_dependencies(
        source: str,
        contracts: Mapping[str, Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        try:
            tree = ast.parse(str(source), filename="<session-mutation>", mode="exec")
        except SyntaxError:
            return []
        roots = {name.split(".", 1)[0] for name in contracts}
        referenced: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if not isinstance(node.func.value, ast.Name):
                continue
            root = str(node.func.value.id)
            if root not in roots:
                continue
            qualified = f"{root}.{node.func.attr}"
            if qualified not in contracts:
                raise MutationError(
                    "unknown_proxy",
                    f"mutation source calls an unavailable proxy: {qualified}",
                )
            referenced.add(qualified)
        return [dict(contracts[name]) for name in sorted(referenced)]

    def propose(
        self, chat_id: str, *, kind: str, slot: str, alias: str, purpose: str,
        schema: Any, source: str, tests: list[Any] | None = None,
        parent: str | None = None,
        expected_authority_revision: int | None = None,
    ) -> dict[str, Any]:
        record, loaded, authority = self._require_write_authority(
            chat_id, expected_revision=expected_authority_revision
        )
        declared_kind = str(kind or "").strip().casefold()
        if declared_kind not in {"mutate", "method", "create", "revise"}:
            raise MutationError(
                "invalid_kind",
                "mutation kind must be mutate, method, create, or revise",
            )
        slot_reference = _selected_slot_reference(record, slot)
        category_id, position, base_slot = _slot(loaded, slot_reference)
        slot_id = f"{loaded.release_id}/{category_id}/{position}"
        occupied = str(base_slot.get("status") or "") == "seed"
        vacant = str(base_slot.get("status") or "") == "vacant"
        clean_alias = str(alias or "").strip()
        if not _ALIAS.fullmatch(clean_alias):
            raise MutationError("invalid_alias", "mutation alias must be a Python identifier")
        parent_slot_id = ""
        if declared_kind in {"mutate", "method"}:
            if not occupied:
                raise MutationError(
                    "shape_mismatch",
                    f"{declared_kind} requires an occupied seed slot",
                )
            if not parent:
                raise MutationError(
                    "parent_required",
                    f"{declared_kind} requires its owning slot as parent",
                )
            parent_reference = _selected_slot_reference(record, parent)
            parent_category, parent_position, _ = _slot(loaded, parent_reference)
            parent_slot_id = f"{loaded.release_id}/{parent_category}/{parent_position}"
            if parent_slot_id != slot_id:
                raise MutationError(
                    "parent_mismatch", "mutation parent must equal the target slot"
                )
            if declared_kind == "method" and str(
                base_slot.get("projection") or "seeds"
            ) != "object":
                raise MutationError(
                    "shape_mismatch", "method mutation requires a coherent object slot"
                )
        elif declared_kind == "revise":
            active_parent = self._active_slot_draft(chat_id, slot_id)
            if (
                not vacant
                or active_parent is None
                or str(active_parent.get("declared_kind") or "")
                not in {"create", "revise"}
            ):
                raise MutationError(
                    "shape_mismatch",
                    "revise requires an active synthesized direct-tool slot",
                )
            if not parent:
                raise MutationError(
                    "parent_required",
                    "revise requires its active synthesized slot as parent",
                )
            parent_reference = _selected_slot_reference(record, parent)
            parent_category, parent_position, _ = _slot(loaded, parent_reference)
            parent_slot_id = f"{loaded.release_id}/{parent_category}/{parent_position}"
            if parent_slot_id != slot_id:
                raise MutationError(
                    "parent_mismatch",
                    "revision parent must equal the target synthesized slot",
                )
            if clean_alias != str(active_parent.get("alias") or ""):
                raise MutationError(
                    "alias_mismatch",
                    "revision alias must retain the active synthesized tool alias",
                )
        elif not vacant:
            raise MutationError("shape_mismatch", "create requires a vacant catalog slot")
        elif self._active_slot_draft(chat_id, slot_id) is not None:
            raise MutationError(
                "slot_already_active",
                "vacant catalog slot already has an active synthesized tool; "
                "revise that mounted tool instead",
            )
        if parent and declared_kind == "create":
            raise MutationError("parent_forbidden", "create cannot declare a parent")
        if declared_kind == "method":
            method_aliases = {
                str(item.get("alias") or "")
                for item in (base_slot.get("methods") or ())
                if isinstance(item, Mapping)
            }
            if clean_alias not in method_aliases:
                raise MutationError(
                    "unknown_method",
                    f"{base_slot.get('bundle')}.{clean_alias} is not a mounted method",
                )
        clean_source = str(source or "")
        if not clean_source.strip() or len(clean_source.encode("utf-8")) > 64 * 1024:
            raise MutationError("source_quota", "mutation source must be 1..65536 bytes")
        clean_purpose = str(purpose or "").strip()
        if not clean_purpose or len(clean_purpose) > 2000:
            raise MutationError("invalid_purpose", "mutation purpose must be 1..2000 characters")
        normalized_schema, params = _public_schema(schema)
        clean_tests = list(tests or ())
        if len(clean_tests) > 20 or len(canonical_bytes(clean_tests)) > 64 * 1024:
            raise MutationError("test_quota", "mutation tests exceed the session draft quota")
        proxy_contracts = self._proxy_contracts(
            loaded,
            str(chat_id),
            exclude_slot_id=slot_id,
            exclude_proxy=(
                f"{base_slot.get('bundle')}.{clean_alias}"
                if declared_kind == "method"
                else ""
            ),
        )
        dependencies = self._source_dependencies(clean_source, proxy_contracts)
        proposal_fingerprint = _digest({
            "catalog_release_id": loaded.release_id,
            "slot_id": slot_id,
            "kind": declared_kind,
            "source_sha256": hashlib.sha256(clean_source.encode("utf-8")).hexdigest(),
            "schema": normalized_schema,
            "dependencies": dependencies,
        })
        with self._lock, self._connect() as conn:
            repeated = int(conn.execute(
                "SELECT COUNT(*) FROM mutation_failure WHERE chat_id=? "
                "AND proposal_fingerprint=?",
                (str(chat_id), proposal_fingerprint),
            ).fetchone()[0])
        if repeated >= IDENTICAL_FAILURE_BREAKER:
            raise MutationError(
                "identical_failure_breaker",
                "this exact failed mutation proposal is circuit-broken for the session",
            )
        source_artifact = self.artifact_store.put_text(
            clean_source, kind="mutation_source", scope=str(chat_id)
        )
        with self._lock, self._connect() as conn:
            active = int(conn.execute(
                "SELECT COUNT(*) FROM mutation_draft WHERE chat_id=? "
                "AND status IN ('draft','validated','tested','probation')",
                (str(chat_id),),
            ).fetchone()[0])
            if active >= 8:
                raise MutationError("draft_quota", "session has eight active mutation drafts")
            draft_id = "draft_" + uuid.uuid4().hex
            now = _now()
            conn.execute("BEGIN IMMEDIATE")
            self._assert_write_authority_in_conn(
                conn, chat_id, expected_revision=authority.revision
            )
            conn.execute(
                "INSERT INTO mutation_draft(draft_id, chat_id, catalog_release_id, "
                "category_id, position, slot_id, declared_kind, parent_slot_id, alias, "
                "purpose, schema_json, params_json, source_ref, source_sha256, "
                "proposal_fingerprint, dependencies_json, tests_json, "
                "status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "'draft', ?, ?)",
                (
                    draft_id, str(chat_id), loaded.release_id, category_id, position,
                    slot_id, declared_kind, parent_slot_id, clean_alias, clean_purpose,
                    _stable(normalized_schema), _stable(params), source_artifact.ref,
                    source_artifact.sha256, proposal_fingerprint,
                    _stable(dependencies), _stable(clean_tests), now, now,
                ),
            )
            receipt = self._receipt_in_conn(conn, chat_id, draft_id, "proposed", {
                "draft_id": draft_id,
                "slot_id": slot_id,
                "kind": declared_kind,
                "source_sha256": source_artifact.sha256,
                "schema_sha256": _digest(normalized_schema),
                "mount_revision": int(record.identity.mount_revision or 0),
            })
            conn.commit()
        operational_log(
            "mutation",
            "proposed",
            chat_id=str(chat_id),
            draft_id=draft_id,
            slot_id=slot_id,
            kind=declared_kind,
            alias=clean_alias,
        )
        return {
            "draft_id": draft_id, "status": "draft", "slot_id": slot_id,
            "kind": declared_kind, "alias": clean_alias,
            "source_sha256": source_artifact.sha256,
            "receipt": receipt,
        }

    async def _mock_call(
        self,
        mocks: Mapping[str, Any],
        name: str,
        arguments: dict[str, Any],
        request_id: str,
        cursors: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        del arguments, request_id
        fallback_name = name.split(".", 1)[-1]
        result = mocks.get(
            name,
            mocks.get(fallback_name, {"mock_proxy": name}),
        )
        if (
            isinstance(result, Mapping)
            and set(result) == {"$sequence"}
        ):
            sequence = result.get("$sequence")
            if not isinstance(sequence, list) or not sequence:
                return {
                    "ok": False,
                    "error": {
                        "code": "invalid_mock_sequence",
                        "message": f"mock sequence for {name!r} must be a non-empty array",
                    },
                }
            positions = cursors if cursors is not None else {}
            index = int(positions.get(name, 0))
            if index >= len(sequence):
                return {
                    "ok": False,
                    "error": {
                        "code": "mock_sequence_exhausted",
                        "message": f"mock sequence for {name!r} has no result for call {index + 1}",
                    },
                }
            positions[name] = index + 1
            result = sequence[index]
        return {
            "ok": True,
            "result": json_safe(result),
            "receipt_id": "mock_" + uuid.uuid4().hex,
        }

    def _merge_observed_dependencies(
        self,
        chat_id: str,
        draft_id: str,
        contracts: Mapping[str, Mapping[str, Any]],
        observed_calls: list[Any],
    ) -> None:
        names = {
            str(row.get("proxy") or "")
            for row in observed_calls
            if isinstance(row, Mapping) and str(row.get("proxy") or "") in contracts
        }
        if not names:
            return
        draft = self._draft(chat_id, draft_id)
        merged = {
            str(row.get("proxy") or ""): dict(row)
            for row in (draft.get("dependencies") or ())
            if isinstance(row, Mapping) and row.get("proxy")
        }
        for name in names:
            merged[name] = dict(contracts[name])
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE mutation_draft SET dependencies_json=?, updated_at=? "
                "WHERE chat_id=? AND draft_id=?",
                (
                    _stable([merged[name] for name in sorted(merged)]),
                    _now(),
                    str(chat_id),
                    str(draft_id),
                ),
            )

    async def validate(
        self,
        chat_id: str,
        draft_id: str,
        *,
        expected_authority_revision: int | None = None,
    ) -> dict[str, Any]:
        _record, loaded, authority = self._require_write_authority(
            chat_id, expected_revision=expected_authority_revision
        )
        draft = self._draft(chat_id, draft_id)
        if draft["status"] != "draft":
            raise MutationError(
                "draft_not_validatable",
                "mutation validation requires a draft in draft status",
                status=str(draft["status"]),
            )
        source = self.artifact_store.read_bytes_scoped(
            draft["source_ref"], str(chat_id)
        ).decode("utf-8")
        started = time.perf_counter()
        contracts = self._proxy_contracts(
            loaded,
            str(chat_id),
            exclude_slot_id=str(draft.get("slot_id") or ""),
            exclude_proxy=self._draft_target_proxy(loaded, draft),
        )
        try:
            report = await self.worker.run(
                {
                    "mode": "validate",
                    "source": source,
                    "proxy_contracts": self._worker_proxy_contracts(contracts),
                },
                proxy_call=lambda name, args, rid: self._mock_call(
                    {}, name, args, rid
                ),
            )
            status = "validated"
        except MutationWorkerError as exc:
            report = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
            status = "rejected"
            failure = exc
        else:
            failure = None
        # The host owns execution metadata. Never trust a candidate's
        # self-reported execution fields.
        report.update(self.worker.execution_status())
        report["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._assert_write_authority_in_conn(
                conn, chat_id, expected_revision=authority.revision
            )
            receipt = self._receipt_in_conn(conn, chat_id, draft_id, "validated", {
                "draft_id": draft_id, **report,
            })
            updated = conn.execute(
                "UPDATE mutation_draft SET status=?, validation_json=?, updated_at=? "
                "WHERE chat_id=? AND draft_id=? AND status='draft'",
                (status, _stable(receipt), _now(), str(chat_id), str(draft_id)),
            )
            if updated.rowcount != 1:
                current = conn.execute(
                    "SELECT status FROM mutation_draft WHERE chat_id=? AND draft_id=?",
                    (str(chat_id), str(draft_id)),
                ).fetchone()
                conn.rollback()
                raise MutationError(
                    "draft_not_validatable",
                    "mutation validation requires a draft in draft status",
                    status=str(current["status"] if current is not None else "missing"),
                )
            conn.commit()
        if failure is not None:
            self._record_failure(
                chat_id, draft_id, stage="validation", code=failure.code,
                details={"message": str(failure)},
            )
        return receipt

    async def test(
        self, chat_id: str, draft_id: str, cases: list[Any] | None = None,
        *, authority: str = "model", persist_status: bool = True,
        expected_authority_revision: int | None = None,
        workspace_roots: Sequence[str] = (),
    ) -> dict[str, Any]:
        _record, loaded, authority_lease = self._require_write_authority(
            chat_id, expected_revision=expected_authority_revision
        )
        draft = self._draft(chat_id, draft_id)
        preactivation_states = {"draft", "validated", "tested"}
        if persist_status and str(draft.get("status") or "") not in preactivation_states:
            raise MutationError(
                "draft_not_testable",
                "persisted mutation tests require a pre-activation draft",
                status=str(draft.get("status") or "missing"),
            )
        source = self.artifact_store.read_bytes_scoped(
            draft["source_ref"], str(chat_id)
        ).decode("utf-8")
        selected = list(cases if cases is not None else (draft.get("tests") or ()))
        if len(selected) > 20:
            raise MutationError("test_quota", "too many mutation test cases")
        results: list[dict[str, Any]] = []
        ok = True
        contracts = self._proxy_contracts(
            loaded,
            str(chat_id),
            exclude_slot_id=str(draft.get("slot_id") or ""),
            exclude_proxy=self._draft_target_proxy(loaded, draft),
        )
        observed_all: list[Any] = []
        effective_roots = _effective_workspace_roots(workspace_roots)
        for index, case in enumerate(selected):
            if not isinstance(case, dict) or not isinstance(case.get("arguments", {}), dict):
                raise MutationError("invalid_test", f"test case {index} is invalid")
            arguments = _normalize_schema_value(
                dict(case.get("arguments") or {}), draft["schema"],
                f"cases[{index}].arguments", coerce=False,
            )
            mocks = dict(case.get("mocks") or {})
            mock_cursors: dict[str, int] = {}
            try:
                report = await self.worker.run(
                    {
                        "mode": "execute",
                        "source": source,
                        "arguments": arguments,
                        "proxy_contracts": self._worker_proxy_contracts(contracts),
                        "workspace_roots": effective_roots,
                    },
                    proxy_call=lambda name, args, rid, values=mocks, cursors=mock_cursors: self._mock_call(
                        values, name, args, rid, cursors
                    ),
                )
                expected_present = "expected" in case
                passed = not expected_present or report.get("result") == case.get("expected")
                observed = list(report.get("observed_calls") or [])
                observed_all.extend(observed)
                results.append({
                    "index": index, "ok": passed,
                    "result_sha256": _digest(report.get("result")),
                    "observed_calls": observed,
                })
                ok = ok and passed
            except MutationWorkerError as exc:
                ok = False
                results.append({
                    "index": index, "ok": False,
                    "error": {"code": exc.code, "message": str(exc)},
                })
        if persist_status:
            self._merge_observed_dependencies(
                chat_id,
                draft_id,
                contracts,
                observed_all,
            )
        payload = {
            "draft_id": draft_id, "ok": ok, "authority": str(authority),
            "case_count": len(selected), "cases_sha256": _digest(selected),
            "results": results,
        }
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._assert_write_authority_in_conn(
                conn, chat_id, expected_revision=authority_lease.revision
            )
            receipt = self._receipt_in_conn(conn, chat_id, draft_id, "tested", payload)
            if persist_status and (authority == "host" or ok):
                updated = conn.execute(
                    "UPDATE mutation_draft SET status=?, host_test_json=?, updated_at=? "
                    "WHERE chat_id=? AND draft_id=? "
                    "AND status IN ('draft','validated','tested')",
                    (
                        "tested" if ok else "rejected",
                        _stable(receipt) if authority == "host" else draft.get("host_test_json") or "",
                        _now(), str(chat_id), str(draft_id),
                    ),
                )
                if updated.rowcount != 1:
                    current = conn.execute(
                        "SELECT status FROM mutation_draft WHERE chat_id=? AND draft_id=?",
                        (str(chat_id), str(draft_id)),
                    ).fetchone()
                    conn.rollback()
                    raise MutationError(
                        "draft_not_testable",
                        "mutation draft activated while isolated tests were running",
                        status=str(
                            current["status"] if current is not None else "missing"
                        ),
                    )
            conn.commit()
        if not ok and authority == "host":
            self._record_failure(
                chat_id, draft_id, stage="host_test", code="case_failure",
                details={"cases_sha256": payload["cases_sha256"]},
            )
        return receipt

    async def _host_contract_gate(
        self,
        chat_id: str,
        draft_id: str,
        *,
        expected_authority_revision: int,
    ) -> dict[str, Any]:
        """Verify the isolated worker contract independently of candidate tests."""
        _record, _loaded, authority = self._require_write_authority(
            chat_id, expected_revision=expected_authority_revision
        )
        full_python = (
            "import os\n"
            "from pathlib import Path\n"
            "def run(arguments):\n"
            "    return {'cwd': os.getcwd(), 'path_type': Path('.').name}\n"
        )
        invalid = "def helper(arguments):\n    return arguments\n"
        full_report = await self.worker.run(
            {"mode": "validate", "source": full_python},
            proxy_call=lambda n, a, r: self._mock_call({}, n, a, r),
        )
        invalid_blocked = False
        try:
            await self.worker.run(
                {"mode": "validate", "source": invalid},
                proxy_call=lambda n, a, r: self._mock_call({}, n, a, r),
            )
        except MutationWorkerError as exc:
            invalid_blocked = exc.code == "candidate_contract_error"
        if not full_report.get("ok") or not invalid_blocked:
            raise MutationError(
                "host_contract_gate_failed", "private worker contract cases failed"
            )
        payload = {
            "draft_id": draft_id, "ok": True,
            "evaluator_version": "variant1.astb.worker-contract.v2",
            "full_python_load": True, "invalid_contract_blocked": True,
            "oracle_exposed": False,
        }
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._assert_write_authority_in_conn(
                conn, chat_id, expected_revision=authority.revision
            )
            receipt = self._receipt_in_conn(
                conn, chat_id, draft_id, "worker_contract_tested", payload
            )
            conn.commit()
        return receipt

    async def _activate_once(
        self,
        chat_id: str,
        draft_id: str,
        *,
        expected_mount_revision: int | None = None,
        expected_authority_revision: int | None = None,
    ) -> dict[str, Any]:
        record, _loaded, authority = self._require_write_authority(
            chat_id, expected_revision=expected_authority_revision
        )
        expected = (
            int(record.identity.mount_revision or 0)
            if expected_mount_revision is None else int(expected_mount_revision)
        )
        draft = self._draft(chat_id, draft_id)
        invocation = self._activation_invocation(_loaded, draft)
        draft_status = str(draft.get("status") or "")
        if draft_status == "draft":
            validation = await self.validate(
                chat_id,
                draft_id,
                expected_authority_revision=authority.revision,
            )
        elif draft_status in {"validated", "tested"}:
            validation = dict(draft.get("validation") or {})
        else:
            raise MutationError(
                "draft_not_activatable",
                "mutation activation requires a draft, validated, or tested candidate",
                status=draft_status,
            )
        if validation.get("ok") is not True:
            raise MutationError("validation_failed", "draft validation did not pass")
        worker_contract = await self._host_contract_gate(
            chat_id,
            draft_id,
            expected_authority_revision=authority.revision,
        )
        draft = self._draft(chat_id, draft_id)
        # Declared examples improve validation but are not an authoring gate.
        # With the chat toggle on, syntax/contract validation followed by live
        # probation is sufficient. This keeps repair and thin-tool creation a
        # one-call model choice instead of forcing mocked-test ceremony.
        host_test = await self.test(
            chat_id,
            draft_id,
            authority="host",
            persist_status=True,
            expected_authority_revision=authority.revision,
        )
        if host_test.get("ok") is not True:
            diagnostics: list[str] = []
            for row in host_test.get("results") or ():
                if not isinstance(row, Mapping) or row.get("ok") is True:
                    continue
                index = int(row.get("index") or 0)
                error = row.get("error")
                if isinstance(error, Mapping):
                    code = str(error.get("code") or "candidate_error")
                    message = str(error.get("message") or "")[:300]
                else:
                    code = "expected_mismatch"
                    message = "candidate return value did not equal expected"
                diagnostics.append(f"case {index}: {code}: {message}".rstrip(": "))
            detail = "; ".join(diagnostics[:5])
            raise MutationError(
                "candidate_tests_failed",
                "draft candidate tests did not pass"
                + (f" ({detail})" if detail else ""),
                diagnostics=diagnostics[:5],
            )
        draft = self._draft(chat_id, draft_id)
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._assert_write_authority_in_conn(
                conn, chat_id, expected_revision=authority.revision
            )
            runtime = conn.execute(
                "SELECT * FROM astb_chat_runtime WHERE chat_id=?", (str(chat_id),)
            ).fetchone()
            if runtime is None or runtime["lifecycle_state"] != "active":
                raise MutationError("runtime_unavailable", "chat runtime is unavailable")
            current_mount = int(runtime["mount_revision"] or 0)
            if current_mount != expected:
                raise MountConflict(f"mutation activation CAS failed ({current_mount} != {expected})")
            current = conn.execute(
                "SELECT a.version, a.draft_id, d.declared_kind, d.alias "
                "FROM astb_activation a JOIN mutation_draft d "
                "ON d.draft_id=a.draft_id WHERE a.chat_id=? "
                "AND a.slot_id=? AND a.active=1",
                (str(chat_id), draft["slot_id"]),
            ).fetchone()
            draft_kind = str(draft.get("declared_kind") or "")
            if draft_kind == "create" and current is not None:
                raise MutationError(
                    "slot_already_active",
                    "a new create cannot replace an active synthesized slot; "
                    "revise the mounted tool instead",
                )
            if draft_kind == "revise" and (
                current is None
                or str(current["declared_kind"] or "") not in {"create", "revise"}
                or str(current["alias"] or "") != str(draft.get("alias") or "")
            ):
                raise MutationError(
                    "revision_parent_changed",
                    "active synthesized revision parent changed before activation",
                )
            previous_version = int(current["version"] if current else 0)
            self._assert_projected_namespace_in_conn(
                conn,
                chat_id,
                _loaded,
                target_slot_id=str(draft["slot_id"]),
                replacement=draft,
            )
            existing = conn.execute(
                "SELECT version FROM astb_slot_version WHERE chat_id=? AND slot_id=? AND draft_id=?",
                (str(chat_id), draft["slot_id"], draft_id),
            ).fetchone()
            if existing is None:
                count = int(conn.execute(
                    "SELECT COUNT(*) FROM astb_slot_version WHERE chat_id=? AND slot_id=? "
                    "AND status<>'garbage_collected'",
                    (str(chat_id), draft["slot_id"]),
                ).fetchone()[0])
                if count >= 8:
                    raise MutationError("version_quota", "slot has eight retained versions")
                version = int(conn.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM astb_slot_version "
                    "WHERE chat_id=? AND slot_id=?",
                    (str(chat_id), draft["slot_id"]),
                ).fetchone()[0])
                conn.execute(
                    "INSERT INTO astb_slot_version(chat_id, slot_id, version, draft_id, "
                    "content_sha256, previous_version, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'probation', ?)",
                    (
                        str(chat_id), draft["slot_id"], version, draft_id,
                        _digest({
                            "source": draft["source_sha256"], "schema": draft["schema"],
                            "dependencies": draft["dependencies"],
                        }), previous_version, now,
                    ),
                )
            else:
                version = int(existing["version"])
            if current and int(current["version"]) == version:
                conn.rollback()
                return {
                    "ok": True, "already_active": True, "draft_id": draft_id,
                    "slot_id": draft["slot_id"], "slot_version": version,
                    "mount_revision": current_mount,
                    "invocation": invocation,
                }
            next_mount = current_mount + 1
            overlay = int(runtime["overlay_revision"] or 0) + 1
            transition = {
                "draft_id": draft_id, "slot_id": draft["slot_id"],
                "slot_version": version, "previous_slot_version": previous_version,
                "from_mount_revision": current_mount, "to_mount_revision": next_mount,
                "validation_receipt_digest": validation["receipt_digest"],
                "host_test_receipt_digest": host_test["receipt_digest"],
                "worker_contract_receipt_digest": worker_contract["receipt_digest"],
            }
            receipt = self._receipt_in_conn(conn, chat_id, draft_id, "activated", transition)
            conn.execute(
                "UPDATE astb_activation SET active=0 WHERE chat_id=? AND slot_id=? AND active=1",
                (str(chat_id), draft["slot_id"]),
            )
            conn.execute(
                "INSERT INTO astb_activation(activation_id, chat_id, slot_id, version, "
                "draft_id, active, action, mount_revision, receipt_digest, created_at) "
                "VALUES (?, ?, ?, ?, ?, 1, 'activate', ?, ?, ?)",
                (
                    "activation_" + uuid.uuid4().hex, str(chat_id), draft["slot_id"],
                    version, draft_id, next_mount, receipt["receipt_digest"], now,
                ),
            )
            conn.execute(
                "INSERT INTO mutation_probation(chat_id, slot_id, version, draft_id, "
                "previous_version, lkg_version, status, calls, successful_calls, "
                "semantic_errors, mechanical_errors, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'probation', 0, 0, 0, 0, ?) "
                "ON CONFLICT(chat_id, slot_id) DO UPDATE SET version=excluded.version, "
                "draft_id=excluded.draft_id, previous_version=excluded.previous_version, "
                "status='probation', calls=0, successful_calls=0, semantic_errors=0, "
                "mechanical_errors=0, updated_at=excluded.updated_at",
                (
                    str(chat_id), draft["slot_id"], version, draft_id,
                    previous_version, previous_version, now,
                ),
            )
            conn.execute(
                "UPDATE mutation_draft SET status='probation', updated_at=? "
                "WHERE chat_id=? AND draft_id=?", (now, str(chat_id), draft_id),
            )
            conn.execute(
                "UPDATE astb_chat_runtime SET mount_revision=?, overlay_revision=?, "
                "updated_at=?, version=version+1 WHERE chat_id=?",
                (next_mount, overlay, now, str(chat_id)),
            )
            conn.execute(
                "INSERT INTO astb_mount_history(chat_id, mount_revision, catalog_release_id, "
                "category_id, reason, created_at) VALUES (?, ?, ?, ?, 'mutation_activate', ?)",
                (
                    str(chat_id), next_mount, draft["catalog_release_id"],
                    str(runtime["selected_category_id"] or ""), now,
                ),
            )
            conn.commit()
        return {
            "ok": True, "already_active": False, "draft_id": draft_id,
            "slot_id": draft["slot_id"], "slot_version": version,
            "previous_slot_version": previous_version,
            "mount_revision": next_mount, "overlay_revision": overlay,
            "probation": True, "receipt": receipt,
            "invocation": invocation,
        }

    @staticmethod
    def _activation_invocation(
        loaded: LoadedCatalog,
        draft: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Describe the exact callable installed by one activation."""

        namespace = "tools"
        declared_kind = str(draft.get("declared_kind") or "")
        if declared_kind in {"mutate", "method"}:
            _category, _position, base = _slot(
                loaded, str(draft.get("slot_id") or "")
            )
            if str(base.get("projection") or "seeds") == "object":
                namespace = str(base.get("bundle") or "tools")
        alias = str(draft.get("alias") or "")
        signature = binding_signature(alias, dict(draft.get("params") or {}))
        qualified_name = (
            f"tools.{alias}" if namespace == "tools"
            else f"{namespace}.{alias}"
        )
        return {
            "qualified_name": qualified_name,
            "call": (
                f"tools.{signature}" if namespace == "tools"
                else f"{namespace}.{signature}"
            ),
            "category_id": str(draft.get("category_id") or ""),
            "available": "next_cell",
        }

    async def activate(
        self,
        chat_id: str,
        draft_id: str,
        *,
        expected_mount_revision: int | None = None,
        expected_authority_revision: int | None = None,
    ) -> dict[str, Any]:
        self._assert_activation_allowed(chat_id)
        try:
            result = await self._activate_once(
                chat_id,
                draft_id,
                expected_mount_revision=expected_mount_revision,
                expected_authority_revision=expected_authority_revision,
            )
            operational_log(
                "mutation",
                "activated",
                chat_id=str(chat_id),
                draft_id=str(draft_id),
                slot_id=result.get("slot_id"),
                slot_version=result.get("slot_version"),
                mount_revision=result.get("mount_revision"),
                already_active=bool(result.get("already_active")),
            )
            return result
        except MutationError as exc:
            if exc.code not in {
                "mutation_write_disabled",
                "mutation_authority_changed",
                "mutation_frozen",
            }:
                self._record_failure(
                    chat_id, draft_id, stage="activation", code=exc.code,
                    details={"message": str(exc)},
                )
            operational_log(
                "mutation", "activation_failed", level="error",
                chat_id=str(chat_id), draft_id=str(draft_id), error_code=exc.code,
            )
            raise
        except MountConflict as exc:
            self._record_failure(
                chat_id, draft_id, stage="activation", code="mount_conflict",
                details={"message": str(exc)},
            )
            operational_log(
                "mutation", "activation_failed", level="error",
                chat_id=str(chat_id), draft_id=str(draft_id), error_code="mount_conflict",
            )
            raise
        except Exception as exc:
            self._record_failure(
                chat_id, draft_id, stage="activation", code="activation_exception",
                details={"type": type(exc).__name__, "message": str(exc)},
            )
            operational_log(
                "mutation", "activation_failed", level="error",
                chat_id=str(chat_id), draft_id=str(draft_id),
                error_code="activation_exception", error_type=type(exc).__name__,
            )
            raise

    async def propose_activate(self, chat_id: str, **kwargs: Any) -> dict[str, Any]:
        record, _loaded, authority = self._require_write_authority(chat_id)
        expected = int(record.identity.mount_revision or 0)
        draft = self.propose(
            chat_id,
            expected_authority_revision=authority.revision,
            **kwargs,
        )
        return await self.activate(
            chat_id,
            draft["draft_id"],
            expected_mount_revision=expected,
            expected_authority_revision=authority.revision,
        )

    async def invoke_activation(
        self,
        context: InvocationContext,
        activation: Mapping[str, Any],
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Run an adaptation's first real call in the activation transaction flow."""

        chat_id = str(context.chat_id or "")
        draft_id = str(activation.get("draft_id") or "")
        slot_id = str(activation.get("slot_id") or "")
        version = int(activation.get("slot_version") or 0)
        if not chat_id or not draft_id or not slot_id or version <= 0:
            raise MutationError(
                "activation_receipt_invalid",
                "activated adaptation is missing its draft, slot, or version",
            )
        draft = self._draft(chat_id, draft_id)
        call_arguments = dict(arguments or {})
        declared_kind = str(draft.get("declared_kind") or "")
        if declared_kind == "method":
            call_arguments["__mutation_method"] = str(draft.get("alias") or "")
        call_context = replace(
            context,
            nested_call_id=f"mutation-first-use-{uuid.uuid4().hex}",
            mount_revision=int(
                activation.get("mount_revision") or context.mount_revision or 0
            ),
            slot_id=slot_id,
            slot_version=version,
        )
        receipt = await self.broker.invoke_name(
            MUTATION_HANDLER,
            {"arguments": call_arguments},
            call_context,
        )
        if not receipt.ok:
            message = receipt.error.message if receipt.error else receipt.status
            raise ToolError(message)
        result = receipt.result_value
        namespace = "tools"
        if declared_kind in {"method", "mutate"}:
            _category, _position, base = _slot(
                self.catalog_repository.load(str(draft["catalog_release_id"])),
                slot_id,
            )
            if str(base.get("projection") or "seeds") == "object":
                namespace = str(base.get("bundle") or "tools")
        alias = str(draft.get("alias") or "")
        return {
            "ok": True,
            "adapted": f"{namespace}.{alias}" if namespace != "tools" else f"tools.{alias}",
            "slot_id": slot_id,
            "slot_version": version,
            "mount_revision": int(
                activation.get("mount_revision") or context.mount_revision or 0
            ),
            "probation": True,
            "invocation": dict(activation.get("invocation") or {}),
            "result": result,
        }

    async def mutate(
        self,
        chat_id: str,
        *,
        slot: str,
        source: str,
        tests: list[Any],
        purpose: str = "",
    ) -> dict[str, Any]:
        """Atomically mutate one occupied direct seed with its exact contract."""

        if not isinstance(tests, list) or not tests:
            raise MutationError(
                "tests_required", "mutate requires at least one test case"
            )
        record, loaded, _authority = self._require_write_authority(chat_id)
        slot_reference = _selected_slot_reference(record, slot)
        _category, _position, base = _slot(loaded, slot_reference)
        slot_id = f"{loaded.release_id}/{_category}/{_position}"
        bindings = list(base.get("bindings") or ())
        if (
            str(base.get("status") or "") != "seed"
            or str(base.get("projection") or "seeds") != "seeds"
            or len(bindings) != 1
        ):
            active = self._active_slot_draft(chat_id, slot_id)
            if (
                str(base.get("status") or "") == "vacant"
                and active is not None
                and str(active.get("declared_kind") or "") in {"create", "revise"}
            ):
                return await self.propose_activate(
                    chat_id,
                    kind="revise",
                    slot=slot_reference,
                    parent=slot_reference,
                    alias=str(active.get("alias") or ""),
                    purpose=str(
                        purpose
                        or active.get("purpose")
                        or f"Revise {active.get('alias')}"
                    ),
                    schema=dict(active.get("schema") or {}),
                    source=str(source),
                    tests=list(tests),
                )
            raise MutationError(
                "atomic_contract_unavailable",
                "mutate requires an occupied direct seed or active synthesized "
                "direct tool; use staged authoring for a coherent object",
            )
        binding = dict(bindings[0])
        alias = str(base.get("primary_alias") or binding.get("alias") or "")
        if not alias:
            raise MutationError(
                "atomic_contract_unavailable", "occupied seed has no stable alias"
            )
        return await self.propose_activate(
            chat_id,
            kind="mutate",
            slot=slot_reference,
            parent=slot_reference,
            alias=alias,
            purpose=str(purpose or binding.get("description") or f"Mutate {alias}"),
            schema=_atomic_schema_from_params(binding.get("params") or {}),
            source=str(source),
            tests=list(tests),
        )

    async def synthesize(
        self,
        chat_id: str,
        *,
        slot: str,
        alias: str,
        purpose: str,
        schema: Any,
        source: str,
        tests: list[Any],
    ) -> dict[str, Any]:
        """Atomically create one tested tool in an explicit vacant slot."""

        if not isinstance(tests, list) or not tests:
            raise MutationError(
                "tests_required", "synthesize requires at least one test case"
            )
        return await self.propose_activate(
            chat_id,
            kind="create",
            slot=str(slot),
            alias=str(alias),
            purpose=str(purpose),
            schema=schema,
            source=str(source),
            tests=list(tests),
            parent=None,
        )

    def _active_row(self, chat_id: str, slot_id: str, version: int) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT a.version, a.draft_id, d.* FROM astb_activation a "
                "JOIN mutation_draft d ON d.draft_id=a.draft_id "
                "WHERE a.chat_id=? AND a.slot_id=? AND a.version=? AND a.active=1",
                (str(chat_id), str(slot_id), int(version)),
            ).fetchone()
        if row is None:
            raise MutationError("stale_mutation_ref", "mutation slot/version is no longer active")
        return self._draft(chat_id, str(row["draft_id"]))

    def _active_method_row(
        self,
        chat_id: str,
        slot_id: str,
        version: int,
        alias: str,
    ) -> dict[str, Any]:
        """Resolve one method from the active object-version ancestry."""

        self._active_row(chat_id, slot_id, version)
        with self._lock, self._connect() as conn:
            current = int(version)
            seen: set[int] = set()
            while current > 0 and current not in seen:
                seen.add(current)
                row = conn.execute(
                    "SELECT v.previous_version, d.draft_id, d.declared_kind, "
                    "d.alias FROM astb_slot_version v JOIN mutation_draft d "
                    "ON d.draft_id=v.draft_id WHERE v.chat_id=? AND "
                    "v.slot_id=? AND v.version=?",
                    (str(chat_id), str(slot_id), current),
                ).fetchone()
                if row is None:
                    break
                kind = str(row["declared_kind"] or "")
                if kind in {"method", "mutate"} and str(row["alias"] or "") == str(alias):
                    return self._draft(chat_id, str(row["draft_id"]))
                if kind == "mutate":
                    break
                current = int(row["previous_version"] or 0)
        raise MutationError(
            "stale_mutation_ref",
            f"active object mutation no longer provides method {alias!r}",
        )

    async def invoke(self, context: InvocationContext, arguments: dict[str, Any]) -> Any:
        if not context.chat_id or not context.slot_id or int(context.slot_version) <= 0:
            raise ToolError("mutation invocation lacks an admitted slot version")
        raw_arguments = dict(arguments or {})
        method_alias = str(raw_arguments.pop("__mutation_method", "") or "")
        draft = (
            self._active_method_row(
                context.chat_id,
                context.slot_id,
                context.slot_version,
                method_alias,
            )
            if method_alias
            else self._active_row(
                context.chat_id, context.slot_id, context.slot_version
            )
        )
        clean_arguments = _normalize_schema_value(
            raw_arguments, draft["schema"], "arguments", coerce=False
        )
        source = self.artifact_store.read_bytes_scoped(
            draft["source_ref"], context.chat_id
        ).decode("utf-8")
        _record, loaded, _authority = self._require_recovery_authority(
            context.chat_id
        )
        contracts = self._proxy_contracts(
            loaded,
            context.chat_id,
            exclude_slot_id=str(context.slot_id),
            exclude_proxy=self._draft_target_proxy(loaded, draft),
        )

        async def live_call(name: str, args: dict[str, Any], request_id: str) -> dict[str, Any]:
            contract = contracts.get(str(name))
            if contract is None:
                return {"ok": False, "error": f"proxy is unavailable: {name}"}
            raw_ref = dict(contract.get("ref") or {})
            ref = CapabilityRef(
                capability_id=str(raw_ref.get("capability_id") or ""),
                schema_revision=str(raw_ref.get("schema_revision") or ""),
                handler_revision=str(raw_ref.get("handler_revision") or ""),
                catalog_release_id=str(raw_ref.get("catalog_release_id") or ""),
                slot_id=str(raw_ref.get("slot_id") or ""),
                slot_version=int(raw_ref.get("slot_version") or 0),
            )
            nested_context = replace(
                context,
                nested_call_id=str(request_id),
                slot_id=ref.slot_id,
                slot_version=ref.slot_version,
            )
            call_arguments = {
                **dict(args or {}),
                **dict(contract.get("fixed_arguments") or {}),
            }
            if bool(contract.get("argument_envelope")):
                call_arguments = {"arguments": call_arguments}
            receipt = await self.broker.invoke(
                ref, call_arguments, nested_context
            )
            if not receipt.ok:
                return {
                    "ok": False,
                    "error": receipt.error.message if receipt.error else receipt.status,
                    "receipt_id": receipt.receipt_id,
                }
            return {
                "ok": True, "result": json_safe(receipt.result_value),
                "receipt_id": receipt.receipt_id,
            }

        started = time.perf_counter()
        status = "ok"
        report: dict[str, Any]
        try:
            report = await self.worker.run(
                {
                    "mode": "execute", "source": source,
                    "arguments": clean_arguments,
                    "proxy_contracts": self._worker_proxy_contracts(contracts),
                    "workspace_roots": list(context.workspace_root_ids),
                },
                proxy_call=live_call,
            )
            result = report.get("result")
        except MutationWorkerError as exc:
            status = "error"
            report = {"error": {"code": exc.code, "message": str(exc)}}
            duration_ms = round((time.perf_counter() - started) * 1000, 3)
            with self._lock, self._connect() as conn:
                conn.execute(
                    "INSERT INTO mutation_invocation(invocation_id, chat_id, run_id, "
                    "outer_tool_call_id, cell_execution_id, nested_call_id, "
                    "kernel_generation, slot_id, version, draft_id, status, "
                    "observed_calls_json, result_sha256, duration_ms, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "minvoke_" + uuid.uuid4().hex,
                        context.chat_id,
                        context.run_id,
                        context.outer_tool_call_id,
                        context.cell_execution_id,
                        context.nested_call_id,
                        context.kernel_generation,
                        context.slot_id,
                        int(context.slot_version),
                        draft["draft_id"],
                        status,
                        "[]",
                        _digest(report["error"]),
                        duration_ms,
                        _now(),
                    ),
                )
            self._record_failure(
                context.chat_id, draft["draft_id"], stage="invocation", code=exc.code,
                details={"duration_ms": duration_ms},
            )
            await self._probation_result(
                context.chat_id, context.slot_id, context.slot_version,
                ok=False, mechanical=exc.code in {
                    "worker_timeout", "worker_crash", "worker_protocol",
                    "worker_frame_quota", "worker_unavailable",
                    "candidate_contract_error",
                },
            )
            raise ToolError(f"session mutation failed [{exc.code}]: {exc}") from exc
        self._merge_observed_dependencies(
            context.chat_id,
            draft["draft_id"],
            contracts,
            list(report.get("observed_calls") or []),
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO mutation_invocation(invocation_id, chat_id, run_id, "
                "outer_tool_call_id, cell_execution_id, nested_call_id, "
                "kernel_generation, slot_id, version, draft_id, status, "
                "observed_calls_json, result_sha256, duration_ms, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "minvoke_" + uuid.uuid4().hex,
                    context.chat_id,
                    context.run_id,
                    context.outer_tool_call_id,
                    context.cell_execution_id,
                    context.nested_call_id,
                    context.kernel_generation,
                    context.slot_id,
                    int(context.slot_version),
                    draft["draft_id"],
                    status,
                    _stable(report.get("observed_calls") or []),
                    _digest(result),
                    float(report.get("duration_ms") or (
                        (time.perf_counter() - started) * 1000
                    )),
                    _now(),
                ),
            )
        await self._probation_result(
            context.chat_id, context.slot_id, context.slot_version,
            ok=True, mechanical=False,
        )
        return result

    async def _probation_result(
        self, chat_id: str, slot_id: str, version: int, *, ok: bool, mechanical: bool
    ) -> None:
        # Invocation evidence is persisted by ``invoke`` before this hook. Off
        # and operator-frozen chats keep serving already activated overlays,
        # but their probation state machine must not advance until write
        # authority is effective again.
        try:
            _record, _loaded, authority = self._require_write_authority(chat_id)
        except MutationError as exc:
            if exc.code in {
                "mutation_write_disabled",
                "mutation_authority_changed",
                "mutation_frozen",
            }:
                return
            raise
        pending_rollback = {
            "mechanical_failure",
            "probation_call_quota_failed",
            "rollback_failed",
        }
        previous_status = ""
        status = ""
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._assert_write_authority_in_conn(
                    conn, chat_id, expected_revision=authority.revision
                )
            except MutationError as exc:
                conn.rollback()
                if exc.code in {
                    "mutation_write_disabled",
                    "mutation_authority_changed",
                    "mutation_frozen",
                }:
                    return
                raise
            row = conn.execute(
                "SELECT * FROM mutation_probation WHERE chat_id=? AND slot_id=?",
                (str(chat_id), str(slot_id)),
            ).fetchone()
            if row is None or int(row["version"]) != int(version):
                conn.rollback()
                return
            previous_status = str(row["status"])
            status = previous_status
            if str(row["status"]) == "passed":
                # LKG is a completed probation state. Invocation evidence and
                # failures were already persisted by ``invoke``; a later bad
                # argument, task-specific semantic error, or transient worker
                # failure must not silently demote and unmount a proven tool.
                # Rollback remains an explicit recovery operation.
                conn.rollback()
                return
            if str(row["status"]) in pending_rollback:
                # A prior rollback was paused by Off/freeze or failed visibly.
                # Re-attempt it below without treating this invocation as new
                # probation evidence.
                should_rollback = True
                conn.rollback()
            else:
                calls = int(row["calls"]) + 1
                successes = int(row["successful_calls"]) + (1 if ok else 0)
                semantic = int(row["semantic_errors"]) + (1 if not ok and not mechanical else 0)
                mechanics = int(row["mechanical_errors"]) + (1 if mechanical else 0)
                should_rollback = False
                if ok and successes >= 2:
                    status = "passed"
                    conn.execute(
                        "UPDATE astb_slot_version SET status='lkg' WHERE chat_id=? "
                        "AND slot_id=? AND version=?", (str(chat_id), str(slot_id), int(version)),
                    )
                    conn.execute(
                        "UPDATE mutation_draft SET status='active' WHERE draft_id=?",
                        (str(row["draft_id"]),),
                    )
                elif mechanical:
                    status = "mechanical_failure"
                    should_rollback = True
                elif not ok:
                    status = "semantic_failure_visible"
                if status != "passed" and calls >= MAX_PROBATION_CALLS:
                    status = "probation_call_quota_failed"
                    should_rollback = True
                conn.execute(
                    "UPDATE mutation_probation SET status=?, calls=?, successful_calls=?, "
                    "semantic_errors=?, mechanical_errors=?, updated_at=? WHERE chat_id=? AND slot_id=?",
                    (status, calls, successes, semantic, mechanics, _now(), str(chat_id), str(slot_id)),
                )
                conn.commit()
        if status != previous_status:
            operational_log(
                "mutation",
                "probation_transition",
                level=(
                    "info" if status == "passed"
                    else "error" if status in {
                        "mechanical_failure", "probation_call_quota_failed",
                        "rollback_failed",
                    }
                    else "warn"
                ),
                chat_id=str(chat_id),
                slot_id=str(slot_id),
                slot_version=int(version),
                status=status,
            )
        if should_rollback:
            try:
                self.rollback(
                    chat_id,
                    slot_id,
                    to_version=int(row["previous_version"]),
                    expected_authority_revision=authority.revision,
                )
            except Exception as exc:
                if isinstance(exc, MutationError) and exc.code in {
                    "mutation_write_disabled",
                    "mutation_authority_changed",
                    "mutation_frozen",
                }:
                    return
                # A concurrent transition may already have removed the failing
                # overlay.  Otherwise the rollback failure is safety-critical:
                # keep it visible instead of reporting a completed probation
                # transition while the bad version remains mounted.
                with self._lock, self._connect() as conn:
                    active = conn.execute(
                        "SELECT version FROM astb_activation WHERE chat_id=? "
                        "AND slot_id=? AND active=1",
                        (str(chat_id), str(slot_id)),
                    ).fetchone()
                    failed_version_still_active = (
                        active is not None and int(active["version"]) == int(version)
                    )
                    if failed_version_still_active:
                        conn.execute(
                            "UPDATE mutation_probation SET status='rollback_failed', "
                            "updated_at=? WHERE chat_id=? AND slot_id=? AND version=?",
                            (_now(), str(chat_id), str(slot_id), int(version)),
                        )
                        conn.commit()
                if not failed_version_still_active:
                    return
                self._record_failure(
                    chat_id, str(row["draft_id"]), stage="probation_rollback",
                    code="probation_rollback_failed",
                    details={"slot_id": str(slot_id), "version": int(version)},
                )
                operational_log(
                    "mutation", "probation_rollback_failed", level="error",
                    chat_id=str(chat_id), slot_id=str(slot_id),
                    slot_version=int(version), error_type=type(exc).__name__,
                )
                raise MutationError(
                    "probation_rollback_failed",
                    "candidate probation failed and its overlay could not be rolled back",
                    slot_id=str(slot_id), version=int(version),
                ) from exc

    def _transition(
        self, chat_id: str, slot_reference: str, target_version: int,
        *, action: str, expected_mount_revision: int | None = None,
        expected_authority_revision: int | None = None,
    ) -> dict[str, Any]:
        record, loaded, authority = self._require_recovery_authority(
            chat_id, expected_revision=expected_authority_revision
        )
        normalized_slot = _selected_slot_reference(record, slot_reference)
        category_id, position, _ = _slot(loaded, normalized_slot)
        slot_id = f"{loaded.release_id}/{category_id}/{position}"
        expected = (
            int(record.identity.mount_revision or 0)
            if expected_mount_revision is None else int(expected_mount_revision)
        )
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._assert_recovery_authority_in_conn(
                conn, chat_id, expected_revision=authority.revision
            )
            runtime = conn.execute(
                "SELECT * FROM astb_chat_runtime WHERE chat_id=?", (str(chat_id),)
            ).fetchone()
            if runtime is None or int(runtime["mount_revision"] or 0) != expected:
                raise MountConflict("mutation transition CAS failed")
            current = conn.execute(
                "SELECT * FROM astb_activation WHERE chat_id=? AND slot_id=? AND active=1",
                (str(chat_id), slot_id),
            ).fetchone()
            current_version = int(current["version"] if current else 0)
            if target_version == current_version:
                conn.rollback()
                return {"ok": True, "already_selected": True, "slot_id": slot_id,
                        "slot_version": target_version, "mount_revision": expected}
            target = None
            if target_version:
                target = conn.execute(
                    "SELECT * FROM astb_slot_version WHERE chat_id=? AND slot_id=? AND version=?",
                    (str(chat_id), slot_id, int(target_version)),
                ).fetchone()
                if target is None or target["status"] == "garbage_collected":
                    raise MutationError("unknown_version", "requested mutation version is unavailable")
            replacement = None
            if target is not None:
                replacement_row = conn.execute(
                    "SELECT slot_id, catalog_release_id, category_id, position, "
                    "declared_kind, alias FROM mutation_draft "
                    "WHERE chat_id=? AND draft_id=?",
                    (str(chat_id), str(target["draft_id"])),
                ).fetchone()
                if replacement_row is None:
                    raise MutationError(
                        "unknown_version",
                        "requested mutation version has no durable draft",
                    )
                replacement = dict(replacement_row)
            self._assert_projected_namespace_in_conn(
                conn,
                chat_id,
                loaded,
                target_slot_id=slot_id,
                replacement=replacement,
            )
            next_mount = expected + 1
            overlay = int(runtime["overlay_revision"] or 0) + 1
            draft_id = str(target["draft_id"] if target else "")
            receipt = self._receipt_in_conn(conn, chat_id, draft_id, action, {
                "slot_id": slot_id, "from_version": current_version,
                "to_version": int(target_version), "to_mount_revision": next_mount,
            })
            conn.execute(
                "UPDATE astb_activation SET active=0 WHERE chat_id=? AND slot_id=? AND active=1",
                (str(chat_id), slot_id),
            )
            if target is not None:
                conn.execute(
                    "INSERT INTO astb_activation(activation_id, chat_id, slot_id, version, "
                    "draft_id, active, action, mount_revision, receipt_digest, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                    (
                        "activation_" + uuid.uuid4().hex, str(chat_id), slot_id,
                        int(target_version), draft_id, action, next_mount,
                        receipt["receipt_digest"], now,
                    ),
                )
            conn.execute(
                "DELETE FROM mutation_probation WHERE chat_id=? AND slot_id=?",
                (str(chat_id), slot_id),
            )
            conn.execute(
                "UPDATE astb_chat_runtime SET mount_revision=?, overlay_revision=?, "
                "updated_at=?, version=version+1 WHERE chat_id=?",
                (next_mount, overlay, now, str(chat_id)),
            )
            conn.execute(
                "INSERT INTO astb_mount_history(chat_id, mount_revision, catalog_release_id, "
                "category_id, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(chat_id), next_mount, loaded.release_id,
                    str(runtime["selected_category_id"] or ""), "mutation_" + action, now,
                ),
            )
            conn.commit()
        return {
            "ok": True, "slot_id": slot_id, "slot_version": int(target_version),
            "previous_slot_version": current_version, "mount_revision": next_mount,
            "overlay_revision": overlay, "receipt": receipt,
        }

    def rollback(
        self, chat_id: str, slot: str, *, to_version: int | None = None,
        expected_mount_revision: int | None = None,
        expected_authority_revision: int | None = None,
    ) -> dict[str, Any]:
        record, loaded, authority = self._require_recovery_authority(
            chat_id, expected_revision=expected_authority_revision
        )
        normalized_slot = _selected_slot_reference(record, slot)
        category_id, position, _ = _slot(loaded, normalized_slot)
        slot_id = f"{loaded.release_id}/{category_id}/{position}"
        with self._lock, self._connect() as conn:
            current = conn.execute(
                "SELECT a.version, v.previous_version FROM astb_activation a "
                "JOIN astb_slot_version v ON v.chat_id=a.chat_id AND v.slot_id=a.slot_id "
                "AND v.version=a.version WHERE a.chat_id=? AND a.slot_id=? AND a.active=1",
                (str(chat_id), slot_id),
            ).fetchone()
        if current is None:
            raise MutationError("not_mutated", "slot has no active session mutation")
        target = int(current["previous_version"] if to_version is None else to_version)
        result = self._transition(
            chat_id, slot_id, target, action="rollback",
            expected_mount_revision=expected_mount_revision,
            expected_authority_revision=authority.revision,
        )
        operational_log(
            "mutation", "rolled_back", level="warn",
            chat_id=str(chat_id), slot_id=result.get("slot_id"),
            slot_version=result.get("slot_version"),
            previous_slot_version=result.get("previous_slot_version"),
            mount_revision=result.get("mount_revision"),
        )
        return result

    def reset_slot(
        self,
        chat_id: str,
        slot: str,
        *,
        expected_mount_revision: int | None = None,
        expected_authority_revision: int | None = None,
    ) -> dict[str, Any]:
        result = self._transition(
            chat_id, slot, 0, action="reset",
            expected_mount_revision=expected_mount_revision,
            expected_authority_revision=expected_authority_revision,
        )
        operational_log(
            "mutation", "slot_reset", level="warn",
            chat_id=str(chat_id), slot_id=result.get("slot_id"),
            previous_slot_version=result.get("previous_slot_version"),
            mount_revision=result.get("mount_revision"),
        )
        return result

    def reset_all(self, chat_id: str) -> dict[str, Any]:
        record, _loaded, authority = self._require_recovery_authority(chat_id)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT slot_id FROM astb_activation WHERE chat_id=? AND active=1 "
                "ORDER BY slot_id", (str(chat_id),),
            ).fetchall()
        results = []
        expected = int(record.identity.mount_revision or 0)
        for row in rows:
            result = self.reset_slot(
                chat_id,
                str(row["slot_id"]),
                expected_mount_revision=expected,
                expected_authority_revision=authority.revision,
            )
            expected = int(result["mount_revision"])
            results.append(result)
        operational_log(
            "mutation", "all_reset", level="warn",
            chat_id=str(chat_id), reset_slots=len(results),
        )
        return {"ok": True, "reset_slots": len(results), "results": results}

    def active_overlays(
        self,
        chat_id: str,
        loaded: LoadedCatalog,
        *,
        mount_revision: int,
        condition_flags: Mapping[str, bool] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, CapabilityRef]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT a.version, d.* FROM astb_activation a "
                "JOIN mutation_draft d ON d.draft_id=a.draft_id "
                "WHERE a.chat_id=? AND a.active=1 ORDER BY d.category_id, d.position",
                (str(chat_id),),
            ).fetchall()
            probation_rows = conn.execute(
                "SELECT * FROM mutation_probation WHERE chat_id=?",
                (str(chat_id),),
            ).fetchall()
            chains: dict[str, list[dict[str, Any]]] = {}
            for active in rows:
                if str(active["declared_kind"]) != "method":
                    continue
                slot_id = str(active["slot_id"])
                version = int(active["version"])
                seen: set[int] = set()
                chain: list[dict[str, Any]] = []
                while version > 0 and version not in seen:
                    seen.add(version)
                    version_row = conn.execute(
                        "SELECT v.version AS active_version, v.previous_version, d.* "
                        "FROM astb_slot_version v JOIN mutation_draft d "
                        "ON d.draft_id=v.draft_id WHERE v.chat_id=? AND "
                        "v.slot_id=? AND v.version=?",
                        (str(chat_id), slot_id, version),
                    ).fetchone()
                    if version_row is None:
                        break
                    decoded = dict(version_row)
                    chain.append(decoded)
                    version = int(decoded.get("previous_version") or 0)
                chains[slot_id] = chain
        probation = {str(row["slot_id"]): dict(row) for row in probation_rows}
        descriptors: list[dict[str, Any]] = []
        refs: dict[str, CapabilityRef] = {}
        for row in rows:
            if str(row["catalog_release_id"]) != loaded.release_id:
                continue
            params = json.loads(row["params_json"])
            version = int(row["version"])
            ref = self.broker.ref_for_name(
                MUTATION_HANDLER,
                catalog_release_id=loaded.release_id,
                slot_id=str(row["slot_id"]),
                slot_version=version,
            )
            category_id = str(row["category_id"])
            category_row = next((
                item for item in (loaded.document.get("categories") or ())
                if str(item.get("category_id") or "") == category_id
            ), {})
            mount_mode = str(category_row.get("mount_mode") or "selected")
            effect_class = max(
                (
                    str(item.get("effect_class") or "pure")
                    for item in json.loads(row["dependencies_json"])
                ),
                key=lambda value: _EFFECT_RANK.get(value, 99),
                default="pure",
            )
            base: dict[str, Any] = {}
            projection = "seeds"
            declared_kind = str(row["declared_kind"])
            if declared_kind in {"mutate", "method"}:
                _category, _position, base = _slot(loaded, str(row["slot_id"]))
                projection = str(base.get("projection") or "seeds")
                if projection not in {"seeds", "object"}:
                    raise MutationError(
                        "retired_projection",
                        f"slot uses retired projection: {projection!r}",
                    )
                namespace = (
                    "tools"
                    if projection == "seeds"
                    else str(base.get("bundle") or "")
                )
            else:
                namespace = "tools"

            if declared_kind == "method":
                if projection != "object":
                    raise MutationError(
                        "shape_mismatch",
                        "active method mutation no longer owns an object slot",
                    )
                binding_rows = [
                    dict(item) for item in (base.get("bindings") or ())
                    if isinstance(item, Mapping)
                ]
                if len(binding_rows) != 1:
                    raise MutationError(
                        "atomic_contract_unavailable",
                        "method mutation lost its coherent object transport",
                    )
                transport = binding_rows[0]
                if str(transport.get("tool_name") or "") not in set(
                    self.enabled_resolver() or ()
                ):
                    continue
                base_ref = CapabilityRef(
                    capability_id=str(transport.get("capability_id") or ""),
                    schema_revision=str(transport.get("schema_revision") or ""),
                    handler_revision=str(transport.get("handler_revision") or ""),
                    catalog_release_id=loaded.release_id,
                    slot_id=str(row["slot_id"]),
                    slot_version=0,
                )
                methods: dict[str, dict[str, Any]] = {}
                for raw_method in base.get("methods") or ():
                    if not isinstance(raw_method, Mapping):
                        continue
                    method = dict(raw_method)
                    condition = str(method.get("condition") or "")
                    if condition and not bool((condition_flags or {}).get(condition)):
                        continue
                    alias = str(method.get("alias") or "")
                    if not alias:
                        continue
                    methods[alias] = {
                        **transport,
                        **method,
                        "kind": "mounted_object_method",
                        "alias": alias,
                        "namespace": namespace,
                        "qualified_alias": f"{namespace}.{alias}",
                        "source_alias": str(transport.get("alias") or ""),
                        "fixed_arguments": {
                            "operation": str(method.get("operation") or alias),
                        },
                        "catalog_release_id": loaded.release_id,
                        "category_id": category_id,
                        "position": int(row["position"]),
                        "slot_id": str(row["slot_id"]),
                        "slot_version": 0,
                        "mount_revision": int(mount_revision),
                    }
                for prior in reversed(chains.get(str(row["slot_id"]), [])):
                    prior_kind = str(prior.get("declared_kind") or "")
                    if prior_kind not in {"mutate", "method"}:
                        continue
                    if prior_kind == "mutate":
                        methods.clear()
                    prior_alias = str(prior.get("alias") or "")
                    prior_params = json.loads(str(prior.get("params_json") or "{}"))
                    dependencies = json.loads(
                        str(prior.get("dependencies_json") or "[]")
                    )
                    inherited_effect = str(
                        (methods.get(prior_alias) or {}).get("effect_class") or "pure"
                    )
                    prior_effect = max(
                        [inherited_effect] + [
                            str(item.get("effect_class") or "pure")
                            for item in dependencies
                            if isinstance(item, Mapping)
                        ],
                        key=lambda value: _EFFECT_RANK.get(value, 99),
                    )
                    methods[prior_alias] = {
                        "kind": "mounted_object_method",
                        "alias": prior_alias,
                        "namespace": namespace,
                        "qualified_alias": f"{namespace}.{prior_alias}",
                        "bundle": str(base.get("bundle") or namespace),
                        "mount_mode": mount_mode,
                        "ref_id": ref.opaque_id,
                        "capability_id": ref.capability_id,
                        "schema_revision": ref.schema_revision,
                        "handler_revision": ref.handler_revision,
                        "effect_class": prior_effect,
                        "description": str(prior.get("purpose") or ""),
                        "params": prior_params,
                        "signature": binding_signature(prior_alias, prior_params),
                        "catalog_release_id": loaded.release_id,
                        "category_id": category_id,
                        "position": int(row["position"]),
                        "slot_id": str(row["slot_id"]),
                        "slot_version": version,
                        "mount_revision": int(mount_revision),
                        "fixed_arguments": {"__mutation_method": prior_alias},
                        "argument_envelope": True,
                        "session_local": True,
                        "probation": dict(
                            probation.get(str(row["slot_id"]), {})
                        ),
                    }
                descriptors.append({
                    "kind": "mounted_object",
                    "name": namespace,
                    "alias": namespace,
                    "namespace": namespace,
                    "qualified_alias": namespace,
                    "bundle": str(base.get("bundle") or namespace),
                    "summary": (
                        f"Session-adapted {namespace} object in slot {row['slot_id']}"
                    ),
                    "mount_mode": mount_mode,
                    "methods": list(methods.values()),
                    "method_count": len(methods),
                    "catalog_release_id": loaded.release_id,
                    "category_id": category_id,
                    "position": int(row["position"]),
                    "slot_id": str(row["slot_id"]),
                    "slot_version": version,
                    "mount_revision": int(mount_revision),
                    "session_local": True,
                })
                refs[ref.opaque_id] = ref
                refs[base_ref.opaque_id] = base_ref
                continue

            operation = {
                "alias": str(row["alias"]),
                "namespace": namespace,
                "qualified_alias": (
                    str(row["alias"])
                    if namespace == "tools"
                    else f"{namespace}.{row['alias']}"
                ),
                "bundle": str(base.get("bundle") or "session_tool"),
                "mount_mode": mount_mode,
                "ref_id": ref.opaque_id,
                "capability_id": ref.capability_id,
                "schema_revision": ref.schema_revision,
                "handler_revision": ref.handler_revision,
                "effect_class": effect_class,
                "description": str(row["purpose"]),
                "params": params,
                "signature": binding_signature(str(row["alias"]), params),
                "catalog_release_id": loaded.release_id,
                "category_id": category_id,
                "position": int(row["position"]),
                "slot_id": str(row["slot_id"]),
                "slot_version": version,
                "mount_revision": int(mount_revision),
                "argument_envelope": True,
                "session_local": True,
                "declared_kind": declared_kind,
                "probation": dict(probation.get(str(row["slot_id"]), {})),
            }
            if (
                declared_kind == "mutate"
                and projection == "object"
            ):
                descriptors.append({
                    "kind": "mounted_object",
                    "name": namespace,
                    "alias": namespace,
                    "namespace": namespace,
                    "qualified_alias": namespace,
                    "bundle": str(base.get("bundle") or namespace),
                    "summary": (
                        f"Session-mutated {namespace} object replacing slot "
                        f"{row['slot_id']}"
                    ),
                    "mount_mode": mount_mode,
                    "methods": [{
                        **operation,
                        "kind": "mounted_object_method",
                    }],
                    "method_count": 1,
                    "catalog_release_id": loaded.release_id,
                    "category_id": category_id,
                    "position": int(row["position"]),
                    "slot_id": str(row["slot_id"]),
                    "slot_version": version,
                    "mount_revision": int(mount_revision),
                    "session_local": True,
                })
            else:
                descriptors.append(operation)
            refs[ref.opaque_id] = ref
        return descriptors, refs

    def status(self, chat_id: str, *, limit: int = 20) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            drafts = conn.execute(
                "SELECT draft_id, slot_id, declared_kind, alias, status, source_sha256, "
                "created_at, updated_at FROM mutation_draft WHERE chat_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (str(chat_id), max(1, min(int(limit), 100))),
            ).fetchall()
            active = conn.execute(
                "SELECT slot_id, version, draft_id, mount_revision, created_at "
                "FROM astb_activation WHERE chat_id=? AND active=1 ORDER BY slot_id",
                (str(chat_id),),
            ).fetchall()
            probation = conn.execute(
                "SELECT * FROM mutation_probation WHERE chat_id=? ORDER BY slot_id",
                (str(chat_id),),
            ).fetchall()
            failures = conn.execute(
                "SELECT failure_id, draft_id, stage, proposal_fingerprint, code, "
                "details_json, created_at FROM mutation_failure WHERE chat_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (str(chat_id), max(1, min(int(limit), 100))),
            ).fetchall()
        return {
            "schema": MUTATION_SCHEMA,
            "authority": self.authority_status(chat_id),
            "drafts": [dict(row) for row in drafts],
            "active": [dict(row) for row in active],
            "probation": [dict(row) for row in probation],
            "failures": [
                {**dict(row), "details": json.loads(row["details_json"])}
                for row in failures
            ],
            "quotas": {
                "failed_activations": MAX_FAILED_ACTIVATIONS,
                "identical_failure_breaker": IDENTICAL_FAILURE_BREAKER,
                "probation_calls": MAX_PROBATION_CALLS,
            },
            **self.worker.execution_status(),
        }

    def gc(self, chat_id: str, *, older_than_s: float = 7 * 24 * 3600) -> dict[str, Any]:
        cutoff = _now() - max(0.0, float(older_than_s))
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT v.chat_id, v.slot_id, v.version, v.draft_id FROM astb_slot_version v "
                "LEFT JOIN astb_activation a ON a.chat_id=v.chat_id AND a.slot_id=v.slot_id "
                "AND a.version=v.version AND a.active=1 WHERE v.chat_id=? AND a.activation_id IS NULL "
                "AND v.status NOT IN ('lkg','garbage_collected') AND v.created_at<? LIMIT 100",
                (str(chat_id), cutoff),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE astb_slot_version SET status='garbage_collected' WHERE "
                    "chat_id=? AND slot_id=? AND version=?",
                    (row["chat_id"], row["slot_id"], row["version"]),
                )
            conn.commit()
        return {"garbage_collected": len(rows)}

    def delete_chat(self, chat_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for table in (
                "mutation_invocation", "mutation_probation",
                "mutation_failure",
                "astb_activation", "astb_slot_version", "mutation_receipt",
                "mutation_draft",
            ):
                conn.execute(f"DELETE FROM {table} WHERE chat_id=?", (str(chat_id),))
            conn.commit()
