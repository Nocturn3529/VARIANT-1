"""Durable chat goals: at most one current goal per chat, in the work database."""
from __future__ import annotations

import sqlite3
import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from core_invariants import (
    sqlite_read_connection,
    sqlite_unit_of_work,
    sqlite_wal_connection,
    sqlite_writer_lock,
)
from work_fabric.repository import CHAT_GOAL_MIGRATION_VERSION

STATUSES = ("active", "paused", "blocked", "budget_limited", "usage_limited", "complete")
# Statuses the user can resume from. A budget-limited goal stays limited.
RESUMABLE = frozenset({"paused", "blocked", "usage_limited"})
MAX_OBJECTIVE = 32_000


class GoalError(RuntimeError):
    @property
    def code(self) -> str:
        return type(self).__name__


class GoalConflict(GoalError):
    pass


class GoalVersionConflict(GoalError):
    pass


class GoalNotFound(GoalError):
    pass


class GoalValidationError(GoalError):
    pass


class ChatUnavailable(GoalError):
    pass


@dataclass(frozen=True)
class ChatGoal:
    goal_id: str
    chat_id: str
    request_id: str
    objective: str
    status: str
    token_budget: int | None
    tokens_used: int
    time_used_s: float
    active_since: float | None
    status_reason: str
    submission_pending: bool
    created_at: float
    updated_at: float
    completed_at: float | None
    completed_run_id: str
    first_run_id: str
    version: int
    objective_revision: int

    def time_used(self, now: float | None = None) -> float:
        extra = 0.0
        if self.status == "active" and self.active_since:
            extra = max(0.0, (time.time() if now is None else now) - self.active_since)
        return round(self.time_used_s + extra, 3)

    def remaining_tokens(self) -> int | None:
        if self.token_budget is None:
            return None
        return max(0, self.token_budget - self.tokens_used)

    def to_dict(self, *, continuing: bool = False) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id, "session_id": self.chat_id, "request_id": self.request_id,
            "objective": self.objective, "status": self.status,
            "token_budget": self.token_budget, "tokens_used": self.tokens_used,
            "time_used_s": self.time_used(), "status_reason": self.status_reason,
            "continuing": bool(continuing), "submission_pending": self.submission_pending,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "completed_at": self.completed_at, "completed_run_id": self.completed_run_id or None,
            "version": self.version,
        }


def clean_objective(value: Any) -> str:
    objective = str(value or "").strip()
    if not objective:
        raise GoalValidationError("A goal needs an objective")
    if len(objective) > MAX_OBJECTIVE:
        raise GoalValidationError(f"A goal objective is limited to {MAX_OBJECTIVE} characters")
    return objective


