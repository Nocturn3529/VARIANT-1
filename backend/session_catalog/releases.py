"""Offline catalog-release evidence pipeline.

This is not ordinary session infrastructure and is not installed in kernels.
Nothing here can move the live catalog pointer. The process host owns one
``CatalogReleases`` service for explicit admin/release workflows; chats and
kernels never construct or receive it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any

from core_invariants import canonical_digest, canonical_json, sqlite_session_connection
from tool_core import json_safe
from .profiles import CHAT_GRAPH_REVISION


FOUNDRY_EVIDENCE_SCHEMA = "variant1.astb.foundry-evidence.v1"
FOUNDRY_RELEASE_SCHEMA = "variant1.astb.capsule-release.v1"
EVIDENCE_LEVELS = frozenset({"E0", "E1", "E2", "E3", "E4"})


def _stable(value: Any) -> str:
    return canonical_json(json_safe(value))


def _digest(value: Any) -> str:
    return canonical_digest(json_safe(value))


class ReleaseError(RuntimeError):
    pass


class CatalogReleases:
    """Authoritative evidence records with no path to the live catalog pointer."""

    def __init__(
        self,
        database_path: str,
        *,
        artifact_store: Any,
        mutation: Any,
        catalog_repository: Any,
    ) -> None:
        self.path = os.path.abspath(database_path)
        self.artifact_store = artifact_store
        self.mutation = mutation
        self.catalog_repository = catalog_repository
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return sqlite_session_connection(self.path)

    def _initialize(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS catalog_release_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    draft_id TEXT NOT NULL,
                    level TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    evaluator_version TEXT NOT NULL,
                    payload_ref TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    public_json TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    privacy_class TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS catalog_release_evidence_draft_idx
                ON catalog_release_evidence(draft_id, level, created_at);
                CREATE TABLE IF NOT EXISTS catalog_release_replay (
                    replay_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    draft_id TEXT NOT NULL,
                    cases_ref TEXT NOT NULL,
                    cases_sha256 TEXT NOT NULL,
                    evaluator_version TEXT NOT NULL,
                    candidate_passes INTEGER NOT NULL,
                    baseline_passes INTEGER NOT NULL,
                    case_count INTEGER NOT NULL,
                    held_out INTEGER NOT NULL,
                    oracle_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    execution_json TEXT NOT NULL DEFAULT '{}',
                    promotion_eligible INTEGER NOT NULL DEFAULT 0,
                    evidence_id TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS catalog_release_release (
                    release_id TEXT PRIMARY KEY,
                    draft_id TEXT NOT NULL,
                    base_catalog_release_id TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL UNIQUE,
                    artifact_ref TEXT NOT NULL,
                    evidence_digest TEXT NOT NULL,
                    compatibility_json TEXT NOT NULL,
                    rollback_release_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    approved_by TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS catalog_release_revocation (
                    revocation_id TEXT PRIMARY KEY,
                    release_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS catalog_release_pointer (
                    channel TEXT PRIMARY KEY,
                    release_id TEXT NOT NULL,
                    previous_release_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            replay_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(catalog_release_replay)")
            }
            if "execution_json" not in replay_columns:
                if "isolation_json" in replay_columns:
                    conn.execute(
                        "ALTER TABLE catalog_release_replay RENAME COLUMN "
                        "isolation_json TO execution_json"
                    )
                else:
                    conn.execute(
                        "ALTER TABLE catalog_release_replay ADD COLUMN "
                        "execution_json TEXT NOT NULL DEFAULT '{}'"
                    )
            if "promotion_eligible" not in replay_columns:
                conn.execute(
                    "ALTER TABLE catalog_release_replay ADD COLUMN "
                    "promotion_eligible INTEGER NOT NULL DEFAULT 0"
                )

    def _record(
        self,
        *,
        chat_id: str,
        draft_id: str,
        level: str,
        kind: str,
        evaluator_version: str,
        payload: dict[str, Any],
        public: dict[str, Any],
        provenance: str,
        privacy_class: str = "private_local",
    ) -> dict[str, Any]:
        if level not in EVIDENCE_LEVELS:
            raise ReleaseError(f"unknown evidence level: {level}")
        body = {
            "schema": FOUNDRY_EVIDENCE_SCHEMA,
            "chat_id": str(chat_id),
            "draft_id": str(draft_id),
            "level": level,
            "kind": str(kind),
            "evaluator_version": str(evaluator_version),
            "payload": json_safe(payload),
            "provenance": str(provenance),
            "privacy_class": str(privacy_class),
        }
        artifact = self.artifact_store.put_json(
            body, kind="catalog_release_private_evidence",
            scope="astb.foundry.private",
        )
        public_body = {
            "schema": FOUNDRY_EVIDENCE_SCHEMA,
            "evidence_id": "",
            "chat_id": str(chat_id),
            "draft_id": str(draft_id),
            "level": level,
            "kind": str(kind),
            "evaluator_version": str(evaluator_version),
            "payload_sha256": artifact.sha256,
            "public": json_safe(public),
            "provenance": str(provenance),
            "privacy_class": str(privacy_class),
        }
        evidence_id = "evidence_" + uuid.uuid4().hex
        public_body["evidence_id"] = evidence_id
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO catalog_release_evidence(evidence_id, chat_id, draft_id, "
                "level, kind, evaluator_version, payload_ref, payload_sha256, "
                "public_json, provenance, privacy_class, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evidence_id, str(chat_id), str(draft_id), level, str(kind),
                    str(evaluator_version), artifact.ref, artifact.sha256,
                    _stable(public_body), str(provenance), str(privacy_class), time.time(),
                ),
            )
        return public_body

    def extract_episode(self, chat_id: str, draft_id: str) -> dict[str, Any]:
        draft = self.mutation._draft(chat_id, draft_id)
        with self._lock, self._connect() as conn:
            invocations = conn.execute(
                "SELECT status, observed_calls_json, result_sha256, duration_ms, created_at "
                "FROM mutation_invocation WHERE chat_id=? AND draft_id=? "
                "ORDER BY created_at",
                (str(chat_id), str(draft_id)),
            ).fetchall()
            probation = conn.execute(
                "SELECT * FROM mutation_probation WHERE chat_id=? AND draft_id=?",
                (str(chat_id), str(draft_id)),
            ).fetchone()
            receipts = conn.execute(
                "SELECT kind, receipt_digest, created_at FROM mutation_receipt "
                "WHERE chat_id=? AND draft_id=? ORDER BY created_at",
                (str(chat_id), str(draft_id)),
            ).fetchall()
        episode = {
            "schema": "variant1.astb.episode.v1",
            "task_input_digest": "revision_unavailable",
            "provider": "revision_unavailable",
            "model_id": "revision_unavailable",
            "provider_model_revision": "revision_unavailable",
            "harness_profile": "trusted-local.v1",
            "graph_revision": CHAT_GRAPH_REVISION,
            "catalog_release_id": draft["catalog_release_id"],
            "slot_id": draft["slot_id"],
            "source_sha256": draft["source_sha256"],
            "schema_sha256": _digest(draft["schema"]),
            "dependency_versions": [
                row.get("ref") for row in (draft.get("dependencies") or ())
            ],
            "bpe_views_exposed": "receipt_unavailable",
            "invocations": [
                {
                    "status": row["status"],
                    "observed_calls": json.loads(row["observed_calls_json"]),
                    "result_sha256": row["result_sha256"],
                    "duration_ms": float(row["duration_ms"]),
                    "created_at": float(row["created_at"]),
                }
                for row in invocations
            ],
            "probation": dict(probation) if probation is not None else None,
            "mutation_receipts": [dict(row) for row in receipts],
            "privacy_class": "private_local",
            "oracle_content_included": False,
        }
        levels = ["E0"]
        if draft.get("validation") and draft.get("host_test"):
            levels.append("E1")
        if (
            probation is not None
            and probation["status"] == "passed"
            and sum(1 for row in invocations if row["status"] == "ok") >= 2
        ):
            levels.append("E2")
        evidence = []
        for level in levels:
            evidence.append(self._record(
                chat_id=chat_id,
                draft_id=draft_id,
                level=level,
                kind="episode_extraction",
                evaluator_version="variant1.astb.episode-extractor.v1",
                payload=episode,
                public={
                    "slot_id": draft["slot_id"],
                    "source_sha256": draft["source_sha256"],
                    "invocation_count": len(invocations),
                    "probation_status": (
                        probation["status"] if probation is not None else None
                    ),
                    "oracle_content_included": False,
                },
                provenance="authoritative_mutation_repository",
            ))
        return {"episode_sha256": _digest(episode), "levels": levels, "evidence": evidence}

    async def replay(
        self,
        chat_id: str,
        draft_id: str,
        *,
        cases: list[dict[str, Any]],
        baseline_passes: int,
        held_out: bool,
        oracle_digest: str,
        evaluator_version: str,
    ) -> dict[str, Any]:
        if not held_out:
            raise ReleaseError("catalog replay must be explicitly held out")
        if not str(oracle_digest or "").strip():
            raise ReleaseError("catalog replay requires an oracle digest, never oracle content")
        private_cases = self.artifact_store.put_json(
            cases, kind="catalog_release_private_replay_cases",
            scope="astb.foundry.private",
        )
        tested = await self.mutation.test(
            chat_id, draft_id, list(cases), authority="replay", persist_status=False
        )
        results = list(tested.get("results") or ())
        candidate_passes = sum(1 for row in results if row.get("ok"))
        baseline = max(0, min(int(baseline_passes), len(cases)))
        uplift = candidate_passes - baseline
        status = "passed" if candidate_passes == len(cases) and uplift > 0 else "failed"
        execution = dict(self.mutation.worker.execution_status())
        promotion_eligible = status == "passed"
        replay_id = "replay_" + uuid.uuid4().hex
        public = {
            "replay_id": replay_id,
            "case_count": len(cases),
            "candidate_passes": candidate_passes,
            "baseline_passes": baseline,
            "uplift": uplift,
            "held_out": True,
            "oracle_digest": str(oracle_digest),
            "status": status,
            "execution_mode": str(execution.get("execution_mode") or "same_user"),
            "execution_grade": str(execution.get("execution_grade") or "unknown"),
            "promotion_eligible": promotion_eligible,
        }
        evidence = self._record(
            chat_id=chat_id,
            draft_id=draft_id,
            level="E3",
            kind="paired_held_out_replay",
            evaluator_version=evaluator_version,
            payload={
                **public,
                "cases_ref": private_cases.ref,
                "cases_sha256": private_cases.sha256,
                "test_receipt_digest": tested.get("receipt_digest"),
                "execution": execution,
            },
            public=public,
            provenance=(
                "promotion-eligible-user-authorized-replay-worker"
                if promotion_eligible
                else "nonpromoting-mutation-replay-worker"
            ),
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO catalog_release_replay(replay_id, chat_id, draft_id, cases_ref, "
                "cases_sha256, evaluator_version, candidate_passes, baseline_passes, "
                "case_count, held_out, oracle_digest, status, execution_json, "
                "promotion_eligible, evidence_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
                (
                    replay_id, str(chat_id), str(draft_id), private_cases.ref,
                    private_cases.sha256, str(evaluator_version), candidate_passes,
                    baseline, len(cases), str(oracle_digest), status,
                    _stable(execution), int(promotion_eligible),
                    evidence["evidence_id"], time.time(),
                ),
            )
        return {**public, "evidence": evidence}

    def approve_e4(
        self,
        chat_id: str,
        draft_id: str,
        *,
        security_report: dict[str, Any],
        canary_report: dict[str, Any],
        compatibility: dict[str, Any],
        approved_by: str,
        approval_note: str,
        evaluator_version: str,
    ) -> dict[str, Any]:
        if security_report.get("status") != "passed":
            raise ReleaseError("security report has not passed")
        if canary_report.get("status") != "passed":
            raise ReleaseError("canary report has not passed")
        if not approved_by or not approval_note:
            raise ReleaseError("E4 requires an explicit actor and approval note")
        if (
            not isinstance(compatibility, dict)
            or not str(compatibility.get("profile") or "").strip()
            or not isinstance(compatibility.get("models"), list)
            or not compatibility.get("models")
        ):
            raise ReleaseError(
                "E4 compatibility requires one disclosure profile and a non-empty model set"
            )
        with self._lock, self._connect() as conn:
            replay = conn.execute(
                "SELECT replay_id, promotion_eligible FROM catalog_release_replay "
                "WHERE chat_id=? AND draft_id=? AND status='passed' "
                "ORDER BY created_at DESC LIMIT 1",
                (str(chat_id), str(draft_id)),
            ).fetchone()
        if replay is None:
            raise ReleaseError("E4 requires a passing paired held-out replay")
        if not bool(replay["promotion_eligible"]):
            raise ReleaseError(
                "E4 requires a promotion-eligible passing held-out replay"
            )
        return self._record(
            chat_id=chat_id,
            draft_id=draft_id,
            level="E4",
            kind="release_approval",
            evaluator_version=evaluator_version,
            payload={
                "security_report": security_report,
                "canary_report": canary_report,
                "compatibility": compatibility,
                "approved_by": approved_by,
                "approval_note": approval_note,
                "replay_id": replay["replay_id"],
            },
            public={
                "security_status": "passed",
                "canary_status": "passed",
                "compatibility": compatibility,
                "approved_by": approved_by,
                "replay_id": replay["replay_id"],
            },
            provenance="explicit_operator_approval",
        )

    def publish_capsule(
        self,
        chat_id: str,
        draft_id: str,
        *,
        approved_by: str,
        compatibility: dict[str, Any],
    ) -> dict[str, Any]:
        draft = self.mutation._draft(chat_id, draft_id)
        with self._lock, self._connect() as conn:
            evidence_rows = conn.execute(
                "SELECT evidence_id, level, payload_sha256, public_json "
                "FROM catalog_release_evidence "
                "WHERE chat_id=? AND draft_id=? ORDER BY created_at, evidence_id",
                (str(chat_id), str(draft_id)),
            ).fetchall()
            e4 = next((row for row in reversed(evidence_rows) if row["level"] == "E4"), None)
            previous = conn.execute(
                "SELECT release_id FROM catalog_release_pointer WHERE channel='candidate'"
            ).fetchone()
        if e4 is None:
            raise ReleaseError("capsule publication requires E4 evidence")
        e4_public = json.loads(e4["public_json"])
        approved_compatibility = dict(
            (e4_public.get("public") or {}).get("compatibility") or {}
        )
        approved_actor = str((e4_public.get("public") or {}).get("approved_by") or "")
        if _stable(compatibility) != _stable(approved_compatibility):
            raise ReleaseError("capsule compatibility differs from the approved E4 record")
        if str(approved_by or "") != approved_actor:
            raise ReleaseError("capsule publisher differs from the approved E4 actor")
        source = self.artifact_store.read_bytes_scoped(
            draft["source_ref"], str(chat_id)
        ).decode("utf-8")
        evidence = [
            {
                "evidence_id": row["evidence_id"],
                "level": row["level"],
                "payload_sha256": row["payload_sha256"],
            }
            for row in evidence_rows
        ]
        capsule = {
            "schema": FOUNDRY_RELEASE_SCHEMA,
            "base_catalog_release_id": draft["catalog_release_id"],
            "slot_id": draft["slot_id"],
            "kind": draft["declared_kind"],
            "alias": draft["alias"],
            "purpose": draft["purpose"],
            "schema_document": draft["schema"],
            "source": source,
            "source_sha256": draft["source_sha256"],
            "dependencies": draft["dependencies"],
            "environment": "variant1.astb.mutation-worker.v2",
            "evidence": evidence,
            "compatibility": compatibility,
            "approved_by": str(approved_by),
            "live_catalog_pointer_changed": False,
        }
        artifact = self.artifact_store.put_json(
            capsule, kind="catalog_release_capsule_release",
            scope="astb.foundry.releases",
        )
        release_id = "astb.capsule." + artifact.sha256[:24] + ".v1"
        rollback = str(previous["release_id"] if previous is not None else "")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM catalog_release_release WHERE release_id=?", (release_id,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO catalog_release_release(release_id, draft_id, "
                    "base_catalog_release_id, content_sha256, artifact_ref, evidence_digest, "
                    "compatibility_json, rollback_release_id, status, approved_by, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'approved_unselected', ?, ?)",
                    (
                        release_id, str(draft_id), draft["catalog_release_id"],
                        artifact.sha256, artifact.ref, _digest(evidence),
                        _stable(compatibility), rollback, str(approved_by), time.time(),
                    ),
                )
            elif existing["content_sha256"] != artifact.sha256:
                raise ReleaseError("immutable catalog release collision")
            conn.commit()
        return {
            "release_id": release_id,
            "content_sha256": artifact.sha256,
            "artifact_ref": artifact.ref,
            "status": "approved_unselected",
            "live_catalog_pointer_changed": False,
            "rollback_release_id": rollback or None,
        }

    def select_release(self, release_id: str, *, channel: str = "candidate") -> dict[str, Any]:
        clean_channel = str(channel or "candidate")
        if clean_channel not in {"candidate", "canary"}:
            raise ReleaseError("catalog releases may select only candidate or canary channels")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            release = conn.execute(
                "SELECT * FROM catalog_release_release WHERE release_id=?", (str(release_id),)
            ).fetchone()
            if release is None or release["status"] == "revoked":
                raise ReleaseError("catalog release is unavailable")
            prior = conn.execute(
                "SELECT release_id, previous_release_id, revision "
                "FROM catalog_release_pointer WHERE channel=?",
                (clean_channel,),
            ).fetchone()
            previous = str(prior["release_id"] if prior is not None else "")
            if previous == str(release_id):
                revision = int(prior["revision"])
                conn.rollback()
                return {
                    "channel": clean_channel, "release_id": str(release_id),
                    "previous_release_id": str(prior["previous_release_id"] or "") or None,
                    "revision": revision, "already_selected": True,
                    "live_catalog_pointer_changed": False,
                }
            revision = int(prior["revision"] if prior is not None else 0) + 1
            conn.execute(
                "INSERT INTO catalog_release_pointer(channel, release_id, previous_release_id, "
                "revision, updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(channel) "
                "DO UPDATE SET previous_release_id=catalog_release_pointer.release_id, "
                "release_id=excluded.release_id, revision=excluded.revision, "
                "updated_at=excluded.updated_at",
                (clean_channel, str(release_id), previous, revision, time.time()),
            )
            conn.execute(
                "UPDATE catalog_release_release SET status=? WHERE release_id=?",
                ("selected_" + clean_channel, str(release_id)),
            )
            conn.commit()
        return {
            "channel": clean_channel, "release_id": str(release_id),
            "previous_release_id": previous or None, "revision": revision,
            "live_catalog_pointer_changed": False,
        }

    def rollback(self, *, channel: str = "candidate") -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM catalog_release_pointer WHERE channel=?", (str(channel),)
            ).fetchone()
        if row is None or not row["previous_release_id"]:
            raise ReleaseError("catalog channel has no rollback release")
        return self.select_release(str(row["previous_release_id"]), channel=str(channel))

    def revoke(self, release_id: str, *, reason: str, actor: str) -> dict[str, Any]:
        if not str(reason or "").strip() or not str(actor or "").strip():
            raise ReleaseError("revocation requires a reason and actor")
        revocation_id = "revocation_" + uuid.uuid4().hex
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM catalog_release_release WHERE release_id=?", (str(release_id),)
            ).fetchone() is None:
                raise ReleaseError("unknown catalog release")
            conn.execute(
                "INSERT INTO catalog_release_revocation(revocation_id, release_id, reason, "
                "actor, created_at) VALUES (?, ?, ?, ?, ?)",
                (revocation_id, str(release_id), str(reason), str(actor), time.time()),
            )
            conn.execute(
                "UPDATE catalog_release_release SET status='revoked' WHERE release_id=?",
                (str(release_id),),
            )
            pointers = conn.execute(
                "SELECT * FROM catalog_release_pointer WHERE release_id=?",
                (str(release_id),),
            ).fetchall()
            restored: dict[str, str | None] = {}
            for pointer in pointers:
                prior_id = str(pointer["previous_release_id"] or "")
                prior = conn.execute(
                    "SELECT status FROM catalog_release_release WHERE release_id=?",
                    (prior_id,),
                ).fetchone() if prior_id else None
                if prior is not None and prior["status"] != "revoked":
                    conn.execute(
                        "UPDATE catalog_release_pointer SET release_id=?, "
                        "previous_release_id='', revision=revision+1, updated_at=? "
                        "WHERE channel=?",
                        (prior_id, time.time(), pointer["channel"]),
                    )
                    restored[str(pointer["channel"])] = prior_id
                else:
                    conn.execute(
                        "DELETE FROM catalog_release_pointer WHERE channel=?",
                        (pointer["channel"],),
                    )
                    restored[str(pointer["channel"])] = None
            conn.commit()
        return {
            "revocation_id": revocation_id, "release_id": str(release_id),
            "status": "revoked", "reason": str(reason), "actor": str(actor),
            "restored_channels": restored,
        }

    def status(self, *, chat_id: str = "", draft_id: str = "", limit: int = 20) -> dict[str, Any]:
        clauses: list[str] = []
        values: list[Any] = []
        if chat_id:
            clauses.append("chat_id=?")
            values.append(str(chat_id))
        if draft_id:
            clauses.append("draft_id=?")
            values.append(str(draft_id))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        cap = max(1, min(int(limit), 100))
        with self._lock, self._connect() as conn:
            evidence = conn.execute(
                "SELECT public_json, created_at FROM catalog_release_evidence" + where
                + " ORDER BY created_at DESC LIMIT ?", (*values, cap),
            ).fetchall()
            releases = conn.execute(
                "SELECT release_id, draft_id, base_catalog_release_id, content_sha256, "
                "artifact_ref, compatibility_json, rollback_release_id, status, approved_by, "
                "created_at FROM catalog_release_release ORDER BY created_at DESC LIMIT ?",
                (cap,),
            ).fetchall()
            pointers = conn.execute(
                "SELECT * FROM catalog_release_pointer ORDER BY channel"
            ).fetchall()
            revocations = conn.execute(
                "SELECT * FROM catalog_release_revocation ORDER BY created_at DESC LIMIT ?",
                (cap,),
            ).fetchall()
        return {
            "schema": "variant1.astb.foundry-status.v1",
            "evidence": [
                {**json.loads(row["public_json"]), "created_at": float(row["created_at"])}
                for row in evidence
            ],
            "releases": [
                {
                    **dict(row),
                    "compatibility": json.loads(row["compatibility_json"]),
                }
                for row in releases
            ],
            "pointers": [dict(row) for row in pointers],
            "revocations": [dict(row) for row in revocations],
            "live_catalog_pointer_mutable_from_foundry": False,
        }
