"""Work-database repository for versioned user-facing artifacts."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Mapping

from core_invariants import canonical_json
from work_fabric.models import WorkActor
from work_fabric.scope import (
    WorkScope,
    append_json_scope_visibility,
    coerce_work_scope,
)

from .runtime_models import (
    ArtifactAliasRecord,
    ArtifactObjectRecord,
    ArtifactRenderRecord,
    ArtifactRevisionRecord,
    ArtifactRuntimeConflict,
    ArtifactRuntimeNotFound,
    ArtifactRuntimeValidationError,
    ArtifactValidationRecord,
)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(value: Any) -> str:
    return canonical_json(value)


def _object(value: str | None) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except Exception:
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}


def _list(value: str | None) -> list[Any]:
    try:
        decoded = json.loads(value or "[]")
    except Exception:
        return []
    return list(decoded) if isinstance(decoded, list) else []


def _text(value: Any, field: str, *, required: bool = False, limit: int = 1000) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise ArtifactRuntimeValidationError(f"{field} is required")
    if "\x00" in result or len(result) > limit:
        raise ArtifactRuntimeValidationError(f"{field} is invalid or too long")
    return result


class ArtifactRuntimeRepository:
    def __init__(self, work_repository: Any) -> None:
        self.work = work_repository
        self._initialize()

    def _initialize(self) -> None:
        # sqlite3.executescript owns its transaction boundary, so it cannot run
        # inside WorkRepository._write() (which has already begun IMMEDIATE).
        with self.work._write_lock:
            connection = self.work._connect()
            try:
                connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS work_artifact_revision_detail (
                    artifact_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    parent_revision INTEGER NOT NULL DEFAULT 0,
                    source_ref TEXT NOT NULL DEFAULT '',
                    manifest_ref TEXT NOT NULL DEFAULT '',
                    producer_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(artifact_id, revision),
                    FOREIGN KEY(artifact_id, revision)
                        REFERENCES work_artifact_revision(artifact_id, revision)
                );
                CREATE TABLE IF NOT EXISTS work_artifact_render (
                    render_id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL,
                    artifact_revision INTEGER NOT NULL,
                    format TEXT NOT NULL,
                    renderer TEXT NOT NULL,
                    renderer_version TEXT NOT NULL,
                    output_ref TEXT NOT NULL DEFAULT '',
                    preview_refs_json TEXT NOT NULL DEFAULT '[]',
                    diagnostics_ref TEXT NOT NULL DEFAULT '',
                    normalized_sha256 TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(artifact_id, artifact_revision)
                        REFERENCES work_artifact_revision(artifact_id, revision)
                );
                CREATE INDEX IF NOT EXISTS work_artifact_render_revision_idx
                    ON work_artifact_render(artifact_id, artifact_revision, created_at);
                CREATE TABLE IF NOT EXISTS work_artifact_validation (
                    validation_id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL,
                    artifact_revision INTEGER NOT NULL,
                    validator TEXT NOT NULL,
                    validator_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    findings_json TEXT NOT NULL DEFAULT '[]',
                    report_ref TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    FOREIGN KEY(artifact_id, artifact_revision)
                        REFERENCES work_artifact_revision(artifact_id, revision)
                );
                CREATE INDEX IF NOT EXISTS work_artifact_validation_revision_idx
                    ON work_artifact_validation(artifact_id, artifact_revision, created_at);
                CREATE TABLE IF NOT EXISTS work_artifact_alias (
                    scope_key TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    artifact_revision INTEGER NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(scope_key, alias),
                    FOREIGN KEY(artifact_id, artifact_revision)
                        REFERENCES work_artifact_revision(artifact_id, revision)
                );
                CREATE TRIGGER IF NOT EXISTS work_artifact_revision_no_update
                BEFORE UPDATE ON work_artifact_revision
                BEGIN SELECT RAISE(ABORT, 'artifact revisions are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS work_artifact_revision_no_delete
                BEFORE DELETE ON work_artifact_revision
                BEGIN SELECT RAISE(ABORT, 'artifact revisions are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS work_artifact_detail_no_update
                BEFORE UPDATE ON work_artifact_revision_detail
                BEGIN SELECT RAISE(ABORT, 'artifact revision details are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS work_artifact_render_no_update
                BEFORE UPDATE ON work_artifact_render
                BEGIN SELECT RAISE(ABORT, 'artifact renders are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS work_artifact_validation_no_update
                BEFORE UPDATE ON work_artifact_validation
                BEGIN SELECT RAISE(ABORT, 'artifact validations are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS work_artifact_link_no_update
                BEFORE UPDATE ON work_artifact_link
                BEGIN SELECT RAISE(ABORT, 'artifact links are immutable'); END;
                """
                )
                columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(work_artifact_validation)")}
                if "render_id" not in columns:
                    connection.execute("ALTER TABLE work_artifact_validation ADD COLUMN render_id TEXT NOT NULL DEFAULT ''")
            finally:
                connection.close()

    @staticmethod
    def _artifact(row: sqlite3.Row | None) -> ArtifactObjectRecord | None:
        if row is None:
            return None
        return ArtifactObjectRecord(
            artifact_id=str(row["artifact_id"]),
            kind=str(row["kind"]),
            title=str(row["name"]),
            scope=WorkScope.from_mapping(_object(row["scope_json"])),
            current_revision=int(row["current_revision"]),
            version=int(row["version"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            tombstoned_at=float(row["tombstoned_at"] or 0),
        )

    @staticmethod
    def _revision(row: sqlite3.Row | None) -> ArtifactRevisionRecord | None:
        if row is None:
            return None
        return ArtifactRevisionRecord(
            artifact_id=str(row["artifact_id"]),
            revision=int(row["revision"]),
            parent_revision=int(row["parent_revision"] or 0),
            content_ref=str(row["content_ref"]),
            media_type=str(row["media_type"]),
            source_ref=str(row["source_ref"] or ""),
            manifest_ref=str(row["manifest_ref"] or ""),
            metadata=_object(row["metadata_json"]),
            producer=_object(row["producer_json"]),
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _render(row: sqlite3.Row) -> ArtifactRenderRecord:
        return ArtifactRenderRecord(
            render_id=str(row["render_id"]),
            artifact_id=str(row["artifact_id"]),
            revision=int(row["artifact_revision"]),
            format=str(row["format"]),
            renderer=str(row["renderer"]),
            renderer_version=str(row["renderer_version"]),
            output_ref=str(row["output_ref"] or ""),
            preview_refs=tuple(str(item) for item in _list(row["preview_refs_json"])),
            diagnostics_ref=str(row["diagnostics_ref"] or ""),
            normalized_sha256=str(row["normalized_sha256"] or ""),
            status=str(row["status"]),
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _validation(row: sqlite3.Row) -> ArtifactValidationRecord:
        findings = tuple(
            dict(item) for item in _list(row["findings_json"])
            if isinstance(item, Mapping)
        )
        return ArtifactValidationRecord(
            validation_id=str(row["validation_id"]),
            render_id=str(row["render_id"] or ""),
            artifact_id=str(row["artifact_id"]),
            revision=int(row["artifact_revision"]),
            validator=str(row["validator"]),
            validator_version=str(row["validator_version"]),
            status=str(row["status"]),
            findings=findings,
            report_ref=str(row["report_ref"] or ""),
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _alias(row: sqlite3.Row) -> ArtifactAliasRecord:
        return ArtifactAliasRecord(
            scope_key=str(row["scope_key"]),
            alias=str(row["alias"]),
            artifact_id=str(row["artifact_id"]),
            revision=int(row["artifact_revision"]),
            version=int(row["version"]),
            updated_at=float(row["updated_at"]),
        )

    def require_artifact(self, artifact_id: str) -> ArtifactObjectRecord:
        with self.work._read() as connection:
            value = self._artifact(connection.execute(
                "SELECT * FROM work_artifact_object WHERE artifact_id=?",
                (str(artifact_id),),
            ).fetchone())
        if value is None:
            raise ArtifactRuntimeNotFound(f"unknown artifact: {artifact_id}")
        return value

    @staticmethod
    def _assert_live(artifact: ArtifactObjectRecord) -> None:
        if artifact.tombstoned_at:
            raise ArtifactRuntimeValidationError("artifact is tombstoned")

    def get_revision(
        self, artifact_id: str, revision: int | None = None,
    ) -> ArtifactRevisionRecord:
        _artifact, selected = self.artifact_and_revision(artifact_id, revision)
        return selected

    def artifact_and_revision(
        self, artifact_id: str, revision: int | None = None,
    ) -> tuple[ArtifactObjectRecord, ArtifactRevisionRecord]:
        """Resolve object metadata and revision in one SQLite read snapshot."""

        with self.work._read() as connection:
            connection.execute("BEGIN")
            artifact = self._artifact(connection.execute(
                "SELECT * FROM work_artifact_object WHERE artifact_id=?",
                (str(artifact_id),),
            ).fetchone())
            if artifact is None:
                raise ArtifactRuntimeNotFound(f"unknown artifact: {artifact_id}")
            selected = int(
                revision if revision is not None else artifact.current_revision
            )
            row = connection.execute(
                """
                SELECT r.*, COALESCE(d.parent_revision,0) AS parent_revision,
                       COALESCE(d.source_ref,'') AS source_ref,
                       COALESCE(d.manifest_ref,'') AS manifest_ref,
                       COALESCE(d.producer_json,'{}') AS producer_json
                FROM work_artifact_revision r
                LEFT JOIN work_artifact_revision_detail d
                  ON d.artifact_id=r.artifact_id AND d.revision=r.revision
                WHERE r.artifact_id=? AND r.revision=?
                """,
                (artifact.artifact_id, selected),
            ).fetchone()
        value = self._revision(row)
        if value is None:
            raise ArtifactRuntimeNotFound(
                f"unknown artifact revision: {artifact_id}@{selected}"
            )
        return artifact, value

    def revisions(
        self, artifact_id: str, *, limit: int = 200,
    ) -> tuple[ArtifactRevisionRecord, ...]:
        self.require_artifact(artifact_id)
        with self.work._read() as connection:
            rows = connection.execute(
                """
                SELECT r.*, COALESCE(d.parent_revision,0) AS parent_revision,
                       COALESCE(d.source_ref,'') AS source_ref,
                       COALESCE(d.manifest_ref,'') AS manifest_ref,
                       COALESCE(d.producer_json,'{}') AS producer_json
                FROM work_artifact_revision r
                LEFT JOIN work_artifact_revision_detail d
                  ON d.artifact_id=r.artifact_id AND d.revision=r.revision
                WHERE r.artifact_id=?
                ORDER BY r.revision DESC LIMIT ?
                """,
                (str(artifact_id), max(1, min(int(limit or 200), 500))),
            ).fetchall()
        return tuple(
            value for row in rows if (value := self._revision(row)) is not None
        )

    def list_artifacts(
        self,
        *,
        scope: WorkScope | Mapping[str, Any] | None = None,
        kind: str = "",
        include_tombstoned: bool = False,
        limit: int = 200,
    ) -> list[ArtifactObjectRecord]:
        clauses = ["1=1"]
        params: list[Any] = []
        if kind:
            clauses.append("kind=?")
            params.append(str(kind))
        if not include_tombstoned:
            clauses.append("tombstoned_at IS NULL")
        if scope is not None:
            append_json_scope_visibility(clauses, params, "scope_json", scope)
        params.append(max(1, min(int(limit or 200), 500)))
        with self.work._read() as connection:
            rows = connection.execute(
                "SELECT * FROM work_artifact_object WHERE "
                + " AND ".join(clauses)
                + " ORDER BY updated_at DESC,artifact_id LIMIT ?",
                params,
            ).fetchall()
        return [
            item for row in rows if (item := self._artifact(row)) is not None
        ]

    def create(
        self,
        *,
        kind: str,
        title: str,
        scope: WorkScope | Mapping[str, Any] | None,
        content_ref: str,
        media_type: str,
        metadata: Mapping[str, Any] | None = None,
        source_ref: str = "",
        manifest_ref: str = "",
        producer: Mapping[str, Any] | None = None,
        artifact_id: str = "",
        actor: WorkActor | None = None,
        correlation_id: str = "",
        idempotency_key: str = "",
    ) -> tuple[ArtifactObjectRecord, ArtifactRevisionRecord, int]:
        identity = str(artifact_id or _id("artifact"))
        resolved_scope = coerce_work_scope(scope)
        now = time.time()
        with self.work._write() as connection:
            connection.execute(
                """
                INSERT INTO work_artifact_object(
                    artifact_id,kind,name,scope_json,current_revision,version,
                    created_at,updated_at
                ) VALUES (?,?,?,?,1,1,?,?)
                """,
                (
                    identity,
                    _text(kind, "kind", required=True, limit=120),
                    _text(title, "title", required=True, limit=1000),
                    _json(resolved_scope.to_dict()),
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO work_artifact_revision(
                    artifact_id,revision,content_ref,media_type,metadata_json,created_at
                ) VALUES (?,1,?,?,?,?)
                """,
                (
                    identity,
                    _text(content_ref, "content_ref", required=True, limit=512),
                    _text(media_type, "media_type", required=True, limit=256),
                    _json(dict(metadata or {})),
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO work_artifact_revision_detail(
                    artifact_id,revision,parent_revision,source_ref,manifest_ref,producer_json
                ) VALUES (?,1,0,?,?,?)
                """,
                (identity, str(source_ref), str(manifest_ref), _json(dict(producer or {}))),
            )
            event = self.work._insert_event_tx(
                connection,
                event_type="artifact.created",
                aggregate_kind="artifact",
                aggregate_id=identity,
                aggregate_version=1,
                expected_aggregate_version=0,
                scope=resolved_scope,
                actor=actor,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                payload={"kind": kind, "title": title, "revision": 1},
                created_at=now,
            )
        return self.require_artifact(identity), self.get_revision(identity, 1), event.sequence

    def add_revision(
        self,
        artifact_id: str,
        *,
        expected_version: int,
        content_ref: str,
        media_type: str,
        metadata: Mapping[str, Any] | None = None,
        source_ref: str = "",
        manifest_ref: str = "",
        producer: Mapping[str, Any] | None = None,
        actor: WorkActor | None = None,
        correlation_id: str = "",
        idempotency_key: str = "",
    ) -> tuple[ArtifactObjectRecord, ArtifactRevisionRecord, int]:
        now = time.time()
        with self.work._write() as connection:
            row = connection.execute(
                "SELECT * FROM work_artifact_object WHERE artifact_id=?",
                (str(artifact_id),),
            ).fetchone()
            artifact = self._artifact(row)
            if artifact is None:
                raise ArtifactRuntimeNotFound(f"unknown artifact: {artifact_id}")
            self._assert_live(artifact)
            if artifact.version != int(expected_version):
                raise ArtifactRuntimeConflict(
                    f"artifact version changed ({artifact.version} != {expected_version})"
                )
            revision = artifact.current_revision + 1
            version = artifact.version + 1
            connection.execute(
                """
                INSERT INTO work_artifact_revision(
                    artifact_id,revision,content_ref,media_type,metadata_json,created_at
                ) VALUES (?,?,?,?,?,?)
                """,
                (
                    artifact.artifact_id,
                    revision,
                    _text(content_ref, "content_ref", required=True, limit=512),
                    _text(media_type, "media_type", required=True, limit=256),
                    _json(dict(metadata or {})),
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO work_artifact_revision_detail(
                    artifact_id,revision,parent_revision,source_ref,manifest_ref,producer_json
                ) VALUES (?,?,?,?,?,?)
                """,
                (
                    artifact.artifact_id,
                    revision,
                    artifact.current_revision,
                    str(source_ref),
                    str(manifest_ref),
                    _json(dict(producer or {})),
                ),
            )
            connection.execute(
                "UPDATE work_artifact_object SET current_revision=?,version=?,updated_at=? "
                "WHERE artifact_id=? AND version=?",
                (revision, version, now, artifact.artifact_id, artifact.version),
            )
            event = self.work._insert_event_tx(
                connection,
                event_type="artifact.revised",
                aggregate_kind="artifact",
                aggregate_id=artifact.artifact_id,
                aggregate_version=version,
                expected_aggregate_version=artifact.version,
                scope=artifact.scope,
                actor=actor,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                payload={"revision": revision, "parent_revision": artifact.current_revision},
                created_at=now,
            )
        return (
            self.require_artifact(artifact.artifact_id),
            self.get_revision(artifact.artifact_id, revision),
            event.sequence,
        )

    def tombstone(
        self,
        artifact_id: str,
        *,
        expected_version: int,
        actor: WorkActor | None = None,
        correlation_id: str = "",
        idempotency_key: str = "",
    ) -> tuple[ArtifactObjectRecord, int]:
        artifact = self.require_artifact(artifact_id)
        if artifact.version != int(expected_version):
            raise ArtifactRuntimeConflict("artifact version changed")
        if artifact.tombstoned_at:
            return artifact, 0
        now = time.time()
        with self.work._write() as connection:
            updated = connection.execute(
                "UPDATE work_artifact_object SET tombstoned_at=?,version=version+1,"
                "updated_at=? WHERE artifact_id=? AND version=? AND tombstoned_at IS NULL",
                (now, now, artifact.artifact_id, artifact.version),
            )
            if updated.rowcount != 1:
                raise ArtifactRuntimeConflict("artifact version changed")
            event = self.work._insert_event_tx(
                connection,
                event_type="artifact.tombstoned",
                aggregate_kind="artifact",
                aggregate_id=artifact.artifact_id,
                aggregate_version=artifact.version + 1,
                expected_aggregate_version=artifact.version,
                scope=artifact.scope,
                actor=actor,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                payload={"revision": artifact.current_revision},
                created_at=now,
            )
        return self.require_artifact(artifact.artifact_id), event.sequence

    def _bump_event(
        self,
        connection: sqlite3.Connection,
        artifact: ArtifactObjectRecord,
        *,
        event_type: str,
        payload: Mapping[str, Any],
        actor: WorkActor | None,
        correlation_id: str,
        idempotency_key: str,
        now: float,
    ):
        version = artifact.version + 1
        updated = connection.execute(
            "UPDATE work_artifact_object SET version=?,updated_at=? "
            "WHERE artifact_id=? AND version=? AND tombstoned_at IS NULL",
            (version, now, artifact.artifact_id, artifact.version),
        )
        if updated.rowcount != 1:
            raise ArtifactRuntimeConflict("artifact version changed")
        return self.work._insert_event_tx(
            connection,
            event_type=event_type,
            aggregate_kind="artifact",
            aggregate_id=artifact.artifact_id,
            aggregate_version=version,
            expected_aggregate_version=artifact.version,
            scope=artifact.scope,
            actor=actor,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            payload=dict(payload),
            created_at=now,
        )

    def add_render(
        self,
        artifact_id: str,
        *,
        expected_version: int,
        revision: int,
        format: str,
        renderer: str,
        renderer_version: str,
        output_ref: str,
        preview_refs: tuple[str, ...] = (),
        diagnostics_ref: str = "",
        normalized_sha256: str = "",
        status: str = "succeeded",
        actor: WorkActor | None = None,
        correlation_id: str = "",
        idempotency_key: str = "",
    ) -> tuple[ArtifactRenderRecord, int]:
        artifact = self.require_artifact(artifact_id)
        self._assert_live(artifact)
        if artifact.version != int(expected_version):
            raise ArtifactRuntimeConflict("artifact version changed")
        self.get_revision(artifact_id, revision)
        now = time.time()
        render_id = _id("render")
        with self.work._write() as connection:
            connection.execute(
                """
                INSERT INTO work_artifact_render(
                    render_id,artifact_id,artifact_revision,format,renderer,
                    renderer_version,output_ref,preview_refs_json,diagnostics_ref,
                    normalized_sha256,status,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    render_id, artifact.artifact_id, int(revision),
                    _text(format, "format", required=True, limit=80),
                    _text(renderer, "renderer", required=True, limit=200),
                    _text(renderer_version, "renderer_version", required=True, limit=120),
                    str(output_ref), _json(list(preview_refs)), str(diagnostics_ref),
                    str(normalized_sha256), str(status), now,
                ),
            )
            event = self._bump_event(
                connection, artifact, event_type="artifact.rendered",
                payload={"render_id": render_id, "revision": revision, "format": format,
                         "status": status},
                actor=actor, correlation_id=correlation_id,
                idempotency_key=idempotency_key, now=now,
            )
            row = connection.execute(
                "SELECT * FROM work_artifact_render WHERE render_id=?", (render_id,)
            ).fetchone()
        assert row is not None
        return self._render(row), event.sequence

    def add_validation(
        self,
        artifact_id: str,
        *,
        expected_version: int,
        revision: int,
        validator: str,
        validator_version: str,
        status: str,
        findings: tuple[Mapping[str, Any], ...],
        report_ref: str = "",
        render_id: str = "",
        actor: WorkActor | None = None,
        correlation_id: str = "",
        idempotency_key: str = "",
    ) -> tuple[ArtifactValidationRecord, int]:
        artifact = self.require_artifact(artifact_id)
        self._assert_live(artifact)
        if artifact.version != int(expected_version):
            raise ArtifactRuntimeConflict("artifact version changed")
        self.get_revision(artifact_id, revision)
        now = time.time()
        validation_id = _id("validation")
        with self.work._write() as connection:
            connection.execute(
                """
                INSERT INTO work_artifact_validation(
                    validation_id,artifact_id,artifact_revision,validator,
                    validator_version,status,findings_json,report_ref,created_at,render_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    validation_id, artifact.artifact_id, int(revision),
                    _text(validator, "validator", required=True, limit=200),
                    _text(validator_version, "validator_version", required=True, limit=120),
                    str(status), _json([dict(item) for item in findings]),
                    str(report_ref), now, str(render_id),
                ),
            )
            event = self._bump_event(
                connection, artifact, event_type="artifact.validated",
                payload={"validation_id": validation_id, "revision": revision,
                         "status": status, "findings": len(findings)},
                actor=actor, correlation_id=correlation_id,
                idempotency_key=idempotency_key, now=now,
            )
            row = connection.execute(
                "SELECT * FROM work_artifact_validation WHERE validation_id=?",
                (validation_id,),
            ).fetchone()
        assert row is not None
        return self._validation(row), event.sequence

    def set_alias(
        self,
        artifact_id: str,
        *,
        expected_version: int,
        revision: int,
        scope_key: str,
        alias: str,
        expected_alias_version: int = 0,
        actor: WorkActor | None = None,
        correlation_id: str = "",
        idempotency_key: str = "",
        _connection: sqlite3.Connection | None = None,
    ) -> tuple[ArtifactAliasRecord, int]:
        artifact = self.require_artifact(artifact_id)
        self._assert_live(artifact)
        if artifact.version != int(expected_version):
            raise ArtifactRuntimeConflict("artifact version changed")
        self.get_revision(artifact_id, revision)
        clean_scope = _text(scope_key, "scope_key", required=True, limit=512)
        clean_alias = _text(alias, "alias", required=True, limit=240)
        now = time.time()
        from contextlib import nullcontext
        with nullcontext(_connection) if _connection is not None else self.work._write() as connection:
            prior = connection.execute(
                "SELECT * FROM work_artifact_alias WHERE scope_key=? AND alias=?",
                (clean_scope, clean_alias),
            ).fetchone()
            prior_version = int(prior["version"]) if prior is not None else 0
            if prior_version != int(expected_alias_version):
                raise ArtifactRuntimeConflict(
                    f"alias version changed ({prior_version} != {expected_alias_version})"
                )
            if prior is None:
                connection.execute(
                    "INSERT INTO work_artifact_alias(scope_key,alias,artifact_id,"
                    "artifact_revision,version,updated_at) VALUES (?,?,?,?,1,?)",
                    (clean_scope, clean_alias, artifact.artifact_id, int(revision), now),
                )
            else:
                connection.execute(
                    "UPDATE work_artifact_alias SET artifact_id=?,artifact_revision=?,"
                    "version=version+1,updated_at=? WHERE scope_key=? AND alias=? AND version=?",
                    (
                        artifact.artifact_id, int(revision), now,
                        clean_scope, clean_alias, prior_version,
                    ),
                )
            event = self._bump_event(
                connection, artifact, event_type="artifact.alias_set",
                payload={"scope_key": clean_scope, "alias": clean_alias,
                         "revision": revision},
                actor=actor, correlation_id=correlation_id,
                idempotency_key=idempotency_key, now=now,
            )
            row = connection.execute(
                "SELECT * FROM work_artifact_alias WHERE scope_key=? AND alias=?",
                (clean_scope, clean_alias),
            ).fetchone()
        assert row is not None
        return self._alias(row), event.sequence

    def link(
        self,
        artifact_id: str,
        *,
        expected_version: int,
        revision: int,
        owner_kind: str,
        owner_id: str,
        role: str,
        actor: WorkActor | None = None,
        correlation_id: str = "",
        idempotency_key: str = "",
    ) -> tuple[dict[str, Any], int]:
        artifact = self.require_artifact(artifact_id)
        self._assert_live(artifact)
        if artifact.version != int(expected_version):
            raise ArtifactRuntimeConflict("artifact version changed")
        self.get_revision(artifact_id, revision)
        now = time.time()
        link_id = _id("artifact_link")
        with self.work._write() as connection:
            connection.execute(
                "INSERT INTO work_artifact_link(link_id,artifact_id,artifact_revision,"
                "owner_kind,owner_id,role,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    link_id, artifact.artifact_id, int(revision),
                    _text(owner_kind, "owner_kind", required=True, limit=120),
                    _text(owner_id, "owner_id", required=True, limit=512),
                    _text(role, "role", required=True, limit=120), now,
                ),
            )
            event = self._bump_event(
                connection, artifact, event_type="artifact.linked",
                payload={"link_id": link_id, "owner_kind": owner_kind,
                         "owner_id": owner_id, "role": role, "revision": revision},
                actor=actor, correlation_id=correlation_id,
                idempotency_key=idempotency_key, now=now,
            )
        return {
            "link_id": link_id, "artifact_id": artifact.artifact_id,
            "revision": int(revision), "owner_kind": owner_kind,
            "owner_id": owner_id, "role": role, "created_at": now,
        }, event.sequence

    def renders(self, artifact_id: str, revision: int) -> tuple[ArtifactRenderRecord, ...]:
        with self.work._read() as connection:
            rows = connection.execute(
                "SELECT * FROM work_artifact_render WHERE artifact_id=? AND "
                "artifact_revision=? ORDER BY created_at,render_id",
                (str(artifact_id), int(revision)),
            ).fetchall()
        return tuple(self._render(row) for row in rows)

    def validations(
        self, artifact_id: str, revision: int,
    ) -> tuple[ArtifactValidationRecord, ...]:
        with self.work._read() as connection:
            rows = connection.execute(
                "SELECT * FROM work_artifact_validation WHERE artifact_id=? AND "
                "artifact_revision=? ORDER BY created_at,validation_id",
                (str(artifact_id), int(revision)),
            ).fetchall()
        return tuple(self._validation(row) for row in rows)

    def aliases(self, artifact_id: str) -> tuple[ArtifactAliasRecord, ...]:
        with self.work._read() as connection:
            rows = connection.execute(
                "SELECT * FROM work_artifact_alias WHERE artifact_id=? "
                "ORDER BY scope_key,alias",
                (str(artifact_id),),
            ).fetchall()
        return tuple(self._alias(row) for row in rows)

    def resolve_alias(self, scope_key: str, alias: str) -> ArtifactAliasRecord:
        with self.work._read() as connection:
            row = connection.execute(
                "SELECT a.* FROM work_artifact_alias a "
                "JOIN work_artifact_object o ON o.artifact_id=a.artifact_id "
                "WHERE a.scope_key=? AND a.alias=? AND o.tombstoned_at IS NULL",
                (str(scope_key), str(alias)),
            ).fetchone()
        if row is None:
            raise ArtifactRuntimeNotFound(f"unknown artifact alias: {scope_key}/{alias}")
        return self._alias(row)

    def links(self, artifact_id: str, revision: int) -> tuple[dict[str, Any], ...]:
        with self.work._read() as connection:
            rows = connection.execute(
                "SELECT * FROM work_artifact_link WHERE artifact_id=? AND "
                "artifact_revision=? ORDER BY created_at,link_id",
                (str(artifact_id), int(revision)),
            ).fetchall()
        return tuple(dict(row) for row in rows)


__all__ = ["ArtifactRuntimeRepository"]