def clean_budget(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise GoalValidationError("token_budget must be a positive integer")
    try:
        budget = int(value)
    except (TypeError, ValueError):
        raise GoalValidationError("token_budget must be a positive integer") from None
    if budget <= 0 or budget != float(value):
        raise GoalValidationError("token_budget must be a positive integer")
    return budget


class ChatGoalStore:
    def __init__(self, path: str, *, clock=time.time) -> None:
        self.path = path
        self._clock = clock
        self._lock = sqlite_writer_lock(path)
        # executescript commits on its own, so the schema runs outside a unit of work.
        with self._lock, sqlite_read_connection(lambda: sqlite_wal_connection(path)) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS chat_goal(
                  goal_id TEXT PRIMARY KEY,
                  chat_id TEXT NOT NULL,
                  request_id TEXT NOT NULL DEFAULT '',
                  submitted_objective TEXT NOT NULL DEFAULT '',
                  first_run_id TEXT NOT NULL DEFAULT '',
                  objective TEXT NOT NULL,
                  objective_revision INTEGER NOT NULL DEFAULT 1,
                  status TEXT NOT NULL,
                  token_budget INTEGER,
                  tokens_used INTEGER NOT NULL DEFAULT 0,
                  time_used_s REAL NOT NULL DEFAULT 0,
                  active_since REAL,
                  status_reason TEXT NOT NULL DEFAULT '',
                  submission_pending INTEGER NOT NULL DEFAULT 0,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL,
                  completed_at REAL,
                  completed_run_id TEXT NOT NULL DEFAULT '',
                  cleared_at REAL,
                  version INTEGER NOT NULL DEFAULT 1
                );
                CREATE UNIQUE INDEX IF NOT EXISTS chat_goal_current
                  ON chat_goal(chat_id) WHERE cleared_at IS NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS chat_goal_request
                  ON chat_goal(chat_id, request_id) WHERE request_id != '';
                CREATE INDEX IF NOT EXISTS chat_goal_active ON chat_goal(status) WHERE cleared_at IS NULL;
                CREATE INDEX IF NOT EXISTS chat_goal_completed_run ON chat_goal(chat_id,completed_run_id)
                  WHERE status='complete';
                CREATE TABLE IF NOT EXISTS work_schema_migration(
                  version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, description TEXT NOT NULL DEFAULT '');
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(chat_goal)")}
            if "submitted_objective" not in columns:
                conn.execute("ALTER TABLE chat_goal ADD COLUMN submitted_objective TEXT NOT NULL DEFAULT ''")
            if "first_run_id" not in columns:
                conn.execute("ALTER TABLE chat_goal ADD COLUMN first_run_id TEXT NOT NULL DEFAULT ''")
            if "objective_revision" not in columns:
                conn.execute("ALTER TABLE chat_goal ADD COLUMN objective_revision INTEGER NOT NULL DEFAULT 1")
            # Also backfill if a prior additive upgrade was interrupted.
            conn.execute("UPDATE chat_goal SET submitted_objective=objective WHERE submitted_objective=''")

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with sqlite_unit_of_work(lambda: sqlite_wal_connection(self.path), self._lock,
                                 fault_name="chat_goal.before_commit") as conn:
            yield conn

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with sqlite_read_connection(lambda: sqlite_wal_connection(self.path)) as conn:
            yield conn

    @staticmethod
    def _row(row: sqlite3.Row | None) -> ChatGoal | None:
        if row is None:
            return None
        return ChatGoal(
            goal_id=row["goal_id"], chat_id=row["chat_id"], request_id=row["request_id"],
            objective=row["objective"], status=row["status"], token_budget=row["token_budget"],
            tokens_used=int(row["tokens_used"]), time_used_s=float(row["time_used_s"]),
            active_since=row["active_since"], status_reason=row["status_reason"],
            submission_pending=bool(row["submission_pending"]), created_at=row["created_at"],
            updated_at=row["updated_at"], completed_at=row["completed_at"],
            completed_run_id=row["completed_run_id"], version=int(row["version"]),
            first_run_id=row["first_run_id"],
            objective_revision=int(row["objective_revision"]),
        )

    def current(self, chat_id: str) -> ChatGoal | None:
        with self._read() as conn:
            return self._row(conn.execute(
                "SELECT * FROM chat_goal WHERE chat_id=? AND cleared_at IS NULL", (str(chat_id),),
            ).fetchone())

    def get(self, goal_id: str) -> ChatGoal | None:
        with self._read() as conn:
            return self._row(conn.execute(
                "SELECT * FROM chat_goal WHERE goal_id=?", (str(goal_id),)).fetchone())

    def chargeable(self, goal_id: str) -> bool:
        with self._read() as conn:
            return conn.execute(
                "SELECT 1 FROM chat_goal WHERE goal_id=? AND cleared_at IS NULL "
                "AND status IN ('active','budget_limited')", (goal_id,)).fetchone() is not None

    def active_chat_ids(self) -> list[str]:
        with self._read() as conn:
            return [row["chat_id"] for row in conn.execute(
                "SELECT chat_id FROM chat_goal WHERE status='active' AND cleared_at IS NULL "
                "ORDER BY updated_at")]

    def completed_by(self, chat_id: str, run_id: str) -> ChatGoal | None:
        if not run_id:
            return None
        with self._read() as conn:
            return self._row(conn.execute(
                "SELECT * FROM chat_goal WHERE chat_id=? AND completed_run_id=? AND status='complete' "
                "ORDER BY completed_at DESC LIMIT 1", (chat_id, run_id)).fetchone())

    def create(self, chat_id: str, objective: str, *, request_id: str = "",
               token_budget: int | None = None, replace: bool = False, expected_goal_id: str | None = None,
               creator_run_id: str | None = None) -> tuple[ChatGoal, bool]:
        """Create the chat's goal. Returns ``(goal, created)``; a repeated request is idempotent."""
        objective = clean_objective(objective)
        token_budget = clean_budget(token_budget)
        request_id = str(request_id or "")[:512]
        now = self._clock()
        with self._write() as conn:
            if request_id:
                prior = conn.execute(
                    "SELECT * FROM chat_goal WHERE chat_id=? AND request_id=?",
                    (chat_id, request_id)).fetchone()
                if prior is not None:
                    if prior["submitted_objective"] != objective or prior["token_budget"] != token_budget:
                        raise GoalConflict("This request_id already set a different goal")
                    return self._row(prior), False
            current = self._row(conn.execute(
                "SELECT * FROM chat_goal WHERE chat_id=? AND cleared_at IS NULL", (chat_id,)).fetchone())
            if replace and not expected_goal_id:
                raise GoalConflict("Confirm the current goal before replacing it")
            if expected_goal_id is not None and (current.goal_id if current else "") != expected_goal_id:
                if current is None and expected_goal_id:
                    raise GoalNotFound("The confirmed goal is no longer current")
                raise GoalConflict("The current goal changed; confirm the replacement again")
            if current is not None:
                if current.status != "complete" and not replace:
                    raise GoalConflict("This chat already has an unfinished goal")
                self._clear_tx(conn, current, now)
            goal_id = "goal_" + uuid.uuid4().hex
            conn.execute(
                "INSERT INTO chat_goal(goal_id,chat_id,request_id,objective,status,token_budget,"
                "active_since,submission_pending,created_at,updated_at,submitted_objective,first_run_id) "
                "VALUES(?,?,?,?, 'active', ?, ?, ?, ?, ?, ?, ?)",
                (goal_id, chat_id, request_id, objective, token_budget, now, int(creator_run_id is None),
                 now, now, objective, creator_run_id or ""))
            return self._row(conn.execute(
                "SELECT * FROM chat_goal WHERE goal_id=?", (goal_id,)).fetchone()), True

    def _require(self, conn, goal_id: str, expected_version: int | None) -> ChatGoal:
        goal = self._row(conn.execute(
            "SELECT * FROM chat_goal WHERE goal_id=? AND cleared_at IS NULL", (goal_id,)).fetchone())
        if goal is None:
            raise GoalNotFound("The goal is no longer current")
        if expected_version is not None and int(expected_version) != goal.version:
            raise GoalVersionConflict("The goal changed; refresh before trying again")
        return goal

    @staticmethod
    def _clear_tx(conn, goal: ChatGoal, now: float) -> None:
        conn.execute("UPDATE chat_goal SET cleared_at=?, updated_at=?, version=version+1, "
                     "time_used_s=time_used_s+?, active_since=NULL WHERE goal_id=?",
                     (now, now, _active_seconds(goal, now), goal.goal_id))

    def transition(self, goal_id: str, status: str, *, reason: str = "",
                   from_statuses: frozenset[str] | set[str] | None = None,
                   expected_version: int | None = None, run_id: str = "",
                   expected_objective_revision: int | None = None) -> ChatGoal | None:
        """Move a current goal to ``status``. Returns ``None`` when ``from_statuses`` doesn't match."""
        if status not in STATUSES:
            raise GoalValidationError(f"Unknown goal status: {status}")
        now = self._clock()
        with self._write() as conn:
            goal = self._require(conn, goal_id, expected_version)
            if expected_objective_revision is not None and goal.objective_revision != expected_objective_revision:
                raise GoalConflict("The goal objective changed since this model request")
            if from_statuses is not None and goal.status not in from_statuses:
                return None
            if status == "active" and goal.remaining_tokens() == 0:
                status, reason = "budget_limited", "Token budget reached"
            if goal.status == status:
                return goal
            self._set_status_tx(conn, goal, status, reason, now, run_id)
            return self._row(conn.execute("SELECT * FROM chat_goal WHERE goal_id=?", (goal_id,)).fetchone())

    @staticmethod
    def _set_status_tx(conn, goal: ChatGoal, status: str, reason: str, now: float, run_id: str = "") -> None:
        conn.execute(
            "UPDATE chat_goal SET status=?, status_reason=?, time_used_s=time_used_s+?, "
            "active_since=?, completed_at=?, completed_run_id=?, updated_at=?, version=version+1 WHERE goal_id=?",
            (status, str(reason or "")[:500], _active_seconds(goal, now), now if status == "active" else None,
             now if status == "complete" else None, str(run_id or "")[:160] if status == "complete" else "",
             now, goal.goal_id))

    def edit(self, goal_id: str, objective: str, *, expected_version: int | None = None) -> ChatGoal:
        objective = clean_objective(objective)
        now = self._clock()
        with self._write() as conn:
            goal = self._require(conn, goal_id, expected_version)
            if goal.status == "complete":
                raise GoalConflict("A completed goal can't be edited; set a new goal instead")
            conn.execute("UPDATE chat_goal SET objective=?, objective_revision=?, updated_at=?, version=version+1 "
                         "WHERE goal_id=?", (objective, goal.objective_revision + int(objective != goal.objective),
                                             now, goal_id))
            return self._row(conn.execute("SELECT * FROM chat_goal WHERE goal_id=?", (goal_id,)).fetchone())

    def clear(self, goal_id: str, *, expected_version: int | None = None) -> None:
        now = self._clock()
        with self._write() as conn:
            self._clear_tx(conn, self._require(conn, goal_id, expected_version), now)

    def admit_turn(self, chat_id: str, goal_id: str, run_id: str, *, submission: bool) -> bool:
        """The ordinary admitted task enters here before any model or tool effects."""
        with self._write() as conn:
            goal = self._row(conn.execute(
                "SELECT * FROM chat_goal WHERE goal_id=? AND chat_id=? AND cleared_at IS NULL AND status='active'",
                (goal_id, chat_id)).fetchone())
            if goal is None:
                return False
            if goal.remaining_tokens() == 0:
                self._set_status_tx(conn, goal, "budget_limited", "Token budget reached", self._clock())
                return False
            if submission != goal.submission_pending:
                return False
            if submission:
                conn.execute(
                    "UPDATE chat_goal SET submission_pending=0,first_run_id=?,version=version+1,updated_at=? "
                    "WHERE goal_id=?", (run_id, self._clock(), goal_id))
            return True

    def apply_token_delta(self, goal_id: str, delta: int) -> tuple[ChatGoal | None, bool]:
        """Charge the goal that owned the request, even if it has since stopped."""
        if not delta:
            return None, False
        now = self._clock()
        with self._write() as conn:
            row = conn.execute("SELECT * FROM chat_goal WHERE goal_id=?", (goal_id,)).fetchone()
            if row is None:
                return None, False
            goal = self._row(row)
            used = max(0, goal.tokens_used + delta)
            reached = (row["cleared_at"] is None and goal.status == "active" and goal.token_budget is not None
                       and used >= goal.token_budget)
            if reached:
                conn.execute(
                    "UPDATE chat_goal SET tokens_used=?, status='budget_limited', "
                    "status_reason='Token budget reached', time_used_s=time_used_s+?, "
                    "active_since=NULL, updated_at=?, version=version+1 WHERE goal_id=?",
                    (used, _active_seconds(goal, now), now, goal_id))
            elif (row["cleared_at"] is None and goal.status == "budget_limited" and
                  goal.token_budget is not None and used < goal.token_budget):
                conn.execute(
                    "UPDATE chat_goal SET tokens_used=?,status='paused',status_reason=?,updated_at=?,"
                    "version=version+1 WHERE goal_id=?",
                    (used, "Provider usage was corrected below the budget; resume when ready", now, goal_id))
            else:
                conn.execute("UPDATE chat_goal SET tokens_used=?, updated_at=?, version=version+1 "
                             "WHERE goal_id=?", (used, now, goal_id))
            return self._row(conn.execute("SELECT * FROM chat_goal WHERE goal_id=?", (goal_id,)).fetchone()), reached

    def delete_chat(self, chat_id: str) -> None:
        with self._write() as conn:
            conn.execute("DELETE FROM chat_goal WHERE chat_id=?", (str(chat_id),))

    def migrate_legacy_goals(self) -> int:
        """Publish the chosen rows, their safe initial statuses, and the marker in one transaction."""
        with self._write() as conn:
            if conn.execute("SELECT 1 FROM work_schema_migration WHERE version=?",
                            (CHAT_GOAL_MIGRATION_VERSION,)).fetchone():
                return 0
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_goal'").fetchone():
                return 0
            # A completed goal can precede an unfinished one in the same chat.
            # Prefer unfinished work, then the latest eligible item for that chat.
            rows = conn.execute(
                "SELECT * FROM workflow_goal WHERE owner_chat_id<>'' "
                "AND status NOT IN ('failed','cancelled','archived') AND json_valid(completion_policy_json) "
                "AND json_extract(completion_policy_json,'$.entrypoint')='composer_goal' "
                "ORDER BY owner_chat_id,(status='succeeded'),updated_at DESC,goal_id DESC")
            carried = 0
            for row in rows:
                if conn.execute("SELECT 1 FROM chat_goal WHERE chat_id=? LIMIT 1",
                                (row["owner_chat_id"],)).fetchone():
                    continue
                policy = json.loads(row["completion_policy_json"])
                complete = row["status"] == "succeeded"
                now = self._clock()
                conn.execute(
                    "INSERT INTO chat_goal(goal_id,chat_id,request_id,objective,submitted_objective,status,"
                    "status_reason,submission_pending,created_at,updated_at,completed_at) VALUES(?,?,?,?,?,?,?,0,?,?,?)",
                    (row["goal_id"], row["owner_chat_id"], str(policy.get("submission_request_id") or "legacy:"+row["goal_id"]),
                     row["objective"], row["objective"], "complete" if complete else "paused",
                     "" if complete else "Carried over from the previous goal engine; resume to continue",
                     row["created_at"] if "created_at" in row.keys() else row["updated_at"], now,
                     (row["completed_at"] if "completed_at" in row.keys() else row["updated_at"]) if complete else None))
                carried += 1
            conn.execute("INSERT INTO work_schema_migration VALUES(?,?,?)",
                         (CHAT_GOAL_MIGRATION_VERSION, self._clock(), "Carry legacy composer goals into chat goals"))
            return carried


def _active_seconds(goal: ChatGoal, now: float) -> float:
    if goal.status != "active" or not goal.active_since:
        return 0.0
    return max(0.0, now - goal.active_since)
