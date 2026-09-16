"""Transactional goal authority colocated with the Work Fabric database.

Every aggregate mutation advances ``workflow_goal.version`` and appends the
matching ``work_event`` plus ``work_outbox`` row in the same SQLite transaction.
The repository intentionally uses WorkRepository's writer boundary rather than
claiming cross-database atomicity.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from core_invariants import StrictJSONError, canonical_json, strict_json_value
from work_fabric.models import WorkActor, WorkConflict
from work_fabric.interactions import InteractionService
from work_fabric.repository import WorkRepository
from work_fabric.scope import WorkScope

from .models import (
    ATTENTION_STATES,
    EFFECT_STATES,
    GOAL_STATES,
    STEP_KINDS,
    STEP_STATES,
    WAIT_STATES,
    AttentionRecord,
    EffectRecord,
    GoalArtifactRecord,
    GoalConflict,
    GoalNotFound,
    GoalRecord,
    GoalTransitionError,
    GoalValidationError,
    StepAttemptRecord,
    StepRecord,
    WaitRecord,
)


MAX_PLAN_STEPS = 256
MAX_JSON_BYTES = 2 * 1024 * 1024


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _text(value: Any, field: str, *, required: bool = False, limit: int = 1000) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise GoalValidationError(f"{field} is required")
    if "\x00" in result:
        raise GoalValidationError(f"{field} contains a NUL character")
    if len(result) > limit:
        raise GoalValidationError(f"{field} exceeds {limit} characters")
    return result


def _json_value(value: Any, path: str = "$") -> Any:
    try:
        return strict_json_value(value, path=path)
    except StrictJSONError as exc:
        raise GoalValidationError(str(exc)) from exc


def _json(value: Any) -> str:
    encoded = canonical_json(_json_value(value))
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise GoalValidationError("inline goal JSON exceeds 2 MiB; use an artifact")
    return encoded


def _load(value: str | None, expected: type, field: str) -> Any:
    try:
        result = json.loads(value or ("{}" if expected is dict else "[]"))
    except Exception as exc:
        raise GoalValidationError(f"persisted {field} is invalid JSON: {exc}") from exc
    if not isinstance(result, expected):
        raise GoalValidationError(f"persisted {field} is not {expected.__name__}")
    return result


def _actor(value: WorkActor | None) -> WorkActor:
    return value or WorkActor("system", "goal-service")


class GoalRepository:
    """Synchronous durable repository over one existing WorkRepository."""

    def __init__(self, work_repository: WorkRepository) -> None:
        if not isinstance(work_repository, WorkRepository):
            raise TypeError("GoalRepository requires a WorkRepository")
        self.work = work_repository
        self.interactions = InteractionService(work_repository)
        self.path = work_repository.path
        self._initialize()

    def _initialize(self) -> None:
        with self.work._write_lock:
            conn = self.work._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS workflow_goal (
                        goal_id TEXT PRIMARY KEY,
                        owner_chat_id TEXT NOT NULL DEFAULT '',
                        workspace_id TEXT NOT NULL DEFAULT '',
                        title TEXT NOT NULL,
                        objective TEXT NOT NULL,
                        constraints_json TEXT NOT NULL DEFAULT '[]',
                        success_criteria_json TEXT NOT NULL DEFAULT '[]',
                        completion_policy_json TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL CHECK(status IN (
                          'draft','queued','running','waiting_user','waiting_external',
                          'blocked','paused','succeeded','failed','cancelled','archived')),
                        priority INTEGER NOT NULL DEFAULT 0,
                        version INTEGER NOT NULL DEFAULT 1,
                        budget_limits_json TEXT NOT NULL DEFAULT '{}',
                        budget_usage_json TEXT NOT NULL DEFAULT '{}',
                        deadline REAL,
                        pause_reason TEXT NOT NULL DEFAULT '',
                        active_worktree_id TEXT NOT NULL DEFAULT '',
                        terminal_summary_ref TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        completed_at REAL,
                        archived_at REAL
                    );
                    CREATE INDEX IF NOT EXISTS idx_workflow_goal_status
                      ON workflow_goal(status, priority DESC, updated_at, goal_id);
                    CREATE INDEX IF NOT EXISTS idx_workflow_goal_owner
                      ON workflow_goal(owner_chat_id, workspace_id, updated_at DESC);

                    CREATE TABLE IF NOT EXISTS workflow_step (
                        step_id TEXT PRIMARY KEY,
                        goal_id TEXT NOT NULL,
                        parent_step_id TEXT NOT NULL DEFAULT '',
                        ordinal INTEGER NOT NULL,
                        kind TEXT NOT NULL CHECK(kind IN (
                          'agent','python','process','child','verification','input','wait','integration')),
                        instructions TEXT NOT NULL DEFAULT '',
                        config_json TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL CHECK(status IN (
                          'pending','ready','leased','running','waiting','retry_scheduled',
                          'succeeded','failed','blocked','skipped','cancelled')),
                        required INTEGER NOT NULL DEFAULT 1,
                        version INTEGER NOT NULL DEFAULT 1,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        max_attempts INTEGER NOT NULL DEFAULT 1,
                        retry_policy_json TEXT NOT NULL DEFAULT '{}',
                        native_thread_id TEXT NOT NULL DEFAULT '',
                        snapshot_cursor_json TEXT NOT NULL DEFAULT '{}',
                        child_id TEXT NOT NULL DEFAULT '',
                        process_id TEXT NOT NULL DEFAULT '',
                        worktree_id TEXT NOT NULL DEFAULT '',
                        wait_spec_json TEXT NOT NULL DEFAULT '{}',
                        verification_spec_json TEXT NOT NULL DEFAULT '{}',
                        result_ref TEXT NOT NULL DEFAULT '',
                        error_ref TEXT NOT NULL DEFAULT '',
                        error TEXT NOT NULL DEFAULT '',
                        lease_owner TEXT NOT NULL DEFAULT '',
                        lease_epoch INTEGER NOT NULL DEFAULT 0,
                        lease_expires_at REAL,
                        available_at REAL NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        completed_at REAL,
                        FOREIGN KEY(goal_id) REFERENCES workflow_goal(goal_id)
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_workflow_step_ordinal
                      ON workflow_step(goal_id, ordinal);
                    CREATE INDEX IF NOT EXISTS idx_workflow_step_ready
                      ON workflow_step(goal_id, status, available_at, ordinal);

                    CREATE TABLE IF NOT EXISTS workflow_step_dependency (
                        step_id TEXT NOT NULL,
                        depends_on_step_id TEXT NOT NULL,
                        PRIMARY KEY(step_id, depends_on_step_id),
                        FOREIGN KEY(step_id) REFERENCES workflow_step(step_id),
                        FOREIGN KEY(depends_on_step_id) REFERENCES workflow_step(step_id),
                        CHECK(step_id <> depends_on_step_id)
                    );

                    CREATE TABLE IF NOT EXISTS workflow_step_attempt (
                        attempt_id TEXT PRIMARY KEY,
                        goal_id TEXT NOT NULL,
                        step_id TEXT NOT NULL,
                        attempt INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        snapshot_thread_id TEXT NOT NULL DEFAULT '',
                        snapshot_cursor_json TEXT NOT NULL DEFAULT '{}',
                        machine_revision TEXT NOT NULL DEFAULT '',
                        native_run_id TEXT NOT NULL DEFAULT '',
                        last_receipt_id TEXT NOT NULL DEFAULT '',
                        result_ref TEXT NOT NULL DEFAULT '',
                        error_ref TEXT NOT NULL DEFAULT '',
                        started_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        completed_at REAL,
                        UNIQUE(step_id, attempt),
                        FOREIGN KEY(goal_id) REFERENCES workflow_goal(goal_id),
                        FOREIGN KEY(step_id) REFERENCES workflow_step(step_id)
                    );

                    CREATE TABLE IF NOT EXISTS workflow_goal_state (
                        goal_id TEXT NOT NULL,
                        state_key TEXT NOT NULL,
                        value_json TEXT NOT NULL,
                        version INTEGER NOT NULL DEFAULT 1,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY(goal_id, state_key),
                        FOREIGN KEY(goal_id) REFERENCES workflow_goal(goal_id)
                    );

                    CREATE TABLE IF NOT EXISTS workflow_attention (
                        attention_id TEXT PRIMARY KEY,
                        goal_id TEXT NOT NULL,
                        step_id TEXT NOT NULL DEFAULT '',
                        kind TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('open','answered','dismissed')),
                        prompt TEXT NOT NULL,
                        schema_json TEXT NOT NULL DEFAULT '{}',
                        response_json TEXT NOT NULL DEFAULT 'null',
                        version INTEGER NOT NULL DEFAULT 1,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        resolved_at REAL,
                        FOREIGN KEY(goal_id) REFERENCES workflow_goal(goal_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_workflow_attention_open
                      ON workflow_attention(goal_id, status, created_at);

                    CREATE TABLE IF NOT EXISTS workflow_wait (
                        wait_id TEXT PRIMARY KEY,
                        goal_id TEXT NOT NULL,
                        step_id TEXT NOT NULL,
                        source TEXT NOT NULL,
                        matcher_json TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL CHECK(status IN ('pending','satisfied','cancelled')),
                        wake_at REAL,
                        result_json TEXT NOT NULL DEFAULT '{}',
                        event_id TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        satisfied_at REAL,
                        FOREIGN KEY(goal_id) REFERENCES workflow_goal(goal_id),
                        FOREIGN KEY(step_id) REFERENCES workflow_step(step_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_workflow_wait_pending
                      ON workflow_wait(status, wake_at, source, goal_id);

                    CREATE TABLE IF NOT EXISTS workflow_effect (
                        effect_id TEXT PRIMARY KEY,
                        goal_id TEXT NOT NULL,
                        step_id TEXT NOT NULL,
                        attempt INTEGER NOT NULL DEFAULT 0,
                        sequence INTEGER NOT NULL,
                        kind TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN (
                          'planned','dispatched','succeeded','failed','cancelled','unknown_effect')),
                        idempotency_key TEXT NOT NULL,
                        request_json TEXT NOT NULL DEFAULT '{}',
                        response_json TEXT NOT NULL DEFAULT '{}',
                        receipt_id TEXT NOT NULL DEFAULT '',
                        error TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        UNIQUE(goal_id, sequence),
                        UNIQUE(goal_id, idempotency_key),
                        FOREIGN KEY(goal_id) REFERENCES workflow_goal(goal_id),
                        FOREIGN KEY(step_id) REFERENCES workflow_step(step_id)
                    );

                    CREATE TABLE IF NOT EXISTS workflow_goal_artifact (
                        link_id TEXT PRIMARY KEY,
                        goal_id TEXT NOT NULL,
                        step_id TEXT NOT NULL DEFAULT '',
                        artifact_ref TEXT NOT NULL,
                        role TEXT NOT NULL,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at REAL NOT NULL,
                        UNIQUE(goal_id, step_id, artifact_ref, role),
                        FOREIGN KEY(goal_id) REFERENCES workflow_goal(goal_id)
                    );

                    """
                )
            finally:
                conn.close()

    @staticmethod
    def _goal(row: sqlite3.Row | None) -> GoalRecord | None:
        if row is None:
            return None
        return GoalRecord(
            goal_id=str(row["goal_id"]), owner_chat_id=str(row["owner_chat_id"] or ""),
            workspace_id=str(row["workspace_id"] or ""),
            title=str(row["title"]), objective=str(row["objective"]),
            constraints=tuple(_load(row["constraints_json"], list, "constraints")),
            success_criteria=tuple(
                dict(item) for item in _load(row["success_criteria_json"], list, "success criteria")
                if isinstance(item, Mapping)
            ),
            completion_policy=_load(row["completion_policy_json"], dict, "completion policy"),
            status=str(row["status"]), priority=int(row["priority"]), version=int(row["version"]),
            budget_limits=_load(row["budget_limits_json"], dict, "budget limits"),
            budget_usage=_load(row["budget_usage_json"], dict, "budget usage"),
            deadline=float(row["deadline"] or 0), pause_reason=str(row["pause_reason"] or ""),
            active_worktree_id=str(row["active_worktree_id"] or ""),
            terminal_summary_ref=str(row["terminal_summary_ref"] or ""),
            created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
            completed_at=float(row["completed_at"] or 0), archived_at=float(row["archived_at"] or 0),
        )

    @staticmethod
    def _step(row: sqlite3.Row | None) -> StepRecord | None:
        if row is None:
            return None
        return StepRecord(
            step_id=str(row["step_id"]), goal_id=str(row["goal_id"]),
            parent_step_id=str(row["parent_step_id"] or ""), ordinal=int(row["ordinal"]),
            kind=str(row["kind"]), instructions=str(row["instructions"] or ""),
            config=_load(row["config_json"], dict, "step config"), status=str(row["status"]),
            required=bool(row["required"]), version=int(row["version"]),
            attempt_count=int(row["attempt_count"]), max_attempts=int(row["max_attempts"]),
            retry_policy=_load(row["retry_policy_json"], dict, "retry policy"),
            native_thread_id=str(row["native_thread_id"] or ""),
            snapshot_cursor=_load(row["snapshot_cursor_json"], dict, "snapshot cursor"),
            child_id=str(row["child_id"] or ""), process_id=str(row["process_id"] or ""),
            worktree_id=str(row["worktree_id"] or ""),
            wait_spec=_load(row["wait_spec_json"], dict, "wait spec"),
            verification_spec=_load(row["verification_spec_json"], dict, "verification spec"),
            result_ref=str(row["result_ref"] or ""), error_ref=str(row["error_ref"] or ""),
            error=str(row["error"] or ""), lease_owner=str(row["lease_owner"] or ""),
            lease_epoch=int(row["lease_epoch"]), lease_expires_at=float(row["lease_expires_at"] or 0),
            available_at=float(row["available_at"] or 0), created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]), completed_at=float(row["completed_at"] or 0),
        )

    @staticmethod
    def _attempt(row: sqlite3.Row | None) -> StepAttemptRecord | None:
        if row is None:
            return None
        return StepAttemptRecord(
            attempt_id=str(row["attempt_id"]), goal_id=str(row["goal_id"]),
            step_id=str(row["step_id"]), attempt=int(row["attempt"]), status=str(row["status"]),
            snapshot_thread_id=str(row["snapshot_thread_id"] or ""),
            snapshot_cursor=_load(row["snapshot_cursor_json"], dict, "attempt cursor"),
            machine_revision=str(row["machine_revision"] or ""),
            native_run_id=str(row["native_run_id"] or ""),
            last_receipt_id=str(row["last_receipt_id"] or ""),
            result_ref=str(row["result_ref"] or ""), error_ref=str(row["error_ref"] or ""),
            started_at=float(row["started_at"]), updated_at=float(row["updated_at"]),
            completed_at=float(row["completed_at"] or 0),
        )

    def _scope(self, goal: GoalRecord, *, step_id: str = "", attempt: int = 0) -> WorkScope:
        return WorkScope(
            chat_id=goal.owner_chat_id, workspace_id=goal.workspace_id,
            goal_id=goal.goal_id, step_id=step_id, attempt=attempt,
        )

    def _require_goal_tx(self, conn: sqlite3.Connection, goal_id: str) -> GoalRecord:
        goal = self._goal(conn.execute(
            "SELECT * FROM workflow_goal WHERE goal_id=?", (str(goal_id),)
        ).fetchone())
        if goal is None:
            raise GoalNotFound(f"unknown goal: {goal_id}")
        return goal

    def _require_step_tx(self, conn: sqlite3.Connection, goal_id: str, step_id: str) -> StepRecord:
        step = self._step(conn.execute(
            "SELECT * FROM workflow_step WHERE goal_id=? AND step_id=?",
            (str(goal_id), str(step_id)),
        ).fetchone())
        if step is None:
            raise GoalNotFound(f"unknown goal step: {step_id}")
        return step

    def _check_version(self, goal: GoalRecord, expected_version: int) -> None:
        if int(expected_version) != goal.version:
            raise GoalConflict(
                f"goal version changed ({goal.version} != {int(expected_version)})"
            )

    def _advance_tx(
        self,
        conn: sqlite3.Connection,
        goal: GoalRecord,
        *,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        actor: WorkActor | None = None,
        correlation_id: str = "",
        causation_id: str = "",
        idempotency_key: str = "",
        step_id: str = "",
        attempt: int = 0,
        now: float | None = None,
    ) -> int:
        at = float(time.time() if now is None else now)
        next_version = goal.version + 1
        changed = conn.execute(
            "UPDATE workflow_goal SET version=?, updated_at=? WHERE goal_id=? AND version=?",
            (next_version, at, goal.goal_id, goal.version),
        )
        if changed.rowcount != 1:
            raise GoalConflict("goal version changed during mutation")
        try:
            self.work._insert_event_tx(
                conn, event_type=event_type, aggregate_kind="goal",
                aggregate_id=goal.goal_id, aggregate_version=next_version,
                expected_aggregate_version=goal.version,
                scope=self._scope(goal, step_id=step_id, attempt=attempt),
                actor=_actor(actor), correlation_id=correlation_id,
                causation_id=causation_id, idempotency_key=idempotency_key,
                payload=dict(payload or {}), created_at=at,
            )
        except WorkConflict as exc:
            raise GoalConflict(str(exc)) from exc
        return next_version

    def create_goal(
        self,
        *,
        title: str,
        objective: str,
        owner_chat_id: str = "",
        workspace_id: str = "",
        constraints: Sequence[Any] = (),
        success_criteria: Sequence[Mapping[str, Any]] = (),
        completion_policy: Mapping[str, Any] | None = None,
        priority: int = 0,
        budget_limits: Mapping[str, Any] | None = None,
        deadline: float = 0.0,
        initial_state: Mapping[str, Any] | None = None,
        goal_id: str = "",
        actor: WorkActor | None = None,
        correlation_id: str = "",
    ) -> GoalRecord:
        gid = _text(goal_id or _id("goal"), "goal_id", required=True, limit=512)
        clean_title = _text(title, "title", required=True, limit=500)
        clean_objective = _text(objective, "objective", required=True, limit=32_000)
        criteria = [dict(item) for item in success_criteria]
        if len(criteria) > 256:
            raise GoalValidationError("success criteria exceed 256 entries")
        clean_owner_chat_id = _text(owner_chat_id, "owner_chat_id", limit=512)
        clean_workspace_id = _text(workspace_id, "workspace_id", limit=512)
        constraints_json = _json(list(constraints))
        criteria_json = _json(criteria)
        completion_json = _json(dict(completion_policy or {"kind": "all_required"}))
        budget_json = _json(dict(budget_limits or {}))
        clean_deadline = float(deadline)
        if not math.isfinite(clean_deadline) or clean_deadline < 0:
            raise GoalValidationError("deadline must be a finite non-negative timestamp")
        now = time.time()
        with self.work._write() as conn:
            existing = self._goal(conn.execute(
                "SELECT * FROM workflow_goal WHERE goal_id=?", (gid,)
            ).fetchone())
            if existing is not None:
                identity_matches = (
                    existing.title == clean_title
                    and existing.objective == clean_objective
                    and existing.owner_chat_id == clean_owner_chat_id
                    and existing.workspace_id == clean_workspace_id
                    and list(existing.constraints) == json.loads(constraints_json)
                    and [dict(item) for item in existing.success_criteria] == json.loads(criteria_json)
                    and dict(existing.completion_policy) == json.loads(completion_json)
                    and existing.priority == int(priority)
                    and dict(existing.budget_limits) == json.loads(budget_json)
                    and existing.deadline == (clean_deadline or 0.0)
                )
                for raw_key, value in dict(initial_state or {}).items():
                    key = _text(raw_key, "state key", required=True, limit=240)
                    row = conn.execute(
                        "SELECT value_json FROM workflow_goal_state WHERE goal_id=? AND state_key=?",
                        (gid, key),
                    ).fetchone()
                    if row is None or json.loads(str(row["value_json"])) != json.loads(_json(value)):
                        identity_matches = False
                        break
                if not identity_matches:
                    raise GoalConflict(
                        "goal idempotency identity was reused with different content"
                    )
                return existing
            conn.execute(
                "INSERT INTO workflow_goal(goal_id,owner_chat_id,workspace_id,"
                "title,objective,constraints_json,success_criteria_json,completion_policy_json,"
                "status,priority,version,budget_limits_json,budget_usage_json,deadline,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,'draft',?,1,?,'{}',?,?,?)",
                (
                    gid, clean_owner_chat_id, clean_workspace_id,
                    clean_title, clean_objective, constraints_json, criteria_json,
                    completion_json, int(priority), budget_json,
                    clean_deadline or None, now, now,
                ),
            )
            for raw_key, value in dict(initial_state or {}).items():
                key = _text(raw_key, "state key", required=True, limit=240)
                conn.execute(
                    "INSERT INTO workflow_goal_state(goal_id,state_key,value_json,version,updated_at) "
                    "VALUES (?,?,?,1,?)", (gid, key, _json(value), now),
                )
            created = self._require_goal_tx(conn, gid)
            try:
                self.work._insert_event_tx(
                    conn, event_type="goal.created", aggregate_kind="goal",
                    aggregate_id=gid, aggregate_version=1, expected_aggregate_version=0,
                    scope=self._scope(created), actor=_actor(actor),
                    correlation_id=correlation_id,
                    payload={"status": "draft", "title": clean_title}, created_at=now,
                )
            except WorkConflict as exc:
                raise GoalConflict(str(exc)) from exc
            return created

    def get_goal(self, goal_id: str) -> GoalRecord | None:
        with self.work._read() as conn:
            return self._goal(conn.execute(
                "SELECT * FROM workflow_goal WHERE goal_id=?", (str(goal_id),)
            ).fetchone())

    def require_goal(self, goal_id: str) -> GoalRecord:
        goal = self.get_goal(goal_id)
        if goal is None:
            raise GoalNotFound(f"unknown goal: {goal_id}")
        return goal

    def latest_composer_goal(self, chat_id: str, *, active_only: bool = False):
        query = """SELECT * FROM workflow_goal WHERE owner_chat_id=?
                   AND json_extract(completion_policy_json,'$.entrypoint')='composer_goal'"""
        if active_only:
            query += " AND status NOT IN ('succeeded','failed','cancelled','archived')"
        query += " ORDER BY created_at DESC, goal_id DESC LIMIT 1"
        with self.work._read() as conn:
            return self._goal(conn.execute(query, (chat_id,)).fetchone())

    def active_composer_goal(self, chat_id: str):
        return self.latest_composer_goal(chat_id, active_only=True)

    def list_goals(
        self, *, status: str = "", owner_chat_id: str = "", workspace_id: str = "",
        limit: int = 100,
    ) -> list[GoalRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            if status not in GOAL_STATES:
                raise GoalValidationError(f"unsupported goal status: {status}")
            clauses.append("status=?"); params.append(status)
        if owner_chat_id:
            clauses.append("owner_chat_id=?"); params.append(str(owner_chat_id))
        if workspace_id:
            clauses.append("workspace_id=?"); params.append(str(workspace_id))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(500, int(limit))))
        with self.work._read() as conn:
            rows = conn.execute(
                "SELECT * FROM workflow_goal" + where
                + " ORDER BY priority DESC, updated_at DESC, goal_id LIMIT ?", params,
            ).fetchall()
        return [item for row in rows if (item := self._goal(row)) is not None]

    def scan_goals(
        self, *, after_goal_id: str = "", limit: int = 500,
    ) -> list[GoalRecord]:
        """Return a stable, exhaustive goal-id page for recovery passes."""

        cap = max(1, min(500, int(limit)))
        with self.work._read() as conn:
            rows = conn.execute(
                "SELECT * FROM workflow_goal WHERE goal_id>? "
                "ORDER BY goal_id LIMIT ?",
                (str(after_goal_id or ""), cap),
            ).fetchall()
        return [item for row in rows if (item := self._goal(row)) is not None]

    def count_goals(self, *, status: str = "") -> int:
        if status and status not in GOAL_STATES:
            raise GoalValidationError(f"unsupported goal status: {status}")
        sql = "SELECT COUNT(*) FROM workflow_goal"
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status=?"
            params = (str(status),)
        with self.work._read() as conn:
            return int(conn.execute(sql, params).fetchone()[0])

    def event_cursor(self, goal_id: str) -> int:
        self.require_goal(goal_id)
        with self.work._read() as conn:
            return int(conn.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM work_event "
                "WHERE aggregate_kind='goal' AND aggregate_id=?",
                (str(goal_id),),
            ).fetchone()[0])

    @staticmethod
    def _normalize_plan(raw_steps: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        if not raw_steps or len(raw_steps) > MAX_PLAN_STEPS:
            raise GoalValidationError(f"plan must contain 1-{MAX_PLAN_STEPS} steps")
        rows: list[dict[str, Any]] = []
        ids: set[str] = set()
        for ordinal, raw in enumerate(raw_steps):
            if not isinstance(raw, Mapping):
                raise GoalValidationError("each plan step must be an object")
            step_id = _text(raw.get("step_id") or _id("step"), "step_id", required=True, limit=512)
            if step_id in ids:
                raise GoalValidationError(f"duplicate step id: {step_id}")
            ids.add(step_id)
            kind = _text(raw.get("kind"), "step kind", required=True, limit=40)
            if kind not in STEP_KINDS:
                raise GoalValidationError(f"unsupported step kind: {kind}")
            config = dict(raw.get("config") or {})
            wait_spec = dict(raw.get("wait_spec") or {})
            if kind == "wait":
                if not wait_spec:
                    wait_spec = dict(config)
                source = _text(
                    wait_spec.get("source") or "time",
                    "wait source", required=True, limit=100,
                )
                if source == "time":
                    try:
                        wake_at = float(wait_spec.get("wake_at") or 0)
                        delay_s = float(wait_spec.get("delay_s") or 0)
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise GoalValidationError(
                            "time wait wake_at/delay_s must be numeric"
                        ) from exc
                    if wake_at <= 0 and delay_s <= 0:
                        raise GoalValidationError(
                            "time wait requires a positive wake_at or delay_s"
                        )
                wait_spec["source"] = source
            deps = tuple(dict.fromkeys(
                _text(item, "dependency", required=True, limit=512)
                for item in (raw.get("dependencies") or ())
            ))
            rows.append({
                "step_id": step_id, "ordinal": ordinal,
                "parent_step_id": _text(raw.get("parent_step_id"), "parent_step_id", limit=512),
                "kind": kind, "instructions": _text(raw.get("instructions"), "instructions", limit=32_000),
                "config": config, "dependencies": deps,
                "required": bool(raw.get("required", True)),
                "max_attempts": max(1, min(100, int(raw.get("max_attempts") or 1))),
                "retry_policy": dict(raw.get("retry_policy") or {}),
                "wait_spec": wait_spec,
                "verification_spec": dict(raw.get("verification_spec") or {}),
            })
        by_id = {row["step_id"]: row for row in rows}
        for row in rows:
            if row["parent_step_id"] and row["parent_step_id"] not in by_id:
                raise GoalValidationError(f"unknown parent step: {row['parent_step_id']}")
            for dep in row["dependencies"]:
                if dep not in by_id:
                    raise GoalValidationError(f"unknown dependency: {dep}")
                if dep == row["step_id"]:
                    raise GoalValidationError("a step cannot depend on itself")
        visiting: set[str] = set(); visited: set[str] = set()
        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise GoalValidationError("step dependency graph contains a cycle")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dep in by_id[step_id]["dependencies"]:
                visit(dep)
            visiting.remove(step_id); visited.add(step_id)
        for step_id in by_id:
            visit(step_id)
        return rows

    def plan_goal(
        self, goal_id: str, steps: Sequence[Mapping[str, Any]], *, expected_version: int,
        actor: WorkActor | None = None, correlation_id: str = "",
    ) -> GoalRecord:
        plan = self._normalize_plan(steps)
        now = time.time()
        with self.work._write() as conn:
            goal = self._require_goal_tx(conn, goal_id); self._check_version(goal, expected_version)
            if goal.status != "draft":
                raise GoalTransitionError("only a draft goal can be planned")
            count = int(conn.execute(
                "SELECT COUNT(*) FROM workflow_step WHERE goal_id=?", (goal.goal_id,)
            ).fetchone()[0])
            if count:
                raise GoalConflict("goal already has a plan; create a new draft to replace it")
            for row in plan:
                conn.execute(
                    "INSERT INTO workflow_step(step_id,goal_id,parent_step_id,ordinal,kind,instructions,"
                    "config_json,status,required,version,attempt_count,max_attempts,retry_policy_json,"
                    "wait_spec_json,verification_spec_json,available_at,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,'pending',?,1,0,?,?,?,?,0,?,?)",
                    (
                        row["step_id"], goal.goal_id, row["parent_step_id"], row["ordinal"],
                        row["kind"], row["instructions"], _json(row["config"]), int(row["required"]),
                        row["max_attempts"], _json(row["retry_policy"]), _json(row["wait_spec"]),
                        _json(row["verification_spec"]), now, now,
                    ),
                )
            for row in plan:
                for dep in row["dependencies"]:
                    conn.execute(
                        "INSERT INTO workflow_step_dependency(step_id,depends_on_step_id) VALUES (?,?)",
                        (row["step_id"], dep),
                    )
            self._advance_tx(
                conn, goal, event_type="goal.planned", actor=actor,
                correlation_id=correlation_id,
                payload={"step_count": len(plan), "step_ids": [row["step_id"] for row in plan]},
                now=now,
            )
            return self._require_goal_tx(conn, goal.goal_id)

    def list_steps(self, goal_id: str) -> list[StepRecord]:
        self.require_goal(goal_id)
        with self.work._read() as conn:
            rows = conn.execute(
                "SELECT * FROM workflow_step WHERE goal_id=? ORDER BY ordinal,step_id",
                (str(goal_id),),
            ).fetchall()
        return [item for row in rows if (item := self._step(row)) is not None]

    def get_step(self, goal_id: str, step_id: str) -> StepRecord | None:
        with self.work._read() as conn:
            return self._step(conn.execute(
                "SELECT * FROM workflow_step WHERE goal_id=? AND step_id=?",
                (str(goal_id), str(step_id)),
            ).fetchone())

    def dependencies(self, goal_id: str) -> dict[str, tuple[str, ...]]:
        with self.work._read() as conn:
            rows = conn.execute(
                "SELECT d.step_id,d.depends_on_step_id FROM workflow_step_dependency d "
                "JOIN workflow_step s ON s.step_id=d.step_id WHERE s.goal_id=? "
                "ORDER BY d.step_id,d.depends_on_step_id", (str(goal_id),),
            ).fetchall()
        out: dict[str, list[str]] = {}
        for row in rows:
            out.setdefault(str(row["step_id"]), []).append(str(row["depends_on_step_id"]))
        return {key: tuple(value) for key, value in out.items()}

    def transition_goal(
        self,
        goal_id: str,
        status: str,
        *,
        expected_version: int,
        reason: str = "",
        terminal_summary_ref: str = "",
        actor: WorkActor | None = None,
        correlation_id: str = "",
        event_type: str = "",
    ) -> GoalRecord:
        target = _text(status, "goal status", required=True, limit=40)
        if target not in GOAL_STATES:
            raise GoalValidationError(f"unsupported goal status: {target}")
        allowed = {
            "draft": {"queued", "cancelled"},
            "queued": {"running", "paused", "cancelled", "blocked"},
            "running": {"waiting_user", "waiting_external", "blocked", "paused", "succeeded", "failed", "cancelled"},
            "waiting_user": {"running", "blocked", "paused", "succeeded", "failed", "cancelled"},
            "waiting_external": {"running", "blocked", "paused", "succeeded", "failed", "cancelled"},
            "blocked": {"running", "paused", "succeeded", "failed", "cancelled"},
            "paused": {"queued", "running", "cancelled"},
            "succeeded": {"archived"}, "failed": {"archived"},
            "cancelled": {"archived"}, "archived": set(),
        }
        now = time.time()
        with self.work._write() as conn:
            goal = self._require_goal_tx(conn, goal_id); self._check_version(goal, expected_version)
            if target == goal.status:
                return goal
            if target not in allowed.get(goal.status, set()):
                raise GoalTransitionError(f"cannot transition goal {goal.status} -> {target}")
            completed = now if target in {"succeeded", "failed", "cancelled"} else None
            archived = now if target == "archived" else None
            pause_reason = _text(reason, "reason", limit=4000) if target in {"paused", "blocked"} else ""
            conn.execute(
                "UPDATE workflow_goal SET status=?,pause_reason=?,terminal_summary_ref=CASE WHEN ?<>'' "
                "THEN ? ELSE terminal_summary_ref END,completed_at=COALESCE(?,completed_at),"
                "archived_at=COALESCE(?,archived_at) WHERE goal_id=?",
                (target, pause_reason, str(terminal_summary_ref), str(terminal_summary_ref),
                 completed, archived, goal.goal_id),
            )
            if target == "cancelled":
                conn.execute(
                    "UPDATE workflow_wait SET status='cancelled' "
                    "WHERE goal_id=? AND status='pending'",
                    (goal.goal_id,),
                )
                conn.execute(
                    "UPDATE workflow_attention SET status='dismissed',version=version+1,"
                    "updated_at=?,resolved_at=? WHERE goal_id=? AND status='open'",
                    (now, now, goal.goal_id),
                )
            self._advance_tx(
                conn, goal, event_type=event_type or f"goal.{target}", actor=actor,
                correlation_id=correlation_id,
                payload={"from": goal.status, "to": target, "reason": reason or None}, now=now,
            )
            return self._require_goal_tx(conn, goal.goal_id)

    def cancel_goal(
        self,
        goal_id: str,
        *,
        expected_version: int,
        reason: str = "",
        actor: WorkActor | None = None,
        correlation_id: str = "",
        owner_chat_id: str | None = None,
    ) -> GoalRecord:
        """Atomically cancel a goal and every nonterminal child record."""

        clean_reason = _text(reason, "reason", limit=4000)
        now = time.time()
        cancellable_goal_states = {
            "draft", "queued", "running", "waiting_user", "waiting_external",
            "blocked", "paused", "cancelled",
        }
        terminal_step_states = {
            "succeeded", "failed", "blocked", "skipped", "cancelled",
        }
        with self.work._write() as conn:
            goal = self._require_goal_tx(conn, goal_id)
            intent_key = None
            applied_to_version = goal.version
            if owner_chat_id is None:
                self._check_version(goal, expected_version)
            else:
                # A user's Stop targets this exact owned goal, not a transient
                # scheduler revision. Validate and admit it in the same write
                # transaction as cancellation, so progress cannot race a read.
                if not owner_chat_id or goal.owner_chat_id != owner_chat_id or not correlation_id:
                    raise GoalConflict('Cancel intent requires the exact goal owner and request identity')
                if type(expected_version) is not int or not 1 <= expected_version <= goal.version:
                    raise GoalConflict('Cancel intent has an invalid or future observed version')
                intent_key = 'cancel_request:' + hashlib.sha256(correlation_id.encode()).hexdigest()
                previous = conn.execute('SELECT value_json FROM workflow_goal_state WHERE goal_id=? AND state_key=?',
                                        (goal_id, intent_key)).fetchone()
                if previous:
                    prior = json.loads(previous['value_json'])
                    if prior['observed_version'] != expected_version or prior['reason'] != clean_reason:
                        raise GoalConflict('Cancel request_id has different content')
                    return goal
            terminal_cleanup = owner_chat_id is not None and goal.status in {'succeeded', 'failed', 'archived'}
            if goal.status not in cancellable_goal_states and not terminal_cleanup:
                raise GoalTransitionError(
                    f"cannot transition goal {goal.status} -> cancelled"
                )
            pending_cleanup={'status':'pending','complete':False,'requested_at':now,'reason':clean_reason,
                             'token':f'{correlation_id or _id("cleanup")}:{goal.version+1}'}
            conn.execute('''INSERT INTO workflow_goal_state(goal_id,state_key,value_json,version,updated_at)
                VALUES (?,'resource_cleanup',?,1,?) ON CONFLICT(goal_id,state_key) DO UPDATE SET
                value_json=excluded.value_json,version=workflow_goal_state.version+1,updated_at=excluded.updated_at''',
                (goal.goal_id,_json(pending_cleanup),now))
            self._advance_tx(conn,goal,event_type='goal.cleanup_requested',actor=actor,
                correlation_id=correlation_id,payload=pending_cleanup,now=now)
            goal=self._require_goal_tx(conn,goal.goal_id)
            steps = [
                step for row in conn.execute(
                    "SELECT * FROM workflow_step WHERE goal_id=? "
                    "ORDER BY ordinal,step_id",
                    (goal.goal_id,),
                ).fetchall()
                if (step := self._step(row)) is not None
                and step.status not in terminal_step_states
            ]
            if goal.status != "cancelled" and not terminal_cleanup:
                conn.execute(
                    "UPDATE workflow_goal SET status='cancelled',pause_reason='',"
                    "completed_at=COALESCE(completed_at,?),updated_at=? "
                    "WHERE goal_id=?",
                    (now, now, goal.goal_id),
                )
                self._advance_tx(
                    conn, goal, event_type="goal.cancelled", actor=actor,
                    correlation_id=correlation_id,
                    payload={
                        "from": goal.status,
                        "to": "cancelled",
                        "reason": clean_reason or None,
                    },
                    now=now,
                )
                goal = self._require_goal_tx(conn, goal.goal_id)
            conn.execute(
                "UPDATE workflow_wait SET status='cancelled' "
                "WHERE goal_id=? AND status='pending'",
                (goal.goal_id,),
            )
            conn.execute(
                "UPDATE workflow_attention SET status='dismissed',version=version+1,"
                "updated_at=?,resolved_at=? WHERE goal_id=? AND status='open'",
                (now, now, goal.goal_id),
            )
            conn.execute(
                "UPDATE workflow_effect SET status='cancelled',error=?,updated_at=? "
                "WHERE goal_id=? AND status IN ('planned','dispatched')",
                (clean_reason, now, goal.goal_id),
            )
            for step in steps:
                conn.execute(
                    "UPDATE workflow_step SET status='cancelled',version=version+1,"
                    "error=?,available_at=0,lease_owner='',lease_expires_at=NULL,"
                    "updated_at=?,completed_at=? WHERE step_id=?",
                    (clean_reason, now, now, step.step_id),
                )
                if step.attempt_count and step.status in {"leased", "running", "waiting"}:
                    conn.execute(
                        "UPDATE workflow_step_attempt SET status='cancelled',"
                        "updated_at=?,completed_at=? WHERE step_id=? AND attempt=?",
                        (now, now, step.step_id, step.attempt_count),
                    )
                self._advance_tx(
                    conn, goal, event_type="goal.step_status", actor=actor,
                    correlation_id=correlation_id, step_id=step.step_id,
                    payload={
                        "step_id": step.step_id,
                        "from": step.status,
                        "to": "cancelled",
                        "reason": clean_reason or None,
                        "available_at": 0.0,
                    },
                    now=now,
                )
                goal = self._require_goal_tx(conn, goal.goal_id)
            if intent_key is not None:
                intent = {'schema': 'variant1.goal-cancel-intent.v1', 'request_id': correlation_id,
                          'owner_chat_id': owner_chat_id, 'observed_version': expected_version,
                          'applied_to_version': applied_to_version, 'admitted_version': goal.version,
                          'reason': clean_reason, 'cleanup_token': pending_cleanup['token']}
                conn.execute('INSERT INTO workflow_goal_state(goal_id,state_key,value_json,version,updated_at) VALUES (?,?,?,1,?)',
                             (goal_id, intent_key, _json(intent), now))
            return goal

    def update_budget_usage(
        self, goal_id: str, delta: Mapping[str, Any], *, expected_version: int,
        actor: WorkActor | None = None, correlation_id: str = "",
    ) -> GoalRecord:
        with self.work._write() as conn:
            goal = self._require_goal_tx(conn, goal_id); self._check_version(goal, expected_version)
            usage = dict(goal.budget_usage)
            for key, raw in dict(delta).items():
                if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
                    raise GoalValidationError(f"budget delta {key!r} must be finite numeric")
                usage[str(key)] = float(usage.get(str(key)) or 0) + float(raw)
                if usage[str(key)] < 0:
                    raise GoalValidationError("budget usage cannot become negative")
            conn.execute(
                "UPDATE workflow_goal SET budget_usage_json=? WHERE goal_id=?",
                (_json(usage), goal.goal_id),
            )
            self._advance_tx(
                conn, goal, event_type="goal.budget_updated", actor=actor,
                correlation_id=correlation_id, payload={"delta": dict(delta), "usage": usage},
            )
            return self._require_goal_tx(conn, goal.goal_id)

    def state_get(self, goal_id: str, key: str | None = None) -> Any:
        self.require_goal(goal_id)
        with self.work._read() as conn:
            if key is not None:
                row = conn.execute(
                    "SELECT value_json FROM workflow_goal_state WHERE goal_id=? AND state_key=?",
                    (str(goal_id), str(key)),
                ).fetchone()
                return None if row is None else json.loads(str(row["value_json"]))
            rows = conn.execute(
                "SELECT state_key,value_json FROM workflow_goal_state WHERE goal_id=? ORDER BY state_key",
                (str(goal_id),),
            ).fetchall()
        return {str(row["state_key"]): json.loads(str(row["value_json"])) for row in rows}

    def state_set(
        self, goal_id: str, key: str, value: Any, *, expected_version: int,
        actor: WorkActor | None = None, correlation_id: str = "",
    ) -> GoalRecord:
        clean_key = _text(key, "state key", required=True, limit=240)
        now = time.time()
        with self.work._write() as conn:
            goal = self._require_goal_tx(conn, goal_id); self._check_version(goal, expected_version)
            conn.execute(
                "INSERT INTO workflow_goal_state(goal_id,state_key,value_json,version,updated_at) "
                "VALUES (?,?,?,1,?) ON CONFLICT(goal_id,state_key) DO UPDATE SET "
                "value_json=excluded.value_json,version=workflow_goal_state.version+1,updated_at=excluded.updated_at",
                (goal.goal_id, clean_key, _json(value), now),
            )
            self._advance_tx(
                conn, goal, event_type="goal.state_set", actor=actor,
                correlation_id=correlation_id, payload={"key": clean_key}, now=now,
            )
            return self._require_goal_tx(conn, goal.goal_id)

    def attach_artifact(
        self, goal_id: str, artifact_ref: str, *, expected_version: int,
        role: str = "evidence", step_id: str = "", metadata: Mapping[str, Any] | None = None,
        actor: WorkActor | None = None, correlation_id: str = "",
    ) -> GoalArtifactRecord:
        ref = _text(artifact_ref, "artifact_ref", required=True, limit=2000)
        clean_role = _text(role, "artifact role", required=True, limit=120)
        now = time.time()
        with self.work._write() as conn:
            goal = self._require_goal_tx(conn, goal_id); self._check_version(goal, expected_version)
            if step_id:
                self._require_step_tx(conn, goal.goal_id, step_id)
            existing = conn.execute(
                "SELECT * FROM workflow_goal_artifact WHERE goal_id=? AND step_id=? AND artifact_ref=? AND role=?",
                (goal.goal_id, str(step_id), ref, clean_role),
            ).fetchone()
            if existing is None:
                link_id = _id("goalart")
                conn.execute(
                    "INSERT INTO workflow_goal_artifact(link_id,goal_id,step_id,artifact_ref,role,metadata_json,created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (link_id, goal.goal_id, str(step_id), ref, clean_role, _json(dict(metadata or {})), now),
                )
            else:
                link_id = str(existing["link_id"])
            self._advance_tx(
                conn, goal, event_type="goal.artifact_attached", actor=actor,
                correlation_id=correlation_id, step_id=str(step_id),
                payload={"link_id": link_id, "artifact_ref": ref, "role": clean_role}, now=now,
            )
            row = conn.execute(
                "SELECT * FROM workflow_goal_artifact WHERE link_id=?", (link_id,)
            ).fetchone()
        return GoalArtifactRecord(
            link_id=str(row["link_id"]), goal_id=str(row["goal_id"]), step_id=str(row["step_id"] or ""),
            artifact_ref=str(row["artifact_ref"]), role=str(row["role"]),
            metadata=_load(row["metadata_json"], dict, "artifact metadata"), created_at=float(row["created_at"]),
        )

    def list_artifacts(self, goal_id: str) -> list[GoalArtifactRecord]:
        self.require_goal(goal_id)
        with self.work._read() as conn:
            rows = conn.execute(
                "SELECT * FROM workflow_goal_artifact WHERE goal_id=? ORDER BY created_at,link_id",
                (str(goal_id),),
            ).fetchall()
        return [GoalArtifactRecord(
            link_id=str(row["link_id"]), goal_id=str(row["goal_id"]), step_id=str(row["step_id"] or ""),
            artifact_ref=str(row["artifact_ref"]), role=str(row["role"]),
            metadata=_load(row["metadata_json"], dict, "artifact metadata"), created_at=float(row["created_at"]),
        ) for row in rows]

    def set_step_status(
        self, goal_id: str, step_id: str, status: str, *, expected_version: int,
        expected_step_version: int | None = None, reason: str = "", available_at: float = 0.0,
        actor: WorkActor | None = None, correlation_id: str = "",
    ) -> tuple[GoalRecord, StepRecord]:
        target = _text(status, "step status", required=True, limit=40)
        if target not in STEP_STATES:
            raise GoalValidationError(f"unsupported step status: {target}")
        now = time.time()
        with self.work._write() as conn:
            goal = self._require_goal_tx(conn, goal_id); self._check_version(goal, expected_version)
            step = self._require_step_tx(conn, goal.goal_id, step_id)
            if expected_step_version is not None and step.version != int(expected_step_version):
                raise GoalConflict("step version changed")
            if step.status == target:
                return goal, step
            if step.terminal and target not in {"pending", "retry_scheduled"}:
                raise GoalTransitionError(f"terminal step cannot transition {step.status} -> {target}")
            completed = now if target in {"succeeded", "failed", "blocked", "skipped", "cancelled"} else None
            conn.execute(
                "UPDATE workflow_step SET status=?,version=version+1,error=?,available_at=?,"
                "lease_owner='',lease_expires_at=NULL,updated_at=?,completed_at=? WHERE step_id=?",
                (target, _text(reason, "step reason", limit=4000), float(available_at), now, completed, step.step_id),
            )
            if step.attempt_count and step.status in {"leased", "running", "waiting"}:
                conn.execute(
                    "UPDATE workflow_step_attempt SET status=?,updated_at=?,completed_at=? "
                    "WHERE step_id=? AND attempt=?",
                    (target, now, completed, step.step_id, step.attempt_count),
                )
            self._advance_tx(
                conn, goal, event_type="goal.step_status", actor=actor,
                correlation_id=correlation_id, step_id=step.step_id,
                payload={"step_id": step.step_id, "from": step.status, "to": target,
                         "reason": reason or None, "available_at": float(available_at)}, now=now,
            )
            return self._require_goal_tx(conn, goal.goal_id), self._require_step_tx(conn, goal.goal_id, step.step_id)

    def lease_step(
        self, goal_id: str, step_id: str, *, expected_version: int,
        lease_owner: str, lease_ttl_s: float = 60.0,
        actor: WorkActor | None = None, correlation_id: str = "",
    ) -> tuple[GoalRecord, StepRecord, StepAttemptRecord]:
        owner = _text(lease_owner, "lease_owner", required=True, limit=512)
        now = time.time(); expires = now + max(5.0, float(lease_ttl_s))
        with self.work._write() as conn:
            goal = self._require_goal_tx(conn, goal_id); self._check_version(goal, expected_version)
            step = self._require_step_tx(conn, goal.goal_id, step_id)
            if step.status != "ready" or step.available_at > now:
                raise GoalTransitionError("only an available ready step can be leased")
            attempt = step.attempt_count + 1
            if attempt > step.max_attempts:
                raise GoalTransitionError("step attempt budget is exhausted")
            epoch = step.lease_epoch + 1
            attempt_id = _id("attempt")
            thread_id = f"goal:{goal.goal_id}:step:{step.step_id}:attempt:{attempt}"
            conn.execute(
                "UPDATE workflow_step SET status='leased',version=version+1,attempt_count=?,"
                "lease_owner=?,lease_epoch=?,lease_expires_at=?,native_thread_id=?,updated_at=? WHERE step_id=?",
                (attempt, owner, epoch, expires, thread_id, now, step.step_id),
            )
            conn.execute(
                "INSERT INTO workflow_step_attempt(attempt_id,goal_id,step_id,attempt,status,"
                "snapshot_thread_id,started_at,updated_at) VALUES (?,?,?,?, 'leased',?,?,?)",
                (attempt_id, goal.goal_id, step.step_id, attempt, thread_id, now, now),
            )
            self._advance_tx(
                conn, goal, event_type="goal.step_leased", actor=actor,
                correlation_id=correlation_id, step_id=step.step_id, attempt=attempt,
                payload={"step_id": step.step_id, "attempt": attempt, "lease_epoch": epoch,
                         "lease_expires_at": expires, "snapshot_thread_id": thread_id}, now=now,
            )
            updated_goal = self._require_goal_tx(conn, goal.goal_id)
            updated_step = self._require_step_tx(conn, goal.goal_id, step.step_id)
            attempted = self._attempt(conn.execute(
                "SELECT * FROM workflow_step_attempt WHERE attempt_id=?", (attempt_id,)
            ).fetchone())
            assert attempted is not None
            return updated_goal, updated_step, attempted

    def heartbeat_step(self, goal_id: str, step_id: str, *, lease_owner: str,
                       lease_epoch: int, lease_ttl_s: float = 60.0) -> StepRecord:
        now = time.time()
        with self.work._write() as conn:
            goal = self._require_goal_tx(conn, goal_id)
            step = self._require_step_tx(conn, goal.goal_id, step_id)
            if (goal.status in {"cancelled", "archived", "failed", "succeeded"}
                    or step.status not in {"leased", "running"}
                    or step.lease_owner != str(lease_owner)
                    or step.lease_epoch != int(lease_epoch)
                    or step.lease_expires_at <= now):
                raise GoalConflict("step lease was lost before renewal")
            conn.execute(
                "UPDATE workflow_step SET lease_expires_at=?,updated_at=? WHERE step_id=?",
                (now + max(5.0, float(lease_ttl_s)), now, step.step_id),
            )
            return self._require_step_tx(conn, goal.goal_id, step_id)

    def start_step(
        self, goal_id: str, step_id: str, *, expected_version: int,
        lease_owner: str, lease_epoch: int,
        actor: WorkActor | None = None, correlation_id: str = "",
    ) -> tuple[GoalRecord, StepRecord, StepAttemptRecord]:
        now = time.time()
        with self.work._write() as conn:
            goal = self._require_goal_tx(conn, goal_id); self._check_version(goal, expected_version)
            step = self._require_step_tx(conn, goal.goal_id, step_id)
            if step.status != "leased" or step.lease_owner != str(lease_owner) or step.lease_epoch != int(lease_epoch):
                raise GoalConflict("step lease was lost before start")
            conn.execute(
                "UPDATE workflow_step SET status='running',version=version+1,updated_at=? WHERE step_id=?",
                (now, step.step_id),
            )
            conn.execute(
                "UPDATE workflow_step_attempt SET status='running',updated_at=? WHERE step_id=? AND attempt=?",
                (now, step.step_id, step.attempt_count),
            )
            self._advance_tx(
                conn, goal, event_type="goal.step_started", actor=actor,
                correlation_id=correlation_id, step_id=step.step_id, attempt=step.attempt_count,
                payload={"step_id": step.step_id, "attempt": step.attempt_count}, now=now,
            )
            updated_goal = self._require_goal_tx(conn, goal.goal_id)
            updated_step = self._require_step_tx(conn, goal.goal_id, step.step_id)
            attempt = self._attempt(conn.execute(
                "SELECT * FROM workflow_step_attempt WHERE step_id=? AND attempt=?",
                (step.step_id, step.attempt_count),
            ).fetchone())
            assert attempt is not None
            return updated_goal, updated_step, attempt

    def finish_step(
        self, goal_id: str, step_id: str, *, expected_version: int,
        status: str, result_ref: str = "", error_ref: str = "", error: str = "",
        expected_attempt: int | None = None, lease_owner: str = "",
        lease_epoch: int | None = None,
        actor: WorkActor | None = None, correlation_id: str = "",
    ) -> tuple[GoalRecord, StepRecord]:
        target = str(status)
        if target not in {"succeeded", "failed", "blocked", "cancelled", "skipped"}:
            raise GoalValidationError("finish status must be terminal")
        now = time.time()
        with self.work._write() as conn:
            goal = self._require_goal_tx(conn, goal_id); self._check_version(goal, expected_version)
            step = self._require_step_tx(conn, goal.goal_id, step_id)
            if step.status not in {"leased", "running", "waiting", "ready"}:
                raise GoalTransitionError(f"cannot finish step from {step.status}")
            if (
                expected_attempt is not None
                and step.attempt_count != int(expected_attempt)
            ):
                raise GoalConflict("step attempt was superseded before completion")
            if lease_epoch is not None and (
                step.lease_epoch != int(lease_epoch)
                or (lease_owner and step.lease_owner != str(lease_owner))
            ):
                raise GoalConflict("step lease epoch was superseded before completion")
            conn.execute(
                "UPDATE workflow_step SET status=?,version=version+1,result_ref=?,error_ref=?,error=?,"
                "lease_owner='',lease_expires_at=NULL,updated_at=?,completed_at=? WHERE step_id=?",
                (target, str(result_ref), str(error_ref), _text(error, "step error", limit=4000), now, now, step.step_id),
            )
            completed_attempt = (
                int(expected_attempt)
                if expected_attempt is not None
                else step.attempt_count
            )
            if completed_attempt:
                conn.execute(
                    "UPDATE workflow_step_attempt SET status=?,result_ref=?,error_ref=?,updated_at=?,completed_at=? "
                    "WHERE step_id=? AND attempt=?",
                    (target, str(result_ref), str(error_ref), now, now, step.step_id, completed_attempt),
                )
            self._advance_tx(
                conn, goal, event_type=f"goal.step_{target}", actor=actor,
                correlation_id=correlation_id, step_id=step.step_id, attempt=completed_attempt,
                payload={"step_id": step.step_id, "attempt": completed_attempt,
                         "status": target, "result_ref": result_ref or None,
                         "error_ref": error_ref or None, "error": error or None}, now=now,
            )
            return self._require_goal_tx(conn, goal.goal_id), self._require_step_tx(conn, goal.goal_id, step.step_id)

    def update_attempt_projection(
        self, goal_id: str, step_id: str, *, expected_version: int,
        attempt: int, snapshot_cursor: Mapping[str, Any], machine_revision: str,
        native_run_id: str, last_receipt_id: str = "",
        actor: WorkActor | None = None, correlation_id: str = "",
    ) -> StepAttemptRecord:
        cursor = dict(snapshot_cursor)
        if not str(cursor.get("thread_id") or "") or int(cursor.get("sequence") or 0) <= 0 or not str(cursor.get("snapshot_id") or ""):
            raise GoalValidationError("attempt projection requires an exact SnapshotCursor")
        now = time.time()
        with self.work._write() as conn:
            goal = self._require_goal_tx(conn, goal_id); self._check_version(goal, expected_version)
            step = self._require_step_tx(conn, goal.goal_id, step_id)
            row = conn.execute(
                "SELECT * FROM workflow_step_attempt WHERE step_id=? AND attempt=?",
                (step.step_id, int(attempt)),
            ).fetchone()
            if row is None:
                raise GoalNotFound("unknown step attempt")
            if str(row["snapshot_thread_id"] or "") != str(cursor["thread_id"]):
                raise GoalConflict("snapshot cursor thread does not match the attempt")
            conn.execute(
                "UPDATE workflow_step_attempt SET snapshot_cursor_json=?,machine_revision=?,native_run_id=?,"
                "last_receipt_id=?,updated_at=? WHERE step_id=? AND attempt=?",
                (_json(cursor), str(machine_revision), str(native_run_id), str(last_receipt_id), now,
                 step.step_id, int(attempt)),
            )
            conn.execute(
                "UPDATE workflow_step SET snapshot_cursor_json=?,native_thread_id=?,version=version+1,updated_at=? "
                "WHERE step_id=?",
                (_json(cursor), str(cursor["thread_id"]), now, step.step_id),
            )
            self._advance_tx(
                conn, goal, event_type="goal.step_snapshot_projected", actor=actor,
                correlation_id=correlation_id, step_id=step.step_id, attempt=int(attempt),
                payload={"step_id": step.step_id, "attempt": int(attempt), "snapshot_cursor": cursor,
                         "machine_revision": str(machine_revision), "native_run_id": str(native_run_id)}, now=now,
            )
            updated = self._attempt(conn.execute(
                "SELECT * FROM workflow_step_attempt WHERE step_id=? AND attempt=?",
                (step.step_id, int(attempt)),
            ).fetchone())
            assert updated is not None
            return updated

    def list_attempts(self, goal_id: str, step_id: str = "") -> list[StepAttemptRecord]:
        self.require_goal(goal_id)
        sql = "SELECT * FROM workflow_step_attempt WHERE goal_id=?"
        params: list[Any] = [str(goal_id)]
        if step_id:
            sql += " AND step_id=?"; params.append(str(step_id))
        sql += " ORDER BY step_id,attempt"
        with self.work._read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [item for row in rows if (item := self._attempt(row)) is not None]

    @staticmethod
    def _wait(row: sqlite3.Row | None) -> WaitRecord | None:
        if row is None:
            return None
        return WaitRecord(
            wait_id=str(row["wait_id"]), goal_id=str(row["goal_id"]),
            step_id=str(row["step_id"]), source=str(row["source"]),
            matcher=_load(row["matcher_json"], dict, "wait matcher"), status=str(row["status"]),
            wake_at=float(row["wake_at"] or 0), result=_load(row["result_json"], dict, "wait result"),
            event_id=str(row["event_id"] or ""), created_at=float(row["created_at"]),
            satisfied_at=float(row["satisfied_at"] or 0),
        )

    @staticmethod
    def _attention(row: sqlite3.Row | None) -> AttentionRecord | None:
        if row is None:
            return None
        return AttentionRecord(
            attention_id=str(row["attention_id"]), goal_id=str(row["goal_id"]),
            step_id=str(row["step_id"] or ""), kind=str(row["kind"]), status=str(row["status"]),
            prompt=str(row["prompt"]), schema=_load(row["schema_json"], dict, "attention schema"),
            response=json.loads(str(row["response_json"])), version=int(row["version"]),
            created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
            resolved_at=float(row["resolved_at"] or 0),
        )

    def create_wait(
        self, goal_id: str, step_id: str, *, expected_version: int,
        source: str, matcher: Mapping[str, Any] | None = None, wake_at: float = 0.0,
        actor: WorkActor | None = None, correlation_id: str = "",
    ) -> WaitRecord:
        clean_source = _text(source, "wait source", required=True, limit=120)
        now = time.time(); wait_id = _id("wait")
        with self.work._write() as conn:
            goal = self._require_goal_tx(conn, goal_id); self._check_version(goal, expected_version)
            step = self._require_step_tx(conn, goal.goal_id, step_id)
            if step.terminal:
                raise GoalTransitionError("terminal step cannot wait")
            conn.execute(
                "UPDATE workflow_wait SET status='cancelled' WHERE step_id=? AND status='pending'",
                (step.step_id,),
            )
            conn.execute(
                "INSERT INTO workflow_wait(wait_id,goal_id,step_id,source,matcher_json,status,wake_at,created_at) "
                "VALUES (?,?,?,?,?,'pending',?,?)",
                (wait_id, goal.goal_id, step.step_id, clean_source, _json(dict(matcher or {})),
                 float(wake_at) or None, now),
            )
            conn.execute(
                "UPDATE workflow_step SET status='waiting',version=version+1,wait_spec_json=?,"
                "lease_owner='',lease_expires_at=NULL,updated_at=? WHERE step_id=?",
                (_json({"source": clean_source, "matcher": dict(matcher or {}),
                        "wake_at": float(wake_at) or None, "wait_id": wait_id}), now, step.step_id),
            )
            if step.attempt_count:
                conn.execute(
                    "UPDATE workflow_step_attempt SET status='waiting',updated_at=? WHERE step_id=? AND attempt=?",
                    (now, step.step_id, step.attempt_count),
                )
            self._advance_tx(
                conn, goal, event_type="goal.step_waiting", actor=actor,
                correlation_id=correlation_id, step_id=step.step_id, attempt=step.attempt_count,
                payload={"step_id": step.step_id, "wait_id": wait_id,
                         "source": clean_source, "wake_at": float(wake_at) or None,
                         "resources_released": True}, now=now,
            )
            result = self._wait(conn.execute(
                "SELECT * FROM workflow_wait WHERE wait_id=?", (wait_id,)
            ).fetchone())
            assert result is not None
            return result

    def satisfy_wait(
        self, wait_id: str, *, expected_version: int, result: Mapping[str, Any] | None = None,
        event_id: str = "", actor: WorkActor | None = None, correlation_id: str = "",
    ) -> tuple[GoalRecord, WaitRecord, StepRecord]:
        now = time.time()
        with self.work._write() as conn:
            row = conn.execute("SELECT * FROM workflow_wait WHERE wait_id=?", (str(wait_id),)).fetchone()
            wait = self._wait(row)
            if wait is None:
                raise GoalNotFound(f"unknown wait: {wait_id}")
            goal = self._require_goal_tx(conn, wait.goal_id); self._check_version(goal, expected_version)
            if wait.status == "satisfied":
                return goal, wait, self._require_step_tx(conn, goal.goal_id, wait.step_id)
            if wait.status != "pending":
                raise GoalTransitionError(f"cannot satisfy {wait.status} wait")
            step = self._require_step_tx(conn, goal.goal_id, wait.step_id)
            result_value = dict(result or {})
            outcome = str(result_value.get("status") or "succeeded")
            if outcome not in {"succeeded", "failed", "blocked", "cancelled"}:
                raise GoalValidationError("wait result status is invalid")
            result_ref = str(
                result_value.get("artifact_ref")
                or result_value.get("result_ref")
                or ""
            )
            error_ref = str(result_value.get("error_ref") or "")
            error = _text(result_value.get("error"), "wait result error", limit=4000)
            conn.execute(
                "UPDATE workflow_wait SET status='satisfied',result_json=?,event_id=?,satisfied_at=? WHERE wait_id=?",
                (_json(result_value), str(event_id), now, wait.wait_id),
            )
            conn.execute(
                "UPDATE workflow_step SET status=?,version=version+1,result_ref=?,error_ref=?,error=?,"
                "lease_owner='',lease_expires_at=NULL,updated_at=?,completed_at=? WHERE step_id=?",
                (outcome, result_ref, error_ref, error, now, now, step.step_id),
            )
            if step.attempt_count:
                conn.execute(
                    "UPDATE workflow_step_attempt SET status=?,result_ref=?,error_ref=?,updated_at=?,completed_at=? "
                    "WHERE step_id=? AND attempt=?",
                    (outcome, result_ref, error_ref, now, now, step.step_id, step.attempt_count),
                )
            self._advance_tx(
                conn, goal, event_type="goal.wait_satisfied", actor=actor,
                correlation_id=correlation_id, step_id=step.step_id, attempt=step.attempt_count,
                payload={"wait_id": wait.wait_id, "step_id": step.step_id,
                         "source": wait.source, "event_id": event_id or None,
                         "outcome": outcome}, now=now,
            )
            updated_wait = self._wait(conn.execute(
                "SELECT * FROM workflow_wait WHERE wait_id=?", (wait.wait_id,)
            ).fetchone())
            assert updated_wait is not None
            return self._require_goal_tx(conn, goal.goal_id), updated_wait, self._require_step_tx(conn, goal.goal_id, step.step_id)

    def list_waits(self, goal_id: str, *, status: str = "") -> list[WaitRecord]:
        self.require_goal(goal_id)
        sql = "SELECT * FROM workflow_wait WHERE goal_id=?"; params: list[Any] = [str(goal_id)]
        if status:
            if status not in WAIT_STATES:
                raise GoalValidationError("invalid wait status")
            sql += " AND status=?"; params.append(status)
        sql += " ORDER BY created_at,wait_id"
        with self.work._read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [item for row in rows if (item := self._wait(row)) is not None]

    def request_input(
        self, goal_id: str, step_id: str, *, expected_version: int,
        prompt: str, schema: Mapping[str, Any] | None = None,
        actor: WorkActor | None = None, correlation_id: str = "",
    ) -> AttentionRecord:
        clean_prompt = _text(prompt, "input prompt", required=True, limit=8000)
        now = time.time(); attention_id = _id("interaction"); wait_id = _id("wait")
        with self.work._write() as conn:
            goal = self._require_goal_tx(conn, goal_id); self._check_version(goal, expected_version)
            step = self._require_step_tx(conn, goal.goal_id, step_id)
            if step.terminal:
                raise GoalTransitionError("terminal step cannot request input")
            self.interactions.create_goal_input_tx(
                conn,
                interaction_id=attention_id,
                goal_id=goal.goal_id,
                step_id=step.step_id,
                prompt=clean_prompt,
                schema=dict(schema or {}),
                scope=self._scope(
                    goal, step_id=step.step_id, attempt=step.attempt_count,
                ),
                actor=_actor(actor),
                correlation_id=correlation_id,
                now=now,
            )
            conn.execute(
                "INSERT INTO workflow_attention(attention_id,goal_id,step_id,kind,status,prompt,"
                "schema_json,response_json,version,created_at,updated_at) "
                "VALUES (?,?,?,'input','open',?,?,'null',1,?,?)",
                (attention_id, goal.goal_id, step.step_id, clean_prompt, _json(dict(schema or {})), now, now),
            )
            conn.execute(
                "INSERT INTO workflow_wait(wait_id,goal_id,step_id,source,matcher_json,status,created_at) "
                "VALUES (?,?,?,'user_input',?,'pending',?)",
                (wait_id, goal.goal_id, step.step_id, _json({"attention_id": attention_id}), now),
            )
            conn.execute(
                "UPDATE workflow_step SET status='waiting',version=version+1,wait_spec_json=?,"
                "lease_owner='',lease_expires_at=NULL,updated_at=? WHERE step_id=?",
                (_json({"source": "user_input", "attention_id": attention_id, "wait_id": wait_id}),
                 now, step.step_id),
            )
            if step.attempt_count:
                conn.execute(
                    "UPDATE workflow_step_attempt SET status='waiting',updated_at=? "
                    "WHERE step_id=? AND attempt=?",
                    (now, step.step_id, step.attempt_count),
                )
            self._advance_tx(
                conn, goal, event_type="goal.input_requested", actor=actor,
                correlation_id=correlation_id, step_id=step.step_id, attempt=step.attempt_count,
                payload={"attention_id": attention_id, "wait_id": wait_id,
                         "step_id": step.step_id, "resources_released": True}, now=now,
            )
            result = self._attention(conn.execute(
                "SELECT * FROM workflow_attention WHERE attention_id=?", (attention_id,)
            ).fetchone())
            assert result is not None
            return result

    def respond_input(
        self, attention_id: str, response: Any, *, expected_version: int,
        expected_attention_version: int = 1,
        actor: WorkActor | None = None, correlation_id: str = "",
    ) -> tuple[GoalRecord, AttentionRecord]:
        now = time.time()
        with self.work._write() as conn:
            attention = self._attention(conn.execute(
                "SELECT * FROM workflow_attention WHERE attention_id=?", (str(attention_id),)
            ).fetchone())
            if attention is None:
                raise GoalNotFound(f"unknown attention: {attention_id}")
            goal = self._require_goal_tx(conn, attention.goal_id); self._check_version(goal, expected_version)
            if attention.status != "open" or attention.version != int(expected_attention_version):
                raise GoalConflict("attention is no longer open at the expected version")
            step = self._require_step_tx(conn, goal.goal_id, attention.step_id)
            try:
                self.interactions.resolve_goal_input_tx(
                    conn,
                    attention.attention_id,
                    response,
                    expected_version=expected_attention_version,
                    actor=_actor(actor),
                    correlation_id=correlation_id,
                    now=now,
                )
            except WorkConflict as exc:
                raise GoalConflict(str(exc)) from exc
            conn.execute(
                "UPDATE workflow_attention SET status='answered',response_json='null',version=version+1,"
                "updated_at=?,resolved_at=? WHERE attention_id=?",
                (now, now, attention.attention_id),
            )
            conn.execute(
                "UPDATE workflow_wait SET status='satisfied',result_json=?,satisfied_at=? "
                "WHERE step_id=? AND source='user_input' AND status='pending'",
                (_json({"response": response, "attention_id": attention.attention_id}), now, step.step_id),
            )
            conn.execute(
                "INSERT INTO workflow_goal_state(goal_id,state_key,value_json,version,updated_at) "
                "VALUES (?,?,?,1,?) ON CONFLICT(goal_id,state_key) DO UPDATE SET value_json=excluded.value_json,"
                "version=workflow_goal_state.version+1,updated_at=excluded.updated_at",
                (goal.goal_id, f"input:{step.step_id}", _json(response), now),
            )
            conn.execute(
                "UPDATE workflow_step SET status='succeeded',version=version+1,lease_owner='',"
                "lease_expires_at=NULL,updated_at=?,completed_at=? WHERE step_id=?",
                (now, now, step.step_id),
            )
            if step.attempt_count:
                conn.execute(
                    "UPDATE workflow_step_attempt SET status='succeeded',updated_at=?,completed_at=? "
                    "WHERE step_id=? AND attempt=?",
                    (now, now, step.step_id, step.attempt_count),
                )
            self._advance_tx(
                conn, goal, event_type="goal.input_answered", actor=actor,
                correlation_id=correlation_id, step_id=step.step_id,
                payload={"attention_id": attention.attention_id, "step_id": step.step_id}, now=now,
            )
            updated = self._attention(conn.execute(
                "SELECT * FROM workflow_attention WHERE attention_id=?", (attention.attention_id,)
            ).fetchone())
            assert updated is not None
            return self._require_goal_tx(conn, goal.goal_id), updated

    def dismiss_input(
        self,
        attention_id: str,
        *,
        expected_attention_version: int = 1,
        actor: WorkActor | None = None,
        correlation_id: str = "",
    ) -> tuple[GoalRecord, AttentionRecord]:
        """Atomically close skipped input and block its required step."""

        now = time.time()
        with self.work._write() as conn:
            attention = self._attention(conn.execute(
                "SELECT * FROM workflow_attention WHERE attention_id=?",
                (str(attention_id),),
            ).fetchone())
            if attention is None:
                raise GoalNotFound(f"unknown attention: {attention_id}")
            goal = self._require_goal_tx(conn, attention.goal_id)
            if (
                attention.status != "open"
                or attention.version != int(expected_attention_version)
            ):
                raise GoalConflict(
                    "attention is no longer open at the expected version"
                )
            step = self._require_step_tx(conn, goal.goal_id, attention.step_id)
            try:
                self.interactions.dismiss_goal_input_tx(
                    conn,
                    attention.attention_id,
                    expected_version=expected_attention_version,
                    actor=_actor(actor),
                    correlation_id=correlation_id,
                    now=now,
                )
            except WorkConflict as exc:
                raise GoalConflict(str(exc)) from exc
            conn.execute(
                "UPDATE workflow_attention SET status='dismissed',version=version+1,"
                "updated_at=?,resolved_at=? WHERE attention_id=?",
                (now, now, attention.attention_id),
            )
            conn.execute(
                "UPDATE workflow_wait SET status='cancelled',result_json=?,satisfied_at=? "
                "WHERE step_id=? AND source='user_input' AND status='pending'",
                (
                    _json({
                        "attention_id": attention.attention_id,
                        "reason": "user_skipped",
                    }),
                    now,
                    step.step_id,
                ),
            )
            conn.execute(
                "UPDATE workflow_step SET status='blocked',version=version+1,error=?,"
                "lease_owner='',lease_expires_at=NULL,updated_at=?,completed_at=? "
                "WHERE step_id=?",
                ("Required user input was skipped", now, now, step.step_id),
            )
            if step.attempt_count:
                conn.execute(
                    "UPDATE workflow_step_attempt SET status='blocked',updated_at=?,"
                    "completed_at=? WHERE step_id=? AND attempt=?",
                    (now, now, step.step_id, step.attempt_count),
                )
            self._advance_tx(
                conn,
                goal,
                event_type="goal.input_dismissed",
                actor=actor,
                correlation_id=correlation_id,
                step_id=step.step_id,
                payload={
                    "attention_id": attention.attention_id,
                    "step_id": step.step_id,
                    "reason": "user_skipped",
                },
                now=now,
            )
            updated = self._attention(conn.execute(
                "SELECT * FROM workflow_attention WHERE attention_id=?",
                (attention.attention_id,),
            ).fetchone())
            assert updated is not None
            return self._require_goal_tx(conn, goal.goal_id), updated

    def list_attention(self, goal_id: str, *, status: str = "") -> list[AttentionRecord]:
        self.require_goal(goal_id)
        sql = "SELECT * FROM workflow_attention WHERE goal_id=?"; params: list[Any] = [str(goal_id)]
        if status:
            if status not in ATTENTION_STATES:
                raise GoalValidationError("invalid attention status")
            sql += " AND status=?"; params.append(status)
        sql += " ORDER BY created_at,attention_id"
        with self.work._read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [item for row in rows if (item := self._attention(row)) is not None]

    @staticmethod
    def _effect(row: sqlite3.Row | None) -> EffectRecord | None:
        if row is None:
            return None
        return EffectRecord(
            effect_id=str(row["effect_id"]), goal_id=str(row["goal_id"]), step_id=str(row["step_id"]),
            attempt=int(row["attempt"]), sequence=int(row["sequence"]), kind=str(row["kind"]),
            status=str(row["status"]), idempotency_key=str(row["idempotency_key"]),
            request=_load(row["request_json"], dict, "effect request"),
            response=_load(row["response_json"], dict, "effect response"),
            receipt_id=str(row["receipt_id"] or ""), error=str(row["error"] or ""),
            created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
        )

    def record_effect(
        self, goal_id: str, step_id: str, *, expected_version: int,
        kind: str, idempotency_key: str, request: Mapping[str, Any] | None = None,
        status: str = "planned", actor: WorkActor | None = None, correlation_id: str = "",
    ) -> EffectRecord:
        if status not in EFFECT_STATES:
            raise GoalValidationError("invalid effect status")
        idem = _text(idempotency_key, "effect idempotency_key", required=True, limit=512)
        now = time.time()
        with self.work._write() as conn:
            goal = self._require_goal_tx(conn, goal_id)
            step = self._require_step_tx(conn, goal.goal_id, step_id)
            prior = self._effect(conn.execute(
                "SELECT * FROM workflow_effect WHERE goal_id=? AND idempotency_key=?",
                (goal.goal_id, idem),
            ).fetchone())
            if prior is not None:
                if (
                    prior.step_id != step.step_id
                    or prior.kind != str(kind)
                    or dict(prior.request) != dict(request or {})
                ):
                    raise GoalConflict(
                        "effect idempotency key was reused for a different request"
                    )
                return prior
            self._check_version(goal, expected_version)
            sequence = int(conn.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM workflow_effect WHERE goal_id=?",
                (goal.goal_id,),
            ).fetchone()[0])
            effect_id = _id("effect")
            conn.execute(
                "INSERT INTO workflow_effect(effect_id,goal_id,step_id,attempt,sequence,kind,status,"
                "idempotency_key,request_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (effect_id, goal.goal_id, step.step_id, step.attempt_count, sequence,
                 _text(kind, "effect kind", required=True, limit=120), status, idem,
                 _json(dict(request or {})), now, now),
            )
            self._advance_tx(
                conn, goal, event_type="goal.effect_recorded", actor=actor,
                correlation_id=correlation_id, step_id=step.step_id, attempt=step.attempt_count,
                payload={"effect_id": effect_id, "sequence": sequence, "status": status,
                         "kind": kind, "idempotency_key": idem}, now=now,
            )
            result = self._effect(conn.execute(
                "SELECT * FROM workflow_effect WHERE effect_id=?", (effect_id,)
            ).fetchone())
            assert result is not None
            return result

    def update_effect(
        self, effect_id: str, *, expected_version: int, status: str,
        response: Mapping[str, Any] | None = None, receipt_id: str = "", error: str = "",
        actor: WorkActor | None = None, correlation_id: str = "",
    ) -> EffectRecord:
        if status not in EFFECT_STATES:
            raise GoalValidationError("invalid effect status")
        now = time.time()
        with self.work._write() as conn:
            effect = self._effect(conn.execute(
                "SELECT * FROM workflow_effect WHERE effect_id=?", (str(effect_id),)
            ).fetchone())
            if effect is None:
                raise GoalNotFound(f"unknown effect: {effect_id}")
            goal = self._require_goal_tx(conn, effect.goal_id)
            if status == effect.status:
                if (
                    dict(effect.response) == dict(response or {})
                    and effect.receipt_id == str(receipt_id)
                    and effect.error == str(error)
                ):
                    return effect
                raise GoalConflict(
                    "effect transition was replayed with different terminal data"
                )
            self._check_version(goal, expected_version)
            allowed = {
                "planned": {"dispatched", "cancelled", "failed"},
                "dispatched": {"succeeded", "failed", "unknown_effect", "cancelled"},
            }
            if status != effect.status and status not in allowed.get(effect.status, set()):
                raise GoalTransitionError(f"cannot transition effect {effect.status} -> {status}")
            conn.execute(
                "UPDATE workflow_effect SET status=?,response_json=?,receipt_id=?,error=?,updated_at=? WHERE effect_id=?",
                (status, _json(dict(response or {})), str(receipt_id),
                 _text(error, "effect error", limit=4000), now, effect.effect_id),
            )
            self._advance_tx(
                conn, goal, event_type="goal.effect_updated", actor=actor,
                correlation_id=correlation_id, step_id=effect.step_id, attempt=effect.attempt,
                payload={"effect_id": effect.effect_id, "from": effect.status, "to": status,
                         "receipt_id": receipt_id or None}, now=now,
            )
            result = self._effect(conn.execute(
                "SELECT * FROM workflow_effect WHERE effect_id=?", (effect.effect_id,)
            ).fetchone())
            assert result is not None
            return result

    def list_effects(self, goal_id: str) -> list[EffectRecord]:
        self.require_goal(goal_id)
        with self.work._read() as conn:
            rows = conn.execute(
                "SELECT * FROM workflow_effect WHERE goal_id=? ORDER BY sequence",
                (str(goal_id),),
            ).fetchall()
        return [item for row in rows if (item := self._effect(row)) is not None]

    def retry_step(
        self, goal_id: str, step_id: str, *, expected_version: int,
        delay_s: float = 0.0, actor: WorkActor | None = None, correlation_id: str = "",
        continuation: bool = False,
    ) -> tuple[GoalRecord, StepRecord]:
        now = time.time()
        with self.work._write() as conn:
            goal = self._require_goal_tx(conn, goal_id); self._check_version(goal, expected_version)
            step = self._require_step_tx(conn, goal.goal_id, step_id)
            if goal.terminal:
                raise GoalTransitionError('A terminal goal cannot schedule another child turn')
            if continuation and goal.completion_policy.get('entrypoint')!='composer_goal':
                raise GoalValidationError('Continuation admission requires an explicit composer goal')
            if step.status not in {"failed", "blocked"}:
                raise GoalTransitionError("only a failed or blocked step can be retried")
            if step.attempt_count >= step.max_attempts and not continuation:
                raise GoalTransitionError("step has exhausted max_attempts")
            available = now + max(0.0, float(delay_s))
            conn.execute(
                "UPDATE workflow_step SET status='retry_scheduled',version=version+1,error='',error_ref='',"
                "result_ref='',available_at=?,completed_at=NULL,updated_at=?,max_attempts=? WHERE step_id=?",
                (available, now, max(step.max_attempts,step.attempt_count+1) if continuation else step.max_attempts,step.step_id),
            )
            self._advance_tx(
                conn, goal, event_type="goal.step_retry_scheduled", actor=actor,
                correlation_id=correlation_id, step_id=step.step_id,
                payload={"step_id": step.step_id, "available_at": available,
                         "next_attempt": step.attempt_count + 1}, now=now,
            )
            return self._require_goal_tx(conn, goal.goal_id), self._require_step_tx(conn, goal.goal_id, step.step_id)

__all__ = ["GoalRepository", "MAX_PLAN_STEPS"]
