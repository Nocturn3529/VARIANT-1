"""Host-owned, versioned artifact authoring and publishing runtime."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import asyncio
import hashlib
import json
import os
import uuid
from typing import Any

from core_invariants import (
    StrictJSONError,
    canonical_digest,
    canonical_json_bytes,
    strict_json_value,
)
from work_fabric.jobs import JobExecutionContext, JobResult
from work_fabric.models import JobRecord, WorkActor
from work_fabric.scope import (
    WorkScope,
    coerce_work_scope,
    current_work_scope,
    work_scope_visible,
)

from .blob_service import ArtifactBlobService

from .builders import BUILDER_VERSION, build_artifact, freeze_spec_resources
from .runtime_models import (
    ArtifactRuntimeConflict,
    ArtifactRuntimeNotFound,
    ArtifactRuntimeValidationError,
    ArtifactSnapshot,
)
from .runtime_repository import ArtifactRuntimeRepository
from .scopes import cas_scope_id
from .validation import VALIDATOR_VERSION, validate_payload


ARTIFACT_PUBLISH_JOB = "artifact.publish.v1"
ARTIFACT_SPEC_MEDIA_TYPE = "application/vnd.variant1.artifact-spec+json"
_SUPPORTED_FORMATS = frozenset(
    {"md", "markdown", "html", "json", "csv", "tsv", "docx", "pptx", "xlsx", "pdf", "latex"}
)


def _strict_mapping(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    try:
        decoded = strict_json_value(dict(value))
        payload = canonical_json_bytes(decoded)
    except (StrictJSONError, TypeError, ValueError) as exc:
        raise ArtifactRuntimeValidationError(f"{field} must be strict JSON: {exc}") from exc
    if len(payload) > 16 * 1024 * 1024:
        raise ArtifactRuntimeValidationError(f"{field} exceeds 16 MiB")
    if not isinstance(decoded, dict):
        raise ArtifactRuntimeValidationError(f"{field} must be an object")
    return decoded


def _formats(value: Sequence[str] | str) -> tuple[str, ...]:
    rows = [value] if isinstance(value, str) else list(value)
    if not rows or len(rows) > 20:
        raise ArtifactRuntimeValidationError("formats must contain 1-20 items")
    result: list[str] = []
    for item in rows:
        name = str(item or "").strip().lower().lstrip(".")
        if name not in _SUPPORTED_FORMATS:
            raise ArtifactRuntimeValidationError(f"unsupported artifact format: {item}")
        name = "md" if name == "markdown" else name
        if name not in result:
            result.append(name)
    return tuple(result)


def _scope_key(scope: WorkScope) -> str:
    return "work-scope:" + canonical_digest(scope.to_dict(include_empty=False))


class ArtifactRuntime:
    """Immutable artifact specs plus reproducible renders and validation evidence.

    The runtime owns no scheduler loop.  It registers its durable handler on the
    process-wide Work Fabric, which must be composed before ``work.start()``.
    """

    def __init__(self, work: Any, artifact_store: Any, *, register_handler: bool = True) -> None:
        if work is None or artifact_store is None:
            raise ValueError("work and artifact_store are required")
        self.work = work
        self.artifact_store = artifact_store
        self.blobs = ArtifactBlobService(artifact_store)
        self.repository = ArtifactRuntimeRepository(work.repository)
        if register_handler:
            self.register_job_handlers()

    def register_job_handlers(self) -> None:
        self.work.register_job_handler(ARTIFACT_PUBLISH_JOB, self._publish_job)

    @staticmethod
    def _actor(value: str = "agent") -> WorkActor:
        return WorkActor("agent" if value == "agent" else "user", str(value or "user"))

    def _require_live(self, artifact_id: str):
        artifact = self.repository.require_artifact(artifact_id)
        if artifact.tombstoned_at:
            raise ArtifactRuntimeValidationError("artifact is tombstoned")
        return artifact

    def _store_spec(
        self,
        specification: Mapping[str, Any],
        *,
        scope: WorkScope,
        artifact_id: str,
        producer: Mapping[str, Any] | None = None,
    ) -> tuple[str, str, dict[str, Any]]:
        clean = _strict_mapping(specification, "specification")
        try:
            clean = freeze_spec_resources(clean)
        except (OSError, TypeError, ValueError) as exc:
            raise ArtifactRuntimeValidationError(f"artifact resource is invalid: {exc}") from exc
        cas_scope = cas_scope_id(scope, artifact_id)
        source = self.artifact_store.put_bytes(
            canonical_json_bytes(clean),
            media_type=ARTIFACT_SPEC_MEDIA_TYPE,
            kind="artifact_specification",
            scope=cas_scope,
        )
        manifest_value = {
            "schema": "variant1.artifact-source-manifest.v1",
            "artifact_id": artifact_id,
            "source_ref": source.ref,
            "source_sha256": source.sha256,
            "source_bytes": source.bytes,
            "scope": scope.to_dict(include_empty=False),
            "producer": dict(producer or {}),
        }
        manifest = self.artifact_store.put_json(
            manifest_value, kind="artifact_source_manifest", scope=cas_scope,
        )
        return source.ref, manifest.ref, clean

    def create(
        self,
        *,
        title: str,
        kind: str,
        specification: Mapping[str, Any],
        scope: WorkScope | Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        producer: Mapping[str, Any] | None = None,
        artifact_id: str = "",
        alias: str = "",
        actor: str = "agent",
        correlation_id: str = "",
        idempotency_key: str = "",
    ) -> ArtifactSnapshot:
        resolved = coerce_work_scope(scope) if scope is not None else current_work_scope()
        if artifact_id:
            identity = str(artifact_id)
        elif idempotency_key:
            seed = json.dumps(
                {"scope": resolved.to_dict(include_empty=False), "key": idempotency_key},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
            identity = f"artifact_{hashlib.sha256(seed).hexdigest()[:32]}"
        else:
            identity = f"artifact_{uuid.uuid4().hex}"
        source_ref, manifest_ref, clean = self._store_spec(
            specification, scope=resolved, artifact_id=identity, producer=producer,
        )
        try:
            prior = self.repository.require_artifact(identity)
        except ArtifactRuntimeNotFound:
            prior = None
        if prior is not None:
            if prior.tombstoned_at:
                raise ArtifactRuntimeValidationError("artifact is tombstoned")
            latest = self.repository.get_revision(identity)
            if (
                idempotency_key
                and prior.kind == str(kind)
                and prior.title == str(title).strip()
                and prior.scope == resolved
                and (latest.source_ref or latest.content_ref) == source_ref
            ):
                if alias:
                    self._reconcile_create_alias(
                        prior,
                        revision=latest.revision,
                        scope=resolved,
                        alias=alias,
                        actor=actor,
                        correlation_id=correlation_id,
                        idempotency_key=idempotency_key,
                    )
                return self.snapshot(identity)
            raise ArtifactRuntimeConflict(f"artifact identity already exists: {identity}")
        artifact, revision, _cursor = self.repository.create(
            kind=kind,
            title=title,
            scope=resolved,
            content_ref=source_ref,
            media_type=ARTIFACT_SPEC_MEDIA_TYPE,
            metadata={
                "specification_schema": str(clean.get("schema") or "variant1.artifact-spec.v1"),
                **dict(metadata or {}),
            },
            source_ref=source_ref,
            manifest_ref=manifest_ref,
            producer=dict(producer or {}),
            artifact_id=identity,
            actor=self._actor(actor),
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        if alias:
            self._reconcile_create_alias(
                artifact,
                revision=revision.revision,
                scope=resolved,
                alias=alias,
                actor=actor,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
            )
        return self.snapshot(identity)

    def _reconcile_create_alias(
        self,
        artifact: Any,
        *,
        revision: int,
        scope: WorkScope,
        alias: str,
        actor: str,
        correlation_id: str,
        idempotency_key: str,
    ) -> None:
        scope_key = _scope_key(scope)
        try:
            current = self.repository.resolve_alias(scope_key, alias)
        except ArtifactRuntimeNotFound:
            current = None
        if current is not None:
            if (
                current.artifact_id == artifact.artifact_id
                and current.revision == int(revision)
            ):
                return
            raise ArtifactRuntimeConflict(
                f"alias version changed ({current.version} != 0)"
            )
        latest = self.repository.require_artifact(artifact.artifact_id)
        self.repository.set_alias(
            artifact.artifact_id,
            expected_version=latest.version,
            revision=int(revision),
            scope_key=scope_key,
            alias=alias,
            expected_alias_version=0,
            actor=self._actor(actor),
            correlation_id=correlation_id,
            idempotency_key=f"{idempotency_key}:alias" if idempotency_key else "",
        )

    def revise(
        self,
        artifact_id: str,
        specification: Mapping[str, Any],
        *,
        expected_version: int,
        metadata: Mapping[str, Any] | None = None,
        producer: Mapping[str, Any] | None = None,
        actor: str = "agent",
        correlation_id: str = "",
        idempotency_key: str = "",
    ) -> ArtifactSnapshot:
        artifact = self.repository.require_artifact(artifact_id)
        if artifact.tombstoned_at:
            raise ArtifactRuntimeValidationError("artifact is tombstoned")
        source_ref, manifest_ref, clean = self._store_spec(
            specification, scope=artifact.scope,
            artifact_id=artifact.artifact_id, producer=producer,
        )
        current = self.repository.require_artifact(artifact.artifact_id)
        if current.version != int(expected_version) and idempotency_key:
            latest = self.repository.get_revision(artifact.artifact_id)
            if (latest.source_ref or latest.content_ref) == source_ref:
                return self.snapshot(artifact.artifact_id)
        self.repository.add_revision(
            artifact.artifact_id,
            expected_version=expected_version,
            content_ref=source_ref,
            media_type=ARTIFACT_SPEC_MEDIA_TYPE,
            metadata={
                "specification_schema": str(clean.get("schema") or "variant1.artifact-spec.v1"),
                **dict(metadata or {}),
            },
            source_ref=source_ref,
            manifest_ref=manifest_ref,
            producer=dict(producer or {}),
            actor=self._actor(actor),
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        return self.snapshot(artifact.artifact_id)

    def get_specification(self, artifact_id: str, revision: int | None = None) -> dict[str, Any]:
        record = self.repository.get_revision(artifact_id, revision)
        reference = record.source_ref or record.content_ref
        try:
            value = json.loads(self.artifact_store.read_bytes(reference).decode("utf-8"))
        except Exception as exc:
            raise ArtifactRuntimeValidationError(
                f"artifact source specification is unreadable: {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise ArtifactRuntimeValidationError("artifact source specification is not an object")
        return value

    def snapshot(self, artifact_id: str, revision: int | None = None) -> ArtifactSnapshot:
        artifact, selected = self.repository.artifact_and_revision(
            artifact_id, revision
        )
        return ArtifactSnapshot(
            artifact=artifact,
            revision=selected,
            renders=self.repository.renders(artifact_id, selected.revision),
            validations=self.repository.validations(artifact_id, selected.revision),
            aliases=self.repository.aliases(artifact_id),
            links=self.repository.links(artifact_id, selected.revision),
        )

    def require_visible(
        self,
        artifact_id: str,
        *,
        scope: WorkScope | Mapping[str, Any] | None,
    ) -> Any:
        artifact = self.repository.require_artifact(str(artifact_id or ""))
        if not work_scope_visible(artifact.scope, coerce_work_scope(scope)):
            raise ArtifactRuntimeNotFound(f"unknown artifact: {artifact_id}")
        return artifact

    def read_text(
        self,
        ref: str,
        *,
        scope: WorkScope | Mapping[str, Any] | None,
        max_chars: int = 20_000,
        artifact_id: str = "",
    ) -> dict[str, Any]:
        return self.blobs.read_text(
            ref, scope=scope, max_chars=max_chars, artifact_id=artifact_id
        )

    def list(self, **filters: Any) -> list[ArtifactSnapshot]:
        rows = self.repository.list_artifacts(**filters)
        return [self.snapshot(item.artifact_id) for item in rows]

    def history(self, artifact_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.repository.revisions(artifact_id, limit=limit)]

    def render(
        self,
        artifact_id: str,
        format: str,
        *,
        revision: int | None = None,
        actor: str = "agent",
        correlation_id: str = "",
        idempotency_key: str = "",
    ) -> Any:
        artifact = self._require_live(artifact_id)
        canonical = _formats((format,))[0]
        selected = self.repository.get_revision(artifact_id, revision)
        for prior in reversed(self.repository.renders(artifact_id, selected.revision)):
            if (
                prior.format == canonical
                and prior.renderer_version == BUILDER_VERSION
                and prior.status == "succeeded"
                and prior.output_ref
            ):
                self.artifact_store.stat(prior.output_ref, verify=True)
                return prior
        specification = self.get_specification(artifact_id, selected.revision)
        output = build_artifact(specification, canonical)
        cas_scope = cas_scope_id(artifact.scope, artifact.artifact_id)
        rendered = self.artifact_store.put_bytes(
            output.payload,
            media_type=output.media_type,
            kind=f"artifact_render_{output.format}",
            scope=cas_scope,
        )
        previews = tuple(
            self.artifact_store.put_bytes(
                payload, media_type="image/png", kind="artifact_preview", scope=cas_scope,
            ).ref
            for payload in output.previews
        )
        diagnostics_ref = ""
        if output.diagnostics:
            diagnostics_ref = self.artifact_store.put_json(
                {"schema": "variant1.artifact-render-diagnostics.v1",
                 "findings": [dict(item) for item in output.diagnostics]},
                kind="artifact_render_diagnostics",
                scope=cas_scope,
            ).ref
        current = self.repository.require_artifact(artifact_id)
        record, _cursor = self.repository.add_render(
            artifact_id,
            expected_version=current.version,
            revision=selected.revision,
            format=output.format,
            renderer=output.renderer,
            renderer_version=output.renderer_version,
            output_ref=rendered.ref,
            preview_refs=previews,
            diagnostics_ref=diagnostics_ref,
            normalized_sha256=output.normalized_sha256,
            status="succeeded",
            actor=self._actor(actor),
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        return record

    def validate(
        self,
        artifact_id: str,
        format: str,
        *,
        revision: int | None = None,
        actor: str = "agent",
        correlation_id: str = "",
        idempotency_key: str = "",
    ) -> Any:
        self._require_live(artifact_id)
        canonical = _formats((format,))[0]
        selected = self.repository.get_revision(artifact_id, revision)
        validator = f"structural:{canonical}"
        render = self.render(
            artifact_id, canonical, revision=selected.revision, actor=actor,
            correlation_id=correlation_id,
            idempotency_key=f"{idempotency_key}:render" if idempotency_key else "",
        )
        for prior in reversed(self.repository.validations(artifact_id, selected.revision)):
            if (prior.validator == validator and prior.validator_version == VALIDATOR_VERSION
                    and prior.render_id == render.render_id):
                return prior
        artifact = self.repository.require_artifact(artifact_id)
        payload = self.artifact_store.read_bytes(render.output_ref)
        specification = self.get_specification(artifact_id, selected.revision)
        status, findings, metrics = validate_payload(
            canonical, payload, specification=specification,
        )
        report_value = {
            "schema": "variant1.artifact-validation-report.v1",
            "artifact_id": artifact_id,
            "revision": selected.revision,
            "render_id": render.render_id,
            "format": canonical,
            "validator": validator,
            "validator_version": VALIDATOR_VERSION,
            "status": status,
            "findings": [dict(item) for item in findings],
            "metrics": metrics,
        }
        report = self.artifact_store.put_json(
            report_value,
            kind="artifact_validation_report",
            scope=cas_scope_id(artifact.scope, artifact.artifact_id),
        )
        current = self.repository.require_artifact(artifact_id)
        record, _cursor = self.repository.add_validation(
            artifact_id,
            expected_version=current.version,
            revision=selected.revision,
            validator=validator,
            validator_version=VALIDATOR_VERSION,
            status=status,
            findings=findings,
            report_ref=report.ref,
            render_id=render.render_id,
            actor=self._actor(actor),
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        return record

    def publish(
        self,
        artifact_id: str,
        formats: Sequence[str] | str,
        *,
        revision: int | None = None,
        validate: bool = True,
        require_valid: bool = True,
        alias: str = "",
        expected_alias_version: int = 0,
        idempotency_key: str = "",
        priority: int = 0,
    ) -> JobRecord:
        artifact = self.repository.require_artifact(artifact_id)
        if artifact.tombstoned_at:
            raise ArtifactRuntimeValidationError("artifact is tombstoned")
        selected = self.repository.get_revision(artifact_id, revision)
        chosen = _formats(formats)
        return self.work.jobs.create(
            ARTIFACT_PUBLISH_JOB,
            owner_kind="artifact",
            owner_id=artifact.artifact_id,
            scope=artifact.scope,
            priority=priority,
            input_manifest={
                "schema": "variant1.artifact-publish-request.v1",
                "artifact_id": artifact.artifact_id,
                "revision": selected.revision,
                "formats": list(chosen),
                "validate": bool(validate),
                "require_valid": bool(require_valid),
                "alias": str(alias or ""),
                "expected_alias_version": int(expected_alias_version),
            },
            artifact_refs=(selected.source_ref or selected.content_ref,),
            max_attempts=1,
            idempotency_key=idempotency_key,
        )

    def _publish_job(self, execution: JobExecutionContext) -> JobResult:
        request = dict(execution.job.input_manifest or {})
        artifact_id = str(request.get("artifact_id") or "")
        revision = int(request.get("revision") or 0)
        chosen = _formats(tuple(str(item) for item in request.get("formats") or ()))
        renders: list[dict[str, Any]] = []
        validations: list[dict[str, Any]] = []
        failed = False
        for index, format_name in enumerate(chosen, 1):
            if execution.cancellation_requested():
                raise asyncio.CancelledError("artifact publication was cancelled")
            execution.progress({
                "phase": "render",
                "current": index - 1,
                "total": len(chosen),
                "message": f"Rendering {format_name}",
            })
            render = self.render(
                artifact_id, format_name, revision=revision,
                actor="agent", correlation_id=execution.job.job_id,
                idempotency_key=f"{execution.job.job_id}:render:{format_name}",
            )
            renders.append(render.to_dict())
            if bool(request.get("validate", True)):
                validation = self.validate(
                    artifact_id, format_name, revision=revision,
                    actor="agent", correlation_id=execution.job.job_id,
                    idempotency_key=f"{execution.job.job_id}:validate:{format_name}",
                )
                validations.append(validation.to_dict())
                failed = failed or validation.status == "failed"
        if failed and bool(request.get("require_valid", True)):
            # The failure manifest is durable evidence, but no public alias may
            # move until the require-valid precondition has succeeded.
            artifact = self.repository.require_artifact(artifact_id)
            rejected_manifest = {
                "schema": "variant1.artifact-publication.v1",
                "artifact_id": artifact_id,
                "revision": revision,
                "artifact_version": artifact.version,
                "renders": renders,
                "validations": validations,
                "alias": None,
                "valid": False,
            }
            rejected = self.artifact_store.put_json(
                rejected_manifest,
                kind="artifact_publication_manifest",
                scope=cas_scope_id(artifact.scope, artifact.artifact_id),
            )
            raise ArtifactRuntimeValidationError(
                "artifact publication failed structural validation; "
                f"report {rejected.ref}"
            )
        return execution.commit_result(lambda connection: self._commit_publication(
            execution, connection, request=request, artifact_id=artifact_id, revision=revision,
            chosen=chosen, renders=renders, validations=validations, failed=failed))

    def _commit_publication(self, execution, connection, *, request, artifact_id, revision,
                            chosen, renders, validations, failed):
        alias_value = str(request.get("alias") or "").strip()
        alias_record = None
        if alias_value:
            artifact = self.repository.require_artifact(artifact_id)
            scope_key = _scope_key(artifact.scope)
            try:
                existing_alias = self.repository.resolve_alias(scope_key, alias_value)
            except ArtifactRuntimeNotFound:
                existing_alias = None
            if (
                existing_alias is not None
                and existing_alias.artifact_id == artifact_id
                and existing_alias.revision == revision
            ):
                alias_record = existing_alias
            else:
                alias_record, _cursor = self.repository.set_alias(
                    artifact_id,
                    expected_version=artifact.version,
                    revision=revision,
                    scope_key=scope_key,
                    alias=alias_value,
                    expected_alias_version=int(request.get("expected_alias_version") or 0),
                    actor=self._actor("agent"),
                    correlation_id=execution.job.job_id,
                    idempotency_key=f"{execution.job.job_id}:alias",
                    _connection=connection,
                )
        artifact = self.repository._artifact(connection.execute(
            "SELECT * FROM work_artifact_object WHERE artifact_id=?", (artifact_id,)).fetchone())
        result_value = {
            "schema": "variant1.artifact-publication.v1",
            "artifact_id": artifact_id,
            "revision": revision,
            "artifact_version": artifact.version,
            "renders": renders,
            "validations": validations,
            "alias": alias_record.to_dict() if alias_record is not None else None,
            "valid": not failed,
        }
        result = self.artifact_store.put_json(
            result_value,
            kind="artifact_publication_manifest",
            scope=cas_scope_id(artifact.scope, artifact.artifact_id),
        )
        return JobResult(
            result_ref=result.ref,
            progress={
                "phase": "complete",
                "current": len(chosen),
                "total": len(chosen),
                "message": "Artifact publication completed",
                "valid": not failed,
            },
        )

    def set_alias(
        self,
        artifact_id: str,
        alias: str,
        *,
        revision: int | None = None,
        expected_version: int,
        expected_alias_version: int = 0,
    ) -> dict[str, Any]:
        artifact = self._require_live(artifact_id)
        selected = self.repository.get_revision(artifact_id, revision)
        record, cursor = self.repository.set_alias(
            artifact_id,
            expected_version=expected_version,
            revision=selected.revision,
            scope_key=_scope_key(artifact.scope),
            alias=alias,
            expected_alias_version=expected_alias_version,
        )
        return {**record.to_dict(), "event_cursor": cursor}

    def resolve_alias(
        self, alias: str, *, scope: WorkScope | Mapping[str, Any] | None = None,
    ) -> ArtifactSnapshot:
        resolved = coerce_work_scope(scope) if scope is not None else current_work_scope()
        record = self.repository.resolve_alias(_scope_key(resolved), alias)
        return self.snapshot(record.artifact_id, record.revision)

    def link(
        self,
        artifact_id: str,
        *,
        owner_kind: str,
        owner_id: str,
        role: str,
        revision: int | None = None,
        expected_version: int,
    ) -> dict[str, Any]:
        self._require_live(artifact_id)
        selected = self.repository.get_revision(artifact_id, revision)
        record, cursor = self.repository.link(
            artifact_id,
            expected_version=expected_version,
            revision=selected.revision,
            owner_kind=owner_kind,
            owner_id=owner_id,
            role=role,
        )
        return {**record, "event_cursor": cursor}

    def export(
        self,
        artifact_id: str,
        destination: str,
        *,
        format: str = "",
        revision: int | None = None,
        overwrite: bool = False,
        cancellation: Any | None = None,
    ) -> dict[str, Any]:
        self._require_live(artifact_id)
        snapshot = self.snapshot(artifact_id, revision)
        selected_format = str(format or "").strip().lower().lstrip(".")
        candidates = [
            item for item in snapshot.renders
            if not selected_format or item.format == ("md" if selected_format == "markdown" else selected_format)
        ]
        if not candidates:
            raise ArtifactRuntimeNotFound("artifact has no matching rendered output")
        render = candidates[-1]
        result = self.artifact_store.export_to(
            render.output_ref,
            os.path.abspath(str(destination)),
            overwrite=overwrite,
            verify=True,
            cancellation=cancellation,
            pre_publish=lambda: self._require_live(artifact_id),
        )
        return {**result.to_dict(), "artifact_id": artifact_id,
                "revision": snapshot.revision.revision, "render_id": render.render_id}

    def tombstone(self, artifact_id: str, *, expected_version: int) -> dict[str, Any]:
        record, cursor = self.repository.tombstone(
            artifact_id, expected_version=expected_version,
        )
        return {**record.to_dict(), "event_cursor": cursor}

    def state(self) -> dict[str, Any]:
        return {
            "schema": "variant1.artifact-runtime.v1",
            "publish_job_kind": ARTIFACT_PUBLISH_JOB,
            "builder_version": BUILDER_VERSION,
            "validator_version": VALIDATOR_VERSION,
            "formats": sorted(_SUPPORTED_FORMATS - {"markdown"}),
            "repository": self.work.repository.path,
        }


def create_artifact_runtime(
    work: Any,
    artifact_store: Any,
    *,
    register_handler: bool = True,
) -> ArtifactRuntime:
    return ArtifactRuntime(work, artifact_store, register_handler=register_handler)


__all__ = [
    "ARTIFACT_PUBLISH_JOB",
    "ARTIFACT_SPEC_MEDIA_TYPE",
    "ArtifactRuntime",
    "create_artifact_runtime",
]
