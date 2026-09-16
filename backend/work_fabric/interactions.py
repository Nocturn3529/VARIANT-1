"""One durable request/answer identity for live and deferred interaction."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import time
import uuid
from typing import Any, Mapping

from core_invariants import canonical_json, request_fingerprint
from .models import WorkActor, WorkConflict, WorkNotFound
from .scope import (
    WorkScope,
    append_json_scope_visibility,
    coerce_work_scope,
    work_scope_visible,
)


INTERACTION_SCHEMA = "variant1.work-interaction.v1"
INTERACTION_TERMINAL_STATES = frozenset({
    "answered", "skipped", "timed_out", "dismissed", "cancelled",
})
INTERACTION_STATES = frozenset({"open", *INTERACTION_TERMINAL_STATES})


def _json(value: Any) -> str:
    return canonical_json(value)


@dataclass(frozen=True, slots=True)
class InteractionRecord:
    interaction_id: str
    kind: str
    owner_kind: str
    owner_id: str
    status: str
    prompt: str
    schema: dict[str, Any] = field(default_factory=dict)
    response: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    scope: WorkScope = WorkScope()
    request_fingerprint: str = ""
    idempotency_key: str = ""
    version: int = 1
    expires_at: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0
    resolved_at: float = 0.0

    @property
    def terminal(self) -> bool:
        return self.status in INTERACTION_TERMINAL_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": INTERACTION_SCHEMA,
            "interaction_id": self.interaction_id,
            "kind": self.kind,
            "owner": {"kind": self.owner_kind, "id": self.owner_id},
            "status": self.status,
            "prompt": self.prompt,
            "input_schema": dict(self.schema),
            "response": self.response,
            "metadata": dict(self.metadata),
            "scope": self.scope.to_dict(include_empty=False),
            "idempotency_key": self.idempotency_key or None,
            "version": self.version,
            "expires_at": self.expires_at or None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "resolved_at": self.resolved_at or None,
        }

class InteractionService:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    @staticmethod
    def _from_row(row: Any) -> InteractionRecord:
        response_envelope = json.loads(str(row["response_json"] or "{}"))
        return InteractionRecord(
            interaction_id=str(row["interaction_id"]),
            kind=str(row["kind"]),
            owner_kind=str(row["owner_kind"]),
            owner_id=str(row["owner_id"]),
            status=str(row["status"]),
            prompt=str(row["prompt"]),
            schema=dict(json.loads(str(row["schema_json"] or "{}"))),
            response=(
                response_envelope.get("value")
                if isinstance(response_envelope, dict) else None
            ),
            metadata=dict(json.loads(str(row["metadata_json"] or "{}"))),
            scope=WorkScope.from_mapping(json.loads(str(row["scope_json"] or "{}"))),
            request_fingerprint=str(row["request_fingerprint"]),
            idempotency_key=str(row["idempotency_key"] or ""),
            version=int(row["version"]),
            expires_at=float(row["expires_at"] or 0),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            resolved_at=float(row["resolved_at"] or 0),
        )

    def create(
        self,
        *,
        kind: str,
        prompt: str,
        scope: WorkScope | Mapping[str, Any] | None,
        owner_kind: str,
        owner_id: str,
        schema: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        expires_at: float = 0.0,
        idempotency_key: str = "",
        interaction_id: str = "",
        actor: WorkActor = WorkActor(),
        correlation_id: str = "",
    ) -> InteractionRecord:
        clean_kind = str(kind or "question").strip()[:120]
        clean_prompt = str(prompt or "").strip()
        if not clean_prompt:
            raise ValueError("interaction prompt is required")
        if len(clean_prompt) > 20_000:
            raise ValueError("interaction prompt exceeds 20000 characters")
        owner_type = str(owner_kind or "run").strip()[:120]
        owner = str(owner_id or "unknown").strip()[:512]
        resolved = coerce_work_scope(scope)
        clean_schema = dict(schema or {})
        clean_metadata = dict(metadata or {})
        key = str(idempotency_key or "").strip()[:512]
        request = {
            "kind": clean_kind,
            "prompt": clean_prompt,
            "schema": clean_schema,
            "metadata": clean_metadata,
            "scope": resolved.to_dict(),
            "owner": {"kind": owner_type, "id": owner},
            "expires_at": float(expires_at or 0),
        }
        fingerprint = request_fingerprint("work.interaction.request", request)
        identity = str(interaction_id or "").strip()
        if not identity:
            identity = (
                "interaction_" + hashlib.sha256(
                    f"{owner_type}\0{owner}\0{clean_kind}\0{key}".encode("utf-8")
                ).hexdigest()[:32]
                if key else "interaction_" + uuid.uuid4().hex
            )
        now = time.time()
        with self.repository._write() as connection:
            existing = connection.execute(
                "SELECT * FROM work_interaction WHERE interaction_id=?",
                (identity,),
            ).fetchone()
            if existing is not None:
                record = self._from_row(existing)
                if record.request_fingerprint != fingerprint:
                    raise WorkConflict(
                        "interaction identity was reused for a different request"
                    )
                return record
            if key:
                existing = connection.execute(
                    "SELECT * FROM work_interaction WHERE owner_kind=? AND owner_id=? "
                    "AND kind=? AND idempotency_key=?",
                    (owner_type, owner, clean_kind, key),
                ).fetchone()
                if existing is not None:
                    record = self._from_row(existing)
                    if record.request_fingerprint != fingerprint:
                        raise WorkConflict(
                            "interaction idempotency key was reused for another request"
                        )
                    return record
            connection.execute(
                "INSERT INTO work_interaction(interaction_id,kind,owner_kind,owner_id,"
                "status,prompt,schema_json,response_json,metadata_json,scope_json,"
                "request_fingerprint,idempotency_key,version,expires_at,created_at,"
                "updated_at,resolved_at) VALUES (?,?,?,?,'open',?,?,'{}',?,?,?,?,1,?,?,?,NULL)",
                (
                    identity, clean_kind, owner_type, owner, clean_prompt,
                    _json(clean_schema), _json(clean_metadata),
                    _json(resolved.to_dict()), fingerprint, key,
                    (float(expires_at) or None), now, now,
                ),
            )
            self.repository._insert_event_tx(
                connection,
                aggregate_kind="interaction",
                aggregate_id=identity,
                aggregate_version=1,
                event_type="interaction.requested",
                scope=resolved,
                actor=actor,
                correlation_id=str(correlation_id or ""),
                payload={
                    "kind": clean_kind,
                    "owner": {"kind": owner_type, "id": owner},
                    "expires_at": float(expires_at or 0) or None,
                },
            )
            row = connection.execute(
                "SELECT * FROM work_interaction WHERE interaction_id=?",
                (identity,),
            ).fetchone()
        return self._from_row(row)

    def create_goal_input_tx(
        self,
        connection: Any,
        *,
        interaction_id: str,
        goal_id: str,
        step_id: str,
        prompt: str,
        schema: Mapping[str, Any] | None,
        scope: WorkScope,
        actor: WorkActor,
        correlation_id: str = "",
        now: float | None = None,
    ) -> InteractionRecord:
        """Create the canonical goal input while Goal holds the same DB tx."""

        at = float(time.time() if now is None else now)
        metadata = {
            "source": "goal",
            "title": "Goal input required",
            "goal_id": str(goal_id),
            "step_id": str(step_id),
        }
        request = {
            "kind": "goal_input",
            "prompt": str(prompt),
            "schema": dict(schema or {}),
            "metadata": metadata,
            "scope": scope.to_dict(),
            "owner": {"kind": "goal", "id": str(goal_id)},
            "expires_at": 0.0,
        }
        connection.execute(
            "INSERT INTO work_interaction(interaction_id,kind,owner_kind,owner_id,"
            "status,prompt,schema_json,response_json,metadata_json,scope_json,"
            "request_fingerprint,idempotency_key,version,expires_at,created_at,"
            "updated_at,resolved_at) VALUES (?,'goal_input','goal',?,'open',?,?,'{}',"
            "?,?,?,'',1,NULL,?,?,NULL)",
            (
                str(interaction_id), str(goal_id), str(prompt),
                _json(dict(schema or {})), _json(metadata), _json(scope.to_dict()),
                request_fingerprint("work.interaction.request", request), at, at,
            ),
        )
        self.repository._insert_event_tx(
            connection,
            aggregate_kind="interaction",
            aggregate_id=str(interaction_id),
            aggregate_version=1,
            event_type="interaction.requested",
            scope=scope,
            actor=actor,
            correlation_id=str(correlation_id or ""),
            payload={
                "kind": "goal_input",
                "owner": {"kind": "goal", "id": str(goal_id)},
                "step_id": str(step_id),
            },
            created_at=at,
        )
        row = connection.execute(
            "SELECT * FROM work_interaction WHERE interaction_id=?",
            (str(interaction_id),),
        ).fetchone()
        return self._from_row(row)

    def resolve_goal_input_tx(
        self,
        connection: Any,
        interaction_id: str,
        response: Any,
        *,
        expected_version: int,
        actor: WorkActor,
        correlation_id: str = "",
        now: float | None = None,
    ) -> InteractionRecord:
        """Answer canonical goal input inside Goal's completion transaction."""

        at = float(time.time() if now is None else now)
        row = connection.execute(
            "SELECT * FROM work_interaction WHERE interaction_id=?",
            (str(interaction_id),),
        ).fetchone()
        if row is None:
            raise WorkNotFound(f"unknown interaction: {interaction_id}")
        current = self._from_row(row)
        if current.status != "open" or current.version != int(expected_version):
            raise WorkConflict("interaction is no longer open at the expected version")
        version = current.version + 1
        connection.execute(
            "UPDATE work_interaction SET status='answered',response_json=?,version=?,"
            "updated_at=?,resolved_at=? WHERE interaction_id=? AND version=?",
            (
                _json({"value": response}), version, at, at,
                current.interaction_id, current.version,
            ),
        )
        self.repository._insert_event_tx(
            connection,
            aggregate_kind="interaction",
            aggregate_id=current.interaction_id,
            aggregate_version=version,
            event_type="interaction.answered",
            scope=current.scope,
            actor=actor,
            correlation_id=str(correlation_id or ""),
            payload={"status": "answered"},
            created_at=at,
        )
        updated = connection.execute(
            "SELECT * FROM work_interaction WHERE interaction_id=?",
            (current.interaction_id,),
        ).fetchone()
        return self._from_row(updated)

    def dismiss_goal_input_tx(
        self,
        connection: Any,
        interaction_id: str,
        *,
        expected_version: int,
        actor: WorkActor,
        correlation_id: str = "",
        now: float | None = None,
    ) -> InteractionRecord:
        """Dismiss canonical goal input inside the owning Goal transaction."""

        at = float(time.time() if now is None else now)
        row = connection.execute(
            "SELECT * FROM work_interaction WHERE interaction_id=?",
            (str(interaction_id),),
        ).fetchone()
        if row is None:
            raise WorkNotFound(f"unknown interaction: {interaction_id}")
        current = self._from_row(row)
        if current.status != "open" or current.version != int(expected_version):
            raise WorkConflict("interaction is no longer open at the expected version")
        version = current.version + 1
        connection.execute(
            "UPDATE work_interaction SET status='dismissed',response_json=?,version=?,"
            "updated_at=?,resolved_at=? WHERE interaction_id=? AND version=?",
            (
                _json({"value": {"skipped": True}}), version, at, at,
                current.interaction_id, current.version,
            ),
        )
        self.repository._insert_event_tx(
            connection,
            aggregate_kind="interaction",
            aggregate_id=current.interaction_id,
            aggregate_version=version,
            event_type="interaction.dismissed",
            scope=current.scope,
            actor=actor,
            correlation_id=str(correlation_id or ""),
            payload={"status": "dismissed", "reason": "user_skipped"},
            created_at=at,
        )
        updated = connection.execute(
            "SELECT * FROM work_interaction WHERE interaction_id=?",
            (current.interaction_id,),
        ).fetchone()
        return self._from_row(updated)

    def get(
        self,
        interaction_id: str,
        *,
        scope: WorkScope | Mapping[str, Any] | None = None,
    ) -> InteractionRecord:
        with self.repository._read() as connection:
            row = connection.execute(
                "SELECT * FROM work_interaction WHERE interaction_id=?",
                (str(interaction_id or ""),),
            ).fetchone()
        if row is None:
            raise WorkNotFound(f"unknown interaction: {interaction_id}")
        record = self._from_row(row)
        if scope is not None and not work_scope_visible(record.scope, scope):
            raise WorkNotFound(f"unknown interaction: {interaction_id}")
        return record

    def list(
        self,
        *,
        scope: WorkScope | Mapping[str, Any] | None = None,
        status: str = "",
        owner_kind: str = "",
        owner_id: str = "",
        limit: int = 200,
        kind: str = "",
        oldest_first: bool = False,
        chat_id: str | None = None,
    ) -> list[InteractionRecord]:
        clauses = ["1=1"]
        values: list[Any] = []
        if status:
            clauses.append("status=?")
            values.append(str(status))
        if kind:
            clauses.append("kind=?")
            values.append(str(kind))
        if owner_kind:
            clauses.append("owner_kind=?")
            values.append(str(owner_kind))
        if owner_id:
            clauses.append("owner_id=?")
            values.append(str(owner_id))
        if scope is not None:
            append_json_scope_visibility(clauses, values, "scope_json", scope)
        if chat_id is not None:
            clauses.append("COALESCE(json_extract(scope_json, '$.chat_id'),'')=?")
            values.append(str(chat_id))
        values.append(max(1, min(int(limit or 200), 1000)))
        with self.repository._read() as connection:
            rows = connection.execute(
                "SELECT * FROM work_interaction WHERE " + " AND ".join(clauses)
                + (" ORDER BY created_at ASC,interaction_id LIMIT ?" if oldest_first
                   else " ORDER BY created_at DESC,interaction_id LIMIT ?"),
                values,
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def resolve(
        self,
        interaction_id: str,
        *,
        status: str,
        response: Any = None,
        expected_version: int | None = None,
        actor: WorkActor = WorkActor(),
        correlation_id: str = "",
    ) -> InteractionRecord:
        target = str(status or "answered").strip()
        if target not in INTERACTION_TERMINAL_STATES:
            raise ValueError(f"invalid terminal interaction status: {target}")
        now = time.time()
        with self.repository._write() as connection:
            row = connection.execute(
                "SELECT * FROM work_interaction WHERE interaction_id=?",
                (str(interaction_id or ""),),
            ).fetchone()
            if row is None:
                raise WorkNotFound(f"unknown interaction: {interaction_id}")
            current = self._from_row(row)
            if current.terminal:
                if current.status == target and current.response == response:
                    return current
                raise WorkConflict("interaction is already resolved")
            if expected_version is not None and current.version != int(expected_version):
                raise WorkConflict(
                    f"interaction version changed ({current.version} != {expected_version})"
                )
            version = current.version + 1
            connection.execute(
                "UPDATE work_interaction SET status=?,response_json=?,version=?,"
                "updated_at=?,resolved_at=? WHERE interaction_id=? AND version=?",
                (
                    target, _json({"value": response}), version, now, now,
                    current.interaction_id, current.version,
                ),
            )
            self.repository._insert_event_tx(
                connection,
                aggregate_kind="interaction",
                aggregate_id=current.interaction_id,
                aggregate_version=version,
                event_type=f"interaction.{target}",
                scope=current.scope,
                actor=actor,
                correlation_id=str(correlation_id or ""),
                payload={"status": target},
            )
            updated = connection.execute(
                "SELECT * FROM work_interaction WHERE interaction_id=?",
                (current.interaction_id,),
            ).fetchone()
        return self._from_row(updated)

    async def wait(
        self,
        interaction_id: str,
        *,
        timeout_s: float | None = None,
        poll_interval_s: float = 0.1,
    ) -> InteractionRecord:
        deadline = (
            time.monotonic() + max(0.0, float(timeout_s))
            if timeout_s is not None else None
        )
        while True:
            record = await asyncio.to_thread(self.get, interaction_id)
            if record.terminal:
                return record
            if deadline is not None and time.monotonic() >= deadline:
                try:
                    return await asyncio.to_thread(
                        self.resolve,
                        interaction_id,
                        status="timed_out",
                        expected_version=record.version,
                    )
                except WorkConflict:
                    continue
            await asyncio.sleep(max(0.02, min(float(poll_interval_s), 1.0)))


__all__ = [
    "INTERACTION_SCHEMA",
    "INTERACTION_STATES",
    "INTERACTION_TERMINAL_STATES",
    "InteractionRecord",
    "InteractionService",
]
