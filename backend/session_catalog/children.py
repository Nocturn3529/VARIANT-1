"""Durable child handles executed as Work-owned persistent-Python workers."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import inspect
import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Mapping
from contextlib import asynccontextmanager, closing
from typing import Any

from capability_broker import current_capability_invocation
from core_invariants import sqlite_session_connection
from tools import Tool, ToolError
from work_fabric.handles import remote_handle_envelope
from work_fabric.jobs import JobExecutionContext, JobResult
from work_fabric.scope import WorkScope

from .profiles import ACTION_SURFACE


CHILD_EXECUTION_JOB = "child.execute.v1"
_CHILD_PROVENANCE_SUFFIX = "\n(Self-reported by the child; verify critical results.)"


def reported_child_text(result_text: str) -> str:
    """Recover the child's exact self-report from the display projection."""

    value = str(result_text or "")
    if value.startswith("[child "):
        marker = value.find("] ")
        if marker >= 0:
            value = value[marker + 2:]
        if value.endswith(_CHILD_PROVENANCE_SUFFIX):
            value = value[:-len(_CHILD_PROVENANCE_SUFFIX)]
    return value


CHILD_OBJECT_METHODS: tuple[dict[str, Any], ...] = (
    {
        "name": "spawn",
        "description": "Spawn a durably admitted child and return immediately.",
        "effect_class": "external_side_effect",
        "params": {
            "task": {"type": "string", "required": True},
            "name": {"type": "string", "required": False},
            "context": {"type": "string", "required": False},
        },
    },
    {
        "name": "list",
        "description": "List child handles for this chat.",
        "effect_class": "read",
        "params": {
            "limit": {"type": "integer", "required": False, "minimum": 1, "maximum": 100},
        },
    },
    {
        "name": "tree",
        "description": "Inspect the bounded durable descendant tree and usage.",
        "effect_class": "read",
        "params": {
            "limit": {"type": "integer", "required": False, "minimum": 1, "maximum": 100},
        },
    },
)

CHILD_HANDLE_METHODS: tuple[dict[str, Any], ...] = (
    {
        "name": "refresh",
        "description": "Reconnect this child to its current durable generation.",
        "params": [],
        "returns": "child",
    },
    {
        "name": "wait",
        "description": "Wait for this child to reach a terminal state.",
        "params": [{
            "name": "timeout_s", "type": "number", "required": False,
            "default": 30.0,
        }],
        "returns": "child",
    },
    {
        "name": "inspect",
        "description": "Inspect this child, its exact report, and messages.",
        "params": [],
        "returns": "dict",
    },
    {
        "name": "send",
        "control": True,
        "description": "Append a durable parent-to-child message.",
        "params": [{"name": "text", "type": "string", "required": True}],
        "returns": "child",
    },
    {
        "name": "cancel",
        "control": True,
        "description": "Cancel this active child.",
        "params": [],
        "returns": "child",
    },
    {
        "name": "restart",
        "description": "Restart this interrupted or terminal child. Returns a new generation-bound handle: assign child = child.restart() before further calls.",
        "params": [],
        "returns": "child",
    },
)


def _child_seed_params() -> dict[str, dict[str, Any]]:
    params: dict[str, dict[str, Any]] = {
        "operation": {
            "type": "string",
            "required": True,
            "enum": [str(row["name"]) for row in CHILD_OBJECT_METHODS],
        }
    }
    for method in CHILD_OBJECT_METHODS:
        for name, raw_spec in dict(method.get("params") or {}).items():
            spec = dict(raw_spec)
            spec["required"] = False
            current = params.get(str(name))
            if current is not None and current != spec:
                raise RuntimeError(f"conflicting children parameter schema: {name}")
            params[str(name)] = spec
    return params


def _child_method_arguments(
    operation: str, args: dict[str, Any]
) -> dict[str, Any]:
    method = next(
        (row for row in CHILD_OBJECT_METHODS if row["name"] == operation),
        None,
    )
    if method is None:
        raise ToolError(f"unknown children operation: {operation!r}")
    specs = dict(method.get("params") or {})
    payload = {key: value for key, value in args.items() if key != "operation"}
    unknown = sorted(set(payload) - set(specs))
    if unknown:
        raise ToolError(
            f"children.{operation}: unknown argument(s): {', '.join(unknown)}"
        )
    for name, spec in specs.items():
        if bool(spec.get("required")) and (
            name not in payload or payload[name] in (None, "", [], {})
        ):
            raise ToolError(f"children.{operation} needs '{name}'")
    return payload


from .outcomes import ChildOutcomes
from .inspection import ChildInspection


class ChildSessionManager(ChildOutcomes,ChildInspection):
    def __init__(
        self,
        database_path: str,
        host: Any,
        artifact_store: Any,
        *,
        work: Any = None,
    ):
        self.path = os.path.abspath(database_path)
        self.host = host
        self.artifact_store = artifact_store
        self._lock = threading.RLock()
        self._capacity_condition = asyncio.Condition()
        self._active_executions = 0
        self._change_lock = threading.RLock()
        self._change_publisher = None
        self._change_loop: asyncio.AbstractEventLoop | None = None
        self._pending_change_events: dict[tuple[str, str], dict[str, Any]] = {}
        self.work = work
        self._work_registered = False
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS astb_child_handle (
                    child_id TEXT PRIMARY KEY,
                    child_chat_id TEXT NOT NULL DEFAULT '',
                    parent_chat_id TEXT NOT NULL,
                    depth INTEGER NOT NULL DEFAULT 1,
                    name TEXT NOT NULL,
                    task_text TEXT NOT NULL,
                    context_text TEXT NOT NULL,
                    action_surface TEXT NOT NULL,
                    model_route_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    result_text TEXT NOT NULL DEFAULT '',
                    artifact_ref TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    usage_json TEXT NOT NULL DEFAULT '{}',
                    run_generation INTEGER NOT NULL DEFAULT 1,
                    usage_rollup_state TEXT NOT NULL DEFAULT 'complete',
                    usage_rollup_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL,
                    deletion_state TEXT NOT NULL DEFAULT '',
                    deletion_error TEXT NOT NULL DEFAULT '',
                    work_job_id TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS astb_child_parent_idx
                ON astb_child_handle(parent_chat_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS astb_child_message (
                    message_id TEXT PRIMARY KEY,
                    child_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    text TEXT NOT NULL,
                    consumed_at REAL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(child_id) REFERENCES astb_child_handle(child_id)
                );
                """
            )
            columns = {
                str(row[1]) for row in conn.execute(
                    "PRAGMA table_info(astb_child_handle)"
                ).fetchall()
            }
            if "usage_json" not in columns:
                conn.execute(
                    "ALTER TABLE astb_child_handle ADD COLUMN usage_json TEXT NOT NULL DEFAULT '{}'"
                )
            for name in ('outcome_json','outcome_run_id','restart_request_id','restart_message'):
                if name not in columns:
                    default = '{}' if name=='outcome_json' else ''
                    conn.execute(f"ALTER TABLE astb_child_handle ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")
            if "model_route_json" not in columns:
                conn.execute("ALTER TABLE astb_child_handle ADD COLUMN model_route_json TEXT NOT NULL DEFAULT '{}'")
            if "run_generation" not in columns:
                conn.execute(
                    "ALTER TABLE astb_child_handle ADD COLUMN "
                    "run_generation INTEGER NOT NULL DEFAULT 1"
                )
            if "usage_rollup_state" not in columns:
                conn.execute(
                    "ALTER TABLE astb_child_handle ADD COLUMN "
                    "usage_rollup_state TEXT NOT NULL DEFAULT 'complete'"
                )
            if "usage_rollup_error" not in columns:
                conn.execute(
                    "ALTER TABLE astb_child_handle ADD COLUMN "
                    "usage_rollup_error TEXT NOT NULL DEFAULT ''"
                )
            if "child_chat_id" not in columns:
                conn.execute(
                    "ALTER TABLE astb_child_handle ADD COLUMN "
                    "child_chat_id TEXT NOT NULL DEFAULT ''"
                )
            if "depth" not in columns:
                conn.execute(
                    "ALTER TABLE astb_child_handle ADD COLUMN depth INTEGER NOT NULL DEFAULT 1"
                )
            if "deletion_state" not in columns:
                conn.execute(
                    "ALTER TABLE astb_child_handle ADD COLUMN "
                    "deletion_state TEXT NOT NULL DEFAULT ''"
                )
            if "deletion_error" not in columns:
                conn.execute(
                    "ALTER TABLE astb_child_handle ADD COLUMN "
                    "deletion_error TEXT NOT NULL DEFAULT ''"
                )
            if "work_job_id" not in columns:
                conn.execute(
                    "ALTER TABLE astb_child_handle ADD COLUMN "
                    "work_job_id TEXT NOT NULL DEFAULT ''"
                )
            message_columns = {
                str(row[1]) for row in conn.execute(
                    "PRAGMA table_info(astb_child_message)"
                ).fetchall()
            }
            if "consumed_at" not in message_columns:
                conn.execute(
                    "ALTER TABLE astb_child_message ADD COLUMN consumed_at REAL"
                )
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS astb_child_clock(id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL);
                INSERT OR IGNORE INTO astb_child_clock VALUES (1,0);
                CREATE TRIGGER IF NOT EXISTS astb_child_insert_clock AFTER INSERT ON astb_child_handle
                BEGIN UPDATE astb_child_clock SET revision=revision+1 WHERE id=1; END;
                CREATE TRIGGER IF NOT EXISTS astb_child_update_clock AFTER UPDATE ON astb_child_handle
                BEGIN UPDATE astb_child_clock SET revision=revision+1 WHERE id=1; END;
                CREATE TRIGGER IF NOT EXISTS astb_child_delete_clock AFTER DELETE ON astb_child_handle
                BEGIN UPDATE astb_child_clock SET revision=revision+1 WHERE id=1; END;
                CREATE TRIGGER IF NOT EXISTS astb_child_message_insert_clock AFTER INSERT ON astb_child_message
                BEGIN UPDATE astb_child_clock SET revision=revision+1 WHERE id=1; END;
                CREATE TRIGGER IF NOT EXISTS astb_child_message_update_clock AFTER UPDATE ON astb_child_message
                BEGIN UPDATE astb_child_clock SET revision=revision+1 WHERE id=1; END;
                CREATE TRIGGER IF NOT EXISTS astb_child_message_delete_clock AFTER DELETE ON astb_child_message
                BEGIN UPDATE astb_child_clock SET revision=revision+1 WHERE id=1; END;
            ''')
        self.recover_usage_rollups()
        if self.work is not None:
            self.bind_work(self.work)

    @property
    def runtimes(self):
        return self.host.require_runtime().session_runtimes

    def bind_work(self, work: Any) -> None:
        if self._work_registered and self.work is work:
            return
        if self._work_registered and self.work is not work:
            raise RuntimeError("child manager is already bound to another Work service")
        self.work = work
        self.work.register_job_handler(
            CHILD_EXECUTION_JOB,
            self._work_handler,
            max_concurrency=lambda: int(self.capacity()["max_active"]),
        )
        self._work_registered = True
        with self._lock, self._connect() as conn:
            pending = conn.execute(
                "SELECT child_id FROM astb_child_handle "
                "WHERE status IN ('queued','running','interrupted') "
                "ORDER BY created_at,child_id"
            ).fetchall()
        for row in pending:
            self._admit_job(str(row["child_id"]))

    async def reconcile_runtime_sagas(self) -> dict[str, int]:
        """Drain legacy unpublished child births before the Work scheduler starts."""
        cleaned = retained = 0
        for record in self.runtimes.repository.list_runtimes(include_deleted=True):
            if not str(record.creation_saga_state or "").startswith("child:"):
                continue
            # Recheck the handle and fence the orphan in one transaction. A
            # completed birth after the inventory read must never be reclaimed.
            with self._lock, closing(self._connect()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                current = self.runtimes.repository.get_runtime(record.chat_id, connection=conn)
                if current is None:
                    continue
                marker = str(current.creation_saga_state or "")
                if not marker.startswith("child:"):
                    continue
                parent = marker[len("child:"):]
                if not parent:
                    raise RuntimeError(f"child runtime has no parent owner: {record.chat_id}")
                owner = conn.execute(
                    "SELECT parent_chat_id FROM astb_child_handle WHERE child_chat_id=?",
                    (record.chat_id,),
                ).fetchone()
                if owner is not None:
                    if str(owner["parent_chat_id"]) != parent:
                        raise RuntimeError(f"child runtime parent ownership mismatch: {record.chat_id}")
                    retained += 1
                    continue
                if (current.lifecycle_state == "deleted"
                        and not current.deletion_saga_state.startswith("orphan_child_creation:")):
                    # Parent deletion removes handles after draining the runtime.
                    # That explicit tombstone is not an unpublished birth.
                    retained += 1
                    continue
                if current.lifecycle_state in {"active", "creating"}:
                    self.runtimes.repository.set_lifecycle(
                        record.chat_id, "deleting", deletion_saga_state="orphan_child_creation",
                        connection=conn,
                    )
            try:
                drained = await self.runtimes.delete_child_runtime(record.chat_id, parent_chat_id=parent)
                discarded = self.runtimes.repository.discard_unstarted_child_runtime(
                    record.chat_id, parent_chat_id=parent,
                )
            except Exception as exc:
                raise RuntimeError(
                    f"child creation cleanup is pending for {record.chat_id}: {exc}"
                ) from exc
            cleaned += bool(drained or discarded)
        return {"cleaned": cleaned, "retained": retained}

    def _require_work(self):
        if self.work is None or not self._work_registered:
            raise RuntimeError("children require the shared Work Fabric")
        return self.work

    def _connect(self) -> sqlite3.Connection:
        return sqlite_session_connection(self.path, autocommit=False)

    @staticmethod
    def _clock_revision(conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT revision FROM astb_child_clock WHERE id=1"
        ).fetchone()
        return int(row[0] if row is not None else 0)

    def bind_change_publisher(
        self,
        publisher,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Bind the websocket invalidation sink and its owning event loop.

        Composition installs the publisher before the server loop exists;
        lifespan binds the loop before Work begins admitting child jobs.  A
        sync capability invoked from another thread can then enqueue the same
        event safely without starting a second loop or blocking its commit.
        """

        selected_loop = loop
        if selected_loop is None:
            try:
                selected_loop = asyncio.get_running_loop()
            except RuntimeError:
                selected_loop = None
        with self._change_lock:
            self._change_publisher = publisher
            if selected_loop is not None:
                self._change_loop = selected_loop
            active_loop = self._change_loop
            pending = list(self._pending_change_events.values())
            if active_loop is not None:
                self._pending_change_events.clear()
        if active_loop is not None:
            for event in sorted(
                pending, key=lambda row: int(row.get("revision") or 0)
            ):
                self._schedule_change_event(event)

    async def _deliver_change_event(self, event: dict[str, Any]) -> None:
        with self._change_lock:
            publisher = self._change_publisher
        if publisher is None:
            return
        result = publisher(dict(event))
        if inspect.isawaitable(result):
            await result

    def _schedule_change_event(self, event: dict[str, Any]) -> None:
        """Schedule one committed invalidation from loop or worker threads."""

        key = (
            str(event.get("session_id") or ""),
            str(event.get("child_id") or ""),
        )
        with self._change_lock:
            publisher = self._change_publisher
            loop = self._change_loop
            if publisher is None:
                return
            if loop is None or loop.is_closed() or not loop.is_running():
                previous = self._pending_change_events.get(key)
                if previous is None or int(event["revision"]) >= int(
                    previous["revision"]
                ):
                    self._pending_change_events[key] = dict(event)
                return

        def submit() -> None:
            import background_tasks

            background_tasks.spawn(
                self._deliver_change_event(event),
                name=(
                    "children-changed:"
                    + str(event.get("session_id") or "")
                    + ":"
                    + str(event.get("revision") or 0)
                ),
            )

        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is loop:
            submit()
            return
        try:
            loop.call_soon_threadsafe(submit)
        except RuntimeError:
            with self._change_lock:
                previous = self._pending_change_events.get(key)
                if previous is None or int(event["revision"]) >= int(
                    previous["revision"]
                ):
                    self._pending_change_events[key] = dict(event)

    def _notify_committed_change(
        self,
        parent_chat_id: str,
        child_id: str,
        revision: int,
    ) -> None:
        """Publish only after the transaction owning ``revision`` committed."""

        parent = str(parent_chat_id or "").strip()
        child = str(child_id or "").strip()
        if not parent:
            return
        event: dict[str, Any] = {
            "type": "children:changed",
            "schema": "variant1.children-changed.v1",
            "session_id": parent,
            "revision": int(revision),
        }
        if child:
            event["child_id"] = child
        self._schedule_change_event(event)

    def capacity(self) -> dict[str, int | str]:
        router = getattr(self.host, "router", None)
        config = getattr(router, "cfg", None)
        section = (
            config.get("subagents")
            if isinstance(config, dict)
            and isinstance(config.get("subagents"), dict)
            else {}
        )
        mode = str(getattr(router, "mode", "cloud") or "cloud").lower()

        def bounded(name: str, default: int, ceiling: int) -> int:
            try:
                return max(1, min(ceiling, int(section.get(name, default))))
            except (TypeError, ValueError):
                return default

        return {
            "mode": mode,
            "max_depth": bounded("max_depth", 4, 8),
            "max_active": bounded(
                "max_active_local" if mode == "local" else "max_active_cloud",
                1 if mode == "local" else 8,
                16,
            ),
            "max_admitted": bounded("max_admitted", 16, 64),
        }

    @asynccontextmanager
    async def _execution_slot(self):
        """Admit one child against the current host-owned route capacity.

        Work jobs may be durably leased while waiting here, but the child row
        remains ``queued`` until it owns a slot. This keeps local inference at
        one active child without rejecting additional durable children, while
        allowing the configured cloud fan-out.
        """

        async with self._capacity_condition:
            while self._active_executions >= int(self.capacity()["max_active"]):
                await self._capacity_condition.wait()
            self._active_executions += 1
        try:
            yield
        finally:
            async with self._capacity_condition:
                self._active_executions = max(0, self._active_executions - 1)
                self._capacity_condition.notify_all()

    def _row(self, child_id: str) -> sqlite3.Row | None:
        with self._lock, self._connect() as conn:
            return conn.execute(
                "SELECT * FROM astb_child_handle WHERE child_id=?",
                (str(child_id),),
            ).fetchone()

    def _parent_model_route(self, parent_chat_id: str) -> dict:
        from model_runtime.context import normalize_model_route
        with self._lock, self._connect() as conn:
            parent = conn.execute(
                "SELECT model_route_json FROM astb_child_handle WHERE child_chat_id=?",
                (str(parent_chat_id),),
            ).fetchone()
        inherited = json.loads(parent[0]) if parent else {}
        router = self.host.router
        if not inherited:
            sessions = getattr(self.host.require_runtime(), "sessions", None)
            getter = getattr(sessions, "get_model_route", None)
            inherited = getter(parent_chat_id) if callable(getter) else None
        return normalize_model_route(router, inherited or router.bound_model_route())

    def _child_model_route(self, row) -> dict:
        route = json.loads(row["model_route_json"] or "{}")
        if route:
            return route
        # Pre-upgrade handles have no historical route evidence. Pin the
        # parent's current selection once, rather than follow global defaults
        # on every later restart.
        candidate = self._parent_model_route(row["parent_chat_id"])
        revision = None
        with self._lock, self._connect() as conn:
            changed = conn.execute(
                "UPDATE astb_child_handle SET model_route_json=? WHERE child_id=? AND model_route_json='{}'",
                (json.dumps(candidate, sort_keys=True), row["child_id"]),
            )
            if changed.rowcount == 1:
                revision = self._clock_revision(conn)
        if revision is not None:
            self._notify_committed_change(
                str(row["parent_chat_id"]), str(row["child_id"]), revision
            )
        return json.loads(self._row(row["child_id"])["model_route_json"])

    def _rollup_usage(self, child_id: str) -> bool:
        """Resume one terminal child's idempotent ancestor budget saga."""

        row = self._row(child_id)
        if row is None or str(row["usage_rollup_state"] or "") != "pending":
            return bool(row is not None)
        generation = max(1, int(row["run_generation"] or 1))
        try:
            usage = json.loads(str(row["usage_json"] or "{}"))
            if not isinstance(usage, dict):
                raise ValueError("child usage is not an object")
            run_id = f"child-rollup:{child_id}:{generation}"
            current = str(row["child_chat_id"] or "")
            visited: set[str] = set()
            while current and current not in visited:
                visited.add(current)
                self.runtimes.record_run_usage(
                    current, run_id, usage
                )
                with self._lock, self._connect() as conn:
                    owner = conn.execute(
                        "SELECT parent_chat_id FROM astb_child_handle "
                        "WHERE child_chat_id=?",
                        (current,),
                    ).fetchone()
                current = str(
                    owner["parent_chat_id"] if owner is not None else ""
                )
        except Exception as exc:
            revision = None
            with self._lock, self._connect() as conn:
                changed = conn.execute(
                    "UPDATE astb_child_handle SET usage_rollup_state='pending',"
                    "usage_rollup_error=?,updated_at=? WHERE child_id=? "
                    "AND run_generation=?",
                    (str(exc)[:2000], time.time(), str(child_id), generation),
                )
                if changed.rowcount == 1:
                    revision = self._clock_revision(conn)
            if revision is not None:
                self._notify_committed_change(
                    str(row["parent_chat_id"]), str(child_id), revision
                )
            return False
        revision = None
        with self._lock, self._connect() as conn:
            changed = conn.execute(
                "UPDATE astb_child_handle SET usage_rollup_state='complete',"
                "usage_rollup_error='',updated_at=? WHERE child_id=? "
                "AND run_generation=? AND usage_rollup_state='pending'",
                (time.time(), str(child_id), generation),
            )
            if changed.rowcount == 1:
                revision = self._clock_revision(conn)
        if revision is not None:
            self._notify_committed_change(
                str(row["parent_chat_id"]), str(child_id), revision
            )
        return True

    def recover_usage_rollups(self) -> dict[str, int]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT child_id FROM astb_child_handle "
                "WHERE usage_rollup_state='pending' "
                "ORDER BY completed_at,child_id"
            ).fetchall()
        completed = sum(
            1 for row in rows if self._rollup_usage(str(row["child_id"]))
        )
        return {"pending": len(rows), "completed": completed}

    def _finish_child(
        self,
        child_id: str,
        *,
        status: str,
        usage: dict[str, Any],
        completed: float,
        result_text: str = "",
        artifact_ref: str = "",
        error: str = "",
        parent_message: bool = False,
    ) -> bool:
        encoded_usage = json.dumps(
            usage, sort_keys=True, separators=(",", ":")
        )
        parent_chat_id = ""
        revision = None
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            owner = conn.execute(
                "SELECT parent_chat_id FROM astb_child_handle WHERE child_id=?",
                (str(child_id),),
            ).fetchone()
            parent_chat_id = str(
                owner["parent_chat_id"] if owner is not None else ""
            )
            changed = conn.execute(
                "UPDATE astb_child_handle SET status=?,result_text=?,"
                "artifact_ref=?,error=?,usage_json=?,usage_rollup_state='pending',"
                "usage_rollup_error='',completed_at=?,updated_at=? "
                "WHERE child_id=? AND status='running'",
                (
                    str(status), str(result_text), str(artifact_ref),
                    str(error)[:2000], encoded_usage, completed, completed,
                    str(child_id),
                ),
            )
            if changed.rowcount == 1 and parent_message:
                conn.execute(
                    "INSERT INTO astb_child_message(message_id,child_id,direction,"
                    "text,created_at) VALUES (?,?,'child_to_parent',?,?)",
                    (
                        "cmsg_" + uuid.uuid4().hex,
                        str(child_id),
                        str(result_text),
                        completed,
                    ),
                )
            if changed.rowcount == 1:
                revision = self._clock_revision(conn)
            conn.commit()
        if changed.rowcount != 1:
            return False
        self._notify_committed_change(
            parent_chat_id, str(child_id), int(revision or 0)
        )
        self._rollup_usage(child_id)
        return True

    def _admit_job(self, child_id: str) -> str:
        work = self._require_work()
        row = self._row(child_id)
        if row is None:
            raise LookupError("unknown child handle")
        generation = max(1, int(row["run_generation"] or 1))
        scope = WorkScope(
            chat_id=str(row["parent_chat_id"] or ""),
        )
        job = work.jobs.create(
            CHILD_EXECUTION_JOB,
            owner_kind="child",
            owner_id=str(child_id),
            scope=scope,
            input_manifest={
                "child_id": str(child_id),
                "generation": generation,
            },
            max_attempts=3,
            retry_policy={
                "on_lease_expiry": "retry",
                "base_delay_s": 0.25,
                "max_delay_s": 5.0,
            },
            idempotency_key=f"child:{child_id}:{generation}",
        )
        revision = None
        with self._lock, self._connect() as conn:
            changed = conn.execute(
                "UPDATE astb_child_handle SET work_job_id=?,updated_at=? "
                "WHERE child_id=? AND run_generation=?",
                (job.job_id, time.time(), str(child_id), generation),
            )
            if changed.rowcount == 1:
                revision = self._clock_revision(conn)
        if revision is not None:
            self._notify_committed_change(
                str(row["parent_chat_id"]), str(child_id), revision
            )
        return job.job_id

    async def _enqueue(self, child_id: str) -> str:
        work = self._require_work()
        job_id = self._admit_job(child_id)
        # Narrow embedded/test compositions can drive one scheduler turn
        # without installing a second scheduler loop.
        if not work.started:
            await work.scheduler.run_once()
        return job_id

    async def _work_handler(self, execution: JobExecutionContext) -> JobResult:
        manifest = dict(execution.job.input_manifest or {})
        child_id = str(manifest.get("child_id") or "")
        generation = int(manifest.get("generation") or 0)
        row = self._row(child_id)
        if row is None:
            raise LookupError(f"unknown child handle: {child_id}")
        if int(row["run_generation"] or 0) != generation:
            raise RuntimeError("stale child Work job generation")
        with self.host.router.bind_model_route(self._child_model_route(row)):
            async with self._execution_slot():
                await self._run(
                    child_id,
                    cancellation_requested=execution.cancellation_requested,
                )
        result = self._row(child_id)
        if result is None:
            raise LookupError(f"child disappeared: {child_id}")
        return JobResult(
            result_ref=str(result["artifact_ref"] or ""),
            progress={
                "phase": "complete",
                "child_id": child_id,
                "child_status": str(result["status"]),
                "generation": generation,
            },
        )

    def _public(self, row: sqlite3.Row, *, messages: bool = False) -> dict[str, Any]:
        result = {
            key: row[key] for key in row.keys()
            if key not in {"task_text", "context_text"}
        }
        if messages:
            with self._lock, self._connect() as conn:
                rows = conn.execute(
                    "SELECT message_id, direction, text, created_at, consumed_at "
                    "FROM astb_child_message WHERE child_id=? "
                    "ORDER BY created_at, message_id LIMIT 100",
                    (row["child_id"],),
                ).fetchall()
            result["messages"] = [dict(item) for item in rows]
        try:
            result["usage"] = json.loads(str(result.pop("usage_json", "{}") or "{}"))
        except ValueError:
            result["usage"] = {}
        result["reported_text"] = reported_child_text(
            str(result.get("result_text") or "")
        )
        result["model_route"] = json.loads(result.pop("model_route_json", "{}") or "{}")
        result['outcome'] = json.loads(result.pop('outcome_json','{}') or '{}') or {
            'schema':'variant1.child-outcome.v1','status':'unreported',
            'basis':None,'independently_verified':False,
        }
        result.pop('outcome_run_id',None)
        return result

    async def spawn(
        self,
        parent_chat_id: str,
        *,
        task: str,
        name: str = "",
        context: str = "",
        child_id: str = "",
        child_chat_id: str = "",
        fresh_catalog: bool = False,
    ) -> dict[str, Any]:
        clean_task = str(task or "").strip()
        if not clean_task:
            raise ValueError("children.spawn requires a task")
        parent_runtime = self.runtimes.ensure_runtime(str(parent_chat_id))
        model_route = self._parent_model_route(str(parent_chat_id))
        if parent_runtime.continuation_state == "paused_budget_exhausted":
            raise RuntimeError("parent continuation budget is exhausted")
        created_revision = None
        with self._lock, closing(self._connect()) as conn, conn:
            # Runtime identity, inherited budget and handle are one durable birth.
            # Production composes both repositories over this same ASTB database.
            conn.execute("BEGIN IMMEDIATE")
            selected_child_id = str(child_id or "").strip()
            selected_chat_id = str(child_chat_id or "").strip()
            if bool(selected_child_id) != bool(selected_chat_id):
                raise ValueError(
                    "deterministic child_id and child_chat_id must be supplied together"
                )
            if selected_child_id:
                existing = conn.execute(
                    "SELECT * FROM astb_child_handle WHERE child_id=?",
                    (selected_child_id,),
                ).fetchone()
                if existing is not None:
                    if (
                        str(existing["parent_chat_id"]) != str(parent_chat_id)
                        or str(existing["child_chat_id"]) != selected_chat_id
                        or str(existing["task_text"]) != clean_task[:20_000]
                        or str(existing["context_text"]) != str(context or "")[:40_000]
                    ):
                        raise RuntimeError(
                            "deterministic child identity conflicts with an existing request"
                        )
                    return self._public(existing, messages=True)
            owner = conn.execute(
                "SELECT depth FROM astb_child_handle WHERE child_chat_id=?",
                (str(parent_chat_id),),
            ).fetchone()
            depth = int(owner["depth"] if owner is not None else 0) + 1
            capacity = self.capacity()
            if depth > int(capacity["max_depth"]):
                raise RuntimeError(
                    f"child depth limit reached ({capacity['max_depth']})"
                )
            count = int(conn.execute(
                "SELECT COUNT(*) FROM astb_child_handle WHERE parent_chat_id=? "
                "AND status IN ('queued','running')",
                (str(parent_chat_id),),
            ).fetchone()[0])
            if count >= int(capacity["max_admitted"]):
                raise RuntimeError(
                    f"child admission-capacity limit reached ({capacity['max_admitted']})"
                )
            child_id = selected_child_id or "child_" + uuid.uuid4().hex
            child_chat_id = selected_chat_id or "childchat_" + uuid.uuid4().hex
            now = time.time()
            if parent_runtime.identity.action_surface != ACTION_SURFACE:
                raise RuntimeError(
                    "children require the current VARIANT-1 action surface"
                )
            child_identity = replace(
                parent_runtime.identity,
                action_surface=ACTION_SURFACE,
                discovery_state_ref="",
                mount_revision=(
                    0 if parent_runtime.identity.mount_revision is not None else None
                ),
                selected_category_id="",
                overlay_revision=0,
            )
            if fresh_catalog:
                # A new explicit goal gets today's base APIs without rebasing
                # or restarting its parent chat's pinned Python environment.
                from .service import environment_digest
                catalog=self.host.require_runtime().catalog
                if catalog is None:raise RuntimeError('Current goal catalog is unavailable')
                child_identity=replace(child_identity,catalog_release_id=catalog.current_release_id,
                    environment_digest=environment_digest(app_version=str(getattr(self.host,'version','0.1.0')),
                                                          catalog_release_id=catalog.current_release_id))
            runtime = self.runtimes.repository.ensure_runtime(
                child_chat_id,
                child_identity,
                creation_saga_state=f"child:{parent_chat_id}",
                connection=conn,
            )
            if runtime.creation_saga_state != f"child:{parent_chat_id}":
                raise RuntimeError("child runtime identity is owned by a different parent")
            if runtime.lifecycle_state != "active" or runtime.identity != child_identity:
                raise RuntimeError("child runtime identity is not available for this creation")
            remaining = {
                key: max(
                    0.0,
                    float(limit or 0.0)
                    - float(parent_runtime.budget_used.get(key) or 0.0),
                )
                for key, limit in parent_runtime.budget_limits.items()
                if float(limit or 0.0) > 0
            }
            if remaining:
                self.runtimes.repository.update_continuation(
                    child_chat_id, "ready", limits=remaining, connection=conn,
                )
            created = conn.execute(
                "INSERT INTO astb_child_handle(child_id, child_chat_id, parent_chat_id, "
                "depth, name, "
                "task_text, context_text, action_surface, model_route_json, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                (
                    child_id, child_chat_id, str(parent_chat_id), depth,
                    str(name or "worker")[:80], clean_task[:20_000],
                    str(context or "")[:40_000],
                    child_identity.action_surface, json.dumps(model_route, sort_keys=True), now, now,
                ),
            )
            if created.rowcount == 1:
                created_revision = self._clock_revision(conn)
        if created_revision is not None:
            self._notify_committed_change(
                str(parent_chat_id), str(child_id), created_revision
            )
        try:
            await self._enqueue(child_id)
        except BaseException as exc:
            failed_revision = None
            with self._lock, self._connect() as conn:
                changed = conn.execute(
                    "UPDATE astb_child_handle SET status='interrupted',error=?,"
                    "updated_at=? WHERE child_id=? AND status='queued'",
                    (f"Work admission failed: {exc}"[:2000], time.time(), child_id),
                )
                if changed.rowcount == 1:
                    failed_revision = self._clock_revision(conn)
            if failed_revision is not None:
                self._notify_committed_change(
                    str(parent_chat_id), str(child_id), failed_revision
                )
            raise
        return self.inspect(parent_chat_id, child_id)

    async def _run(
        self,
        child_id: str,
        *,
        cancellation_requested=None,
    ) -> None:
        row = self._row(child_id)
        if row is None:
            return
        started = time.time()
        claimed_revision = None
        with self._lock, self._connect() as conn:
            claimed = conn.execute(
                "UPDATE astb_child_handle SET status='running', started_at=?, updated_at=? "
                "WHERE child_id=? AND status IN ('queued','running','interrupted')",
                (started, started, child_id),
            )
            if claimed.rowcount != 1:
                # Cancellation can durably win before this coroutine receives
                # its first event-loop turn. Never execute a child whose
                # queued -> running claim lost that compare-and-set.
                return
            claimed_revision = self._clock_revision(conn)
        self._notify_committed_change(
            str(row["parent_chat_id"]), str(child_id), int(claimed_revision)
        )
        usage = {
            "llm_calls": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "wall_time_s": 0.0,
        }

        admission_id = ""

        def finish_usage(completed: float) -> None:
            usage["wall_time_s"] = max(0.0, completed - started)

        try:
            from session_catalog.child_worker import run_child_worker,ChildWorkerPorts
            from llm_usage import observe_usage, observe_usage_category
            from run_context import bind_run_context
            from model_runtime.context import validate_worker_model_route

            validate_worker_model_route(self.host.router, self._child_model_route(row))

            def observe(event: dict[str, Any]) -> None:
                usage["llm_calls"] += 1
                usage["total_tokens"] += int(
                    event.get("total_tokens") or event.get("tokens") or 0
                )
                usage["cost_usd"] += float(event.get("cost_usd") or 0.0)

            child_runtime = self.runtimes.ensure_runtime(row["child_chat_id"])
            admission_id = await self.runtimes.reserve_run(row["child_chat_id"], background=True)
            if not admission_id:
                raise RuntimeError("child runtime already has an admitted run")
            self.runtimes.bind_admission_task(admission_id, asyncio.current_task())
            session = self.runtimes.attached_session(row["child_chat_id"])
            runtime_prompt = ""
            catalog = self.host.require_runtime().catalog
            if catalog is not None and child_runtime.identity.action_surface == ACTION_SURFACE:
                runtime_prompt = catalog.runtime_prompt(row["child_chat_id"], row["task_text"])
            ctx = self.host.make_run_context(
                "subagent",
                row["task_text"],
                session=session,
                inherit_parent=False,
                isolate_desktop=True,
                metadata={
                    "_server_bound_kind": "subagent",
                    "parent_chat_id": row["parent_chat_id"],
                    "child_id": child_id,
                    "runtime_profile": child_runtime.identity.action_surface,
                    "parent_thread_id": row["parent_chat_id"],
                    "chat_id": row["child_chat_id"],
                    "runtime_identity": child_runtime.identity.to_dict(),
                    "runtime_prompt": runtime_prompt,
                },
            )
            with (
                bind_run_context(ctx),
                observe_usage(observe),
                observe_usage_category("child_session"),
            ):
                outcome_run_revision = None
                with self._lock,self._connect() as conn:
                    changed = conn.execute(
                        'UPDATE astb_child_handle SET outcome_run_id=? '
                        'WHERE child_id=? AND outcome_run_id<>?',
                        (ctx.run_id, child_id, ctx.run_id),
                    )
                    if changed.rowcount == 1:
                        outcome_run_revision = self._clock_revision(conn)
                if outcome_run_revision is not None:
                    self._notify_committed_change(
                        str(row["parent_chat_id"]),
                        str(child_id),
                        outcome_run_revision,
                    )
                self.runtimes.begin_run(admission_id, run_id=ctx.run_id,
                                        thread_id=ctx.run_id, source="subagent")
                ports=self.host.child_worker_ports()
                if isinstance(ports,ChildWorkerPorts):
                    ports=replace(ports,bind_execution_run=lambda run_id:self.bind_outcome_run(child_id,row['run_generation'],run_id))
                result = await run_child_worker(
                    ports,
                    row["task_text"],
                    row["context_text"],
                    inbound_messages=lambda: self._drain_messages(child_id),
                    ack_inbound=lambda message_ids: self._ack_messages(
                        child_id, message_ids
                    ),
                    cancellation_requested=cancellation_requested,
                )
            raw = str(result or "")
            artifact_ref = ""
            projected = raw
            if len(raw.encode("utf-8")) > 64_000:
                artifact = self.artifact_store.put_text(
                    raw, kind="child_result", scope=row["parent_chat_id"]
                )
                artifact_ref = artifact.ref
                projected = raw[:20_000]
            status = (
                "cancelled" if "INTERRUPTED" in raw[:100]
                else "completed" if "FAILED" not in raw[:100]
                else "failed"
            )
            completed = time.time()
            finish_usage(completed)
            self._finish_child(
                child_id,
                status=status,
                usage=usage,
                completed=completed,
                result_text=projected,
                artifact_ref=artifact_ref,
                parent_message=(status == "completed"),
            )
        except asyncio.CancelledError:
            completed = time.time()
            finish_usage(completed)
            self._finish_child(
                child_id,
                status="cancelled",
                usage=usage,
                completed=completed,
                error="cancelled by parent",
            )
            raise
        except Exception as exc:
            completed = time.time()
            finish_usage(completed)
            self._finish_child(
                child_id,
                status="failed",
                usage=usage,
                completed=completed,
                error=str(exc),
            )

        finally:
            if admission_id:
                terminal = self._row(child_id)
                self.runtimes.finish_run(admission_id, status=str(terminal["status"] if terminal else "failed"))

    def send(self, parent_chat_id: str, child_id: str, text: str) -> dict[str, Any]:
        row = self._row(child_id)
        if row is None or row["parent_chat_id"] != str(parent_chat_id):
            raise LookupError("unknown child handle")
        if str(row["status"]) not in {"queued", "running", "interrupted"}:
            raise RuntimeError("child is terminal; restart it before sending guidance")
        clean = str(text or "").strip()
        if not clean:
            raise ValueError("child message is empty")
        revision = None
        with self._lock, self._connect() as conn:
            created = conn.execute(
                "INSERT INTO astb_child_message(message_id, child_id, direction, text, created_at) "
                "VALUES (?, ?, 'parent_to_child', ?, ?)",
                ("cmsg_" + uuid.uuid4().hex, child_id, clean[:20_000], time.time()),
            )
            if created.rowcount == 1:
                revision = self._clock_revision(conn)
        if revision is not None:
            self._notify_committed_change(
                str(parent_chat_id), str(child_id), revision
            )
        return self.inspect(parent_chat_id, child_id)

    def _drain_messages(self, child_id: str) -> list[dict[str, Any]]:
        """Read pending parent messages without acknowledging delivery."""

        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT message_id, text, created_at FROM astb_child_message "
                "WHERE child_id=? AND direction='parent_to_child' AND consumed_at IS NULL "
                "ORDER BY created_at, message_id LIMIT 20",
                (str(child_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def _ack_messages(self, child_id: str, message_ids) -> int:
        """Acknowledge IDs only after their worker boundary is durable."""

        identities = tuple(dict.fromkeys(
            str(item or "").strip()
            for item in message_ids or ()
            if str(item or "").strip()
        ))
        if not identities:
            return 0
        now = time.time()
        parent_chat_id = ""
        revision = None
        with self._lock, self._connect() as conn:
            owner = conn.execute(
                "SELECT parent_chat_id FROM astb_child_handle WHERE child_id=?",
                (str(child_id),),
            ).fetchone()
            parent_chat_id = str(
                owner["parent_chat_id"] if owner is not None else ""
            )
            changed = 0
            for message_id in identities:
                result = conn.execute(
                    "UPDATE astb_child_message SET consumed_at=? "
                    "WHERE child_id=? AND message_id=? "
                    "AND direction='parent_to_child' AND consumed_at IS NULL",
                    (now, str(child_id), message_id),
                )
                changed += int(result.rowcount or 0)
            if changed:
                revision = self._clock_revision(conn)
        if revision is not None:
            self._notify_committed_change(
                parent_chat_id, str(child_id), revision
            )
        return changed

    async def restart(self, parent_chat_id: str, child_id: str, *,
                      expected_generation=None, request_id='', message='') -> dict[str, Any]:
        row = self._row(child_id)
        if row is None or row["parent_chat_id"] != str(parent_chat_id):
            raise LookupError("unknown child handle")
        if request_id and row['restart_request_id']==request_id:
            if row['restart_message']!=message:
                raise RuntimeError('restart request_id already has different guidance')
            if row['status']=='queued':
                await self._enqueue(child_id)
            return self.inspect(parent_chat_id,child_id)
        if expected_generation is not None and row['run_generation']!=expected_generation:
            raise RuntimeError('child generation changed before restart')
        if str(row["status"]) in {"queued", "running"}:
            raise RuntimeError("child is already active")
        if str(row["usage_rollup_state"] or "") == "pending":
            self._rollup_usage(str(child_id))
            row = self._row(child_id)
            if row is None or str(row["usage_rollup_state"] or "") == "pending":
                raise RuntimeError(
                    "child usage roll-up is incomplete; retry after its budget "
                    "authority is available"
                )
        restart_revision = None
        with self._lock, self._connect() as conn:
            # Fence concurrent restart callers again under the write lock.
            latest=conn.execute('SELECT * FROM astb_child_handle WHERE child_id=?',(child_id,)).fetchone()
            if request_id and latest['restart_request_id']==request_id:
                if latest['restart_message']!=message:
                    raise RuntimeError('restart request_id already has different guidance')
                return self._public(latest)
            if latest['run_generation']!=row['run_generation'] or latest['status'] in {'queued','running'}:
                raise RuntimeError('child changed before restart; refresh its handle')
            conn.execute(
                'UPDATE astb_child_handle SET outcome_json=?,outcome_run_id=?,'
                'restart_request_id=?,restart_message=? WHERE child_id=?',
                ('{}', '', request_id, message, child_id),
            )
            if message:
                conn.execute("INSERT INTO astb_child_message(message_id,child_id,direction,text,created_at) VALUES (?,?, 'parent_to_child',?,?)",
                             ('cmsg_'+uuid.uuid4().hex,child_id,str(message)[:20000],time.time()))
            active = int(conn.execute(
                "SELECT COUNT(*) FROM astb_child_handle WHERE parent_chat_id=? "
                "AND status IN ('queued','running')",
                (str(parent_chat_id),),
            ).fetchone()[0])
            maximum = int(self.capacity()["max_admitted"])
            if active >= maximum:
                raise RuntimeError(
                    f"child admission-capacity limit reached ({maximum})"
                )
            changed = conn.execute(
                "UPDATE astb_child_handle SET status='queued', result_text='', "
                "artifact_ref='', error='', usage_json='{}', started_at=NULL, "
                "completed_at=NULL,run_generation=run_generation+1,"
                "usage_rollup_state='complete',usage_rollup_error='',work_job_id='',updated_at=? "
                "WHERE child_id=?",
                (time.time(), str(child_id)),
            )
            if changed.rowcount == 1:
                restart_revision = self._clock_revision(conn)
        if restart_revision is not None:
            self._notify_committed_change(
                str(parent_chat_id), str(child_id), restart_revision
            )
        await self._enqueue(str(child_id))
        return self.inspect(parent_chat_id, child_id)

    def inspect(self, parent_chat_id: str, child_id: str) -> dict[str, Any]:
        row = self._row(child_id)
        if row is None or row["parent_chat_id"] != str(parent_chat_id):
            raise LookupError("unknown child handle")
        return self._public(row, messages=True)

    def list(self, parent_chat_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        cap = max(1, min(int(limit), 100))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM astb_child_handle WHERE parent_chat_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (str(parent_chat_id), cap),
            ).fetchall()
        return [self._public(row) for row in rows]

    def tree(self, parent_chat_id: str, *, limit: int = 100) -> dict[str, Any]:
        cap = max(1, min(int(limit), 100))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                WITH RECURSIVE descendants AS (
                    SELECT *, 1 AS tree_level
                    FROM astb_child_handle
                    WHERE parent_chat_id=?
                    UNION ALL
                    SELECT child.*, descendants.tree_level + 1
                    FROM astb_child_handle AS child
                    JOIN descendants
                      ON child.parent_chat_id=descendants.child_chat_id
                )
                SELECT * FROM descendants
                ORDER BY tree_level, created_at, child_id
                LIMIT ?
                """,
                (str(parent_chat_id), cap + 1),
            ).fetchall()
        truncated = len(rows) > cap
        items = []
        for row in rows[:cap]:
            public = self._public(row)
            items.append({
                "child_id": public.get("child_id"),
                "child_chat_id": public.get("child_chat_id"),
                "parent_chat_id": public.get("parent_chat_id"),
                "depth": int(public.get("depth") or 0),
                "name": public.get("name"),
                "status": public.get("status"),
                "work_job_id": public.get("work_job_id"),
                "created_at": float(public.get("created_at") or 0.0),
                "usage": dict(public.get("usage") or {}),
                "reported_text": str(public.get("reported_text") or "")[:500],
            })
        statuses: dict[str, int] = {}
        for item in items:
            status = str(item.get("status") or "unknown")
            statuses[status] = statuses.get(status, 0) + 1
        return {
            "root_chat_id": str(parent_chat_id),
            "capacity": self.capacity(),
            "total": len(items),
            "truncated": truncated,
            "statuses": statuses,
            "items": items,
        }

    async def cancel(self, parent_chat_id: str, child_id: str) -> dict[str, Any]:
        row = self._row(child_id)
        if row is None or row["parent_chat_id"] != str(parent_chat_id):
            raise LookupError("unknown child handle")
        work = self._require_work()
        job_id = str(row["work_job_id"] or "")
        job = work.jobs.get(job_id) if job_id else None
        if job is not None and not job.terminal:
            work.jobs.cancel(job_id, reason="cancelled by parent")
            work.scheduler.cancel_active(job_id)
        completed = time.time()
        empty_usage = json.dumps(
            {
                "llm_calls": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "wall_time_s": 0.0,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        cancelled_revision = None
        with self._lock, self._connect() as conn:
            # A queued Work job is terminal immediately. Running work observes
            # cancel_requested and owns final child cleanup.
            changed = conn.execute(
                "UPDATE astb_child_handle SET status='cancelled', "
                "error='cancelled by parent', "
                "usage_json=CASE WHEN usage_json IN ('', '{}') THEN ? ELSE usage_json END, "
                "usage_rollup_state='complete',usage_rollup_error='',"
                "completed_at=?, updated_at=? "
                "WHERE child_id=? AND status='queued'",
                (empty_usage, completed, completed, str(child_id)),
            )
            if changed.rowcount == 1:
                cancelled_revision = self._clock_revision(conn)
        if cancelled_revision is not None:
            self._notify_committed_change(
                str(parent_chat_id), str(child_id), cancelled_revision
            )
        if job is not None and job.status == "running":
            try:
                await work.jobs.wait(job_id, timeout_s=10)
            except TimeoutError:
                pass
        return self.inspect(parent_chat_id, child_id)

    async def delete_chat(self, chat_id: str) -> None:
        # Internal cleanup is not capped by the model-facing list limit. Each
        # handle remains a durable retry owner until its runtime, kernel,
        # snapshots, and descendants have all been removed successfully.
        with self._lock, self._connect() as conn:
            children = conn.execute(
                "SELECT * FROM astb_child_handle WHERE parent_chat_id=? "
                "ORDER BY created_at, child_id",
                (str(chat_id),),
            ).fetchall()
        failures: list[str] = []
        for raw_row in children:
            row = dict(raw_row)
            child_id = str(row["child_id"])
            child_chat_id = str(row.get("child_chat_id") or "")
            parent_chat_id = str(row.get("parent_chat_id") or chat_id)
            cleaning_revision = None
            with self._lock, self._connect() as conn:
                changed = conn.execute(
                    "UPDATE astb_child_handle SET deletion_state='cleaning', "
                    "deletion_error='', updated_at=? WHERE child_id=?",
                    (time.time(), child_id),
                )
                if changed.rowcount == 1:
                    cleaning_revision = self._clock_revision(conn)
            if cleaning_revision is not None:
                self._notify_committed_change(
                    parent_chat_id, child_id, cleaning_revision
                )
            try:
                job_id = str(row.get("work_job_id") or "")
                if self.work is not None and job_id:
                    job = self.work.jobs.get(job_id)
                    if job is not None and not job.terminal:
                        self.work.jobs.cancel(
                            job_id, reason="parent chat deleted",
                        )
                        self.work.scheduler.cancel_active(job_id)
                        try:
                            await self.work.jobs.wait(job_id, timeout_s=10)
                        except TimeoutError:
                            raise RuntimeError(
                                f"child Work job did not stop: {job_id}"
                            )
                if child_chat_id:
                    await self.runtimes.delete_child_runtime(
                        child_chat_id,
                        parent_chat_id=str(chat_id),
                    )
                deleted_revision = None
                with self._lock, self._connect() as conn:
                    conn.execute(
                        "DELETE FROM astb_child_message WHERE child_id=?",
                        (child_id,),
                    )
                    deleted = conn.execute(
                        "DELETE FROM astb_child_handle WHERE child_id=?",
                        (child_id,),
                    )
                    if deleted.rowcount == 1:
                        deleted_revision = self._clock_revision(conn)
                if deleted_revision is not None:
                    # The row no longer exists, so retain its committed owner
                    # from the pre-delete record in the invalidation event.
                    self._notify_committed_change(
                        parent_chat_id, child_id, deleted_revision
                    )
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                detail = f"{type(exc).__name__}: {exc}"[:2000]
                failed_revision = None
                with self._lock, self._connect() as conn:
                    changed = conn.execute(
                        "UPDATE astb_child_handle SET deletion_state='failed', "
                        "deletion_error=?, updated_at=? WHERE child_id=?",
                        (detail, time.time(), child_id),
                    )
                    if changed.rowcount == 1:
                        failed_revision = self._clock_revision(conn)
                if failed_revision is not None:
                    self._notify_committed_change(
                        parent_chat_id, child_id, failed_revision
                    )
                failures.append(f"{child_id}: {detail}")
        if failures:
            raise RuntimeError(
                "child cleanup remains incomplete: " + "; ".join(failures[:20])
            )

    async def cancel_chat(self, chat_id: str) -> int:
        """Stop descendants while retaining their durable handle history."""

        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                WITH RECURSIVE descendants AS (
                    SELECT *, 1 AS tree_level
                    FROM astb_child_handle
                    WHERE parent_chat_id=?
                    UNION ALL
                    SELECT child.*, descendants.tree_level + 1
                    FROM astb_child_handle AS child
                    JOIN descendants
                      ON child.parent_chat_id=descendants.child_chat_id
                )
                SELECT * FROM descendants
                ORDER BY tree_level DESC, created_at DESC
                """,
                (str(chat_id),),
            ).fetchall()
        items = [self._public(row) for row in rows]
        cancelled = 0
        kernel = self.host.require_runtime().kernel
        for item in items:
            status = str(item.get("status") or "")
            if status not in {"completed", "failed", "cancelled"}:
                settled = await self.cancel(
                    str(item.get("parent_chat_id") or chat_id),
                    str(item.get("child_id") or ""),
                )
                if str(settled.get("status") or "") not in {
                    "completed", "failed", "cancelled",
                }:
                    raise RuntimeError(
                        "child Work job did not stop before parent tombstone: "
                        + str(item.get("child_id") or "")
                    )
                cancelled += 1
            child_chat_id = str(item.get("child_chat_id") or "")
            if child_chat_id:
                await kernel.close_chat(
                    child_chat_id,
                    reason="parent_chat_tombstoned",
                )
        return cancelled

    def descendants_for_cleanup(self, chat_id: str):
        """Internal ownership traversal, without the model-facing tree cap."""
        with self._lock,self._connect() as conn:
            rows=conn.execute('''WITH RECURSIVE descendants AS (
                SELECT * FROM astb_child_handle WHERE parent_chat_id=?
                UNION
                SELECT child.* FROM astb_child_handle child JOIN descendants parent
                ON child.parent_chat_id=parent.child_chat_id
            ) SELECT * FROM descendants ORDER BY depth DESC,created_at,child_id''',(str(chat_id),)).fetchall()
        return [self._public(row) for row in rows]


def _child_handle(
    manager: ChildSessionManager,
    context: Any,
    row: Mapping[str, Any],
) -> dict[str, Any]:
    return remote_handle_envelope(
        service="children",
        kind="child",
        handle_id=str(row.get("child_id") or ""),
        generation=max(1, int(row.get("run_generation") or 1)),
        revision=1,
        metadata={
            "name": str(row.get("name") or "worker")[:80],
            "status": str(row.get("status") or ""),
            "work_job_id": str(row.get("work_job_id") or ""),
            "reported_text": str(row.get("reported_text") or "")[:2_000],
            "terminal": str(row.get("status") or "") in {
                "completed", "failed", "cancelled",
            },
        },
        methods=CHILD_HANDLE_METHODS,
        broker=manager.host.require_runtime().broker,
        context=context,
    )


async def _child_handle_router(
    manager: ChildSessionManager,
    context: Any,
    identity: Mapping[str, Any],
    method: str,
    arguments: dict[str, Any],
    *, control_only: bool = False,
) -> Any:
    if str(identity.get("kind") or "") != "child":
        raise ToolError("children handle kind is unsupported")
    child_id = str(identity.get("id") or "")
    row = manager.inspect(context.chat_id, child_id)
    if method == "refresh" and not control_only:
        if arguments:
            raise ToolError("children.child.refresh takes no arguments")
        return _child_handle(manager, context, row)
    expected_generation = max(1, int(row.get("run_generation") or 1))
    if int(identity.get("generation") or -1) != expected_generation:
        raise ToolError(
            f"stale children.child handle: expected generation "
            f"{expected_generation}; call refresh()"
        )
    if control_only:
        return ((method == "cancel" and not arguments)
                or (method == "send" and set(arguments) == {"text"}
                    and isinstance(arguments["text"], str) and bool(arguments["text"])))
    if method == "inspect":
        if arguments:
            raise ToolError("children.child.inspect takes no arguments")
        return row
    if method == "wait":
        unknown = sorted(set(arguments) - {"timeout_s"})
        if unknown:
            raise ToolError(
                "children.child.wait: unknown argument(s): "
                + ", ".join(unknown)
            )
        timeout_s = max(
            0.0, min(float(arguments.get("timeout_s", 30.0)), 30.0)
        )
        job_id = str(row.get("work_job_id") or "")
        if job_id and str(row.get("status") or "") not in {
            "completed", "failed", "cancelled",
        }:
            work = manager._require_work()
            try:
                await work.jobs.wait(job_id, timeout_s=timeout_s)
            except TimeoutError:
                # A bounded observation expiring is pending work, not failure.
                pass
        return _child_handle(
            manager, context, manager.inspect(context.chat_id, child_id)
        )
    if method == "send":
        if set(arguments) != {"text"} or not str(arguments.get("text") or ""):
            raise ToolError("children.child.send needs only non-empty 'text'")
        row = manager.send(context.chat_id, child_id, str(arguments["text"]))
        return _child_handle(manager, context, row)
    if method == "cancel":
        if arguments:
            raise ToolError("children.child.cancel takes no arguments")
        row = await manager.cancel(context.chat_id, child_id)
        return _child_handle(manager, context, row)
    if method == "restart":
        if arguments:
            raise ToolError("children.child.restart takes no arguments")
        row = await manager.restart(context.chat_id, child_id)
        return _child_handle(manager, context, row)
    raise ToolError(f"unsupported children.child method: {method}")


def register_children_tool(
    registry: Any,
    manager: ChildSessionManager | None,
) -> None:
    """Install one slot-owned ``children`` seed and no lifecycle handlers."""

    if registry.get("children") is not None:
        return

    async def children(args: dict[str, Any]) -> Any:
        if manager is None:
            raise ToolError("children runtime is unavailable")
        invocation = current_capability_invocation()
        if invocation is None or not invocation.chat_id:
            raise ToolError("children require an active admitted Python cell")
        operation = str(args.get("operation") or "")
        payload = _child_method_arguments(operation, args)
        if operation == "spawn":
            row = await manager.spawn(
                invocation.chat_id,
                task=str(payload.get("task") or ""),
                name=str(payload.get("name") or ""),
                context=str(payload.get("context") or ""),
            )
            return _child_handle(manager, invocation, row)
        if operation == "list":
            return [
                _child_handle(manager, invocation, row)
                for row in manager.list(
                    invocation.chat_id, limit=int(payload.get("limit") or 20)
                )
            ]
        if operation == "tree":
            return manager.tree(
                invocation.chat_id, limit=int(payload.get("limit") or 100)
            )
        raise ToolError(f"unsupported children root operation: {operation}")

    registry.register(Tool(
        "children",
        "Spawn/list durable child-agent and subagent handles; continuation lives "
        "on each handle.",
        children,
        category="session_infrastructure",
        params=_child_seed_params(),
        hidden=True,
        visibility="broker_only",
        effect_class="external_side_effect",
        parallel_safe=False,
        may_return_secrets=True,
        schema_revision="variant1.children-seed.v4",
        handler_revision="variant1.children-seed-handler.v3",
        object_methods=CHILD_OBJECT_METHODS,
    ))

    if manager is not None:
        routers = getattr(manager.host, "remote_handle_routers", None)
        if routers is None:
            routers = {}
            setattr(manager.host, "remote_handle_routers", routers)
        if not isinstance(routers, dict):
            raise TypeError("host.remote_handle_routers must be a dictionary")
        routers["children"] = (
            lambda context, identity, method, arguments:
            _child_handle_router(
                manager, context, identity, method, dict(arguments or {})
            )
        )
        routers["children"].control_admission = (
            lambda context, identity, method, arguments:
            _child_handle_router(
                manager, context, identity, method, dict(arguments or {}), control_only=True,
            )
        )


__all__ = [
    "CHILD_HANDLE_METHODS",
    "CHILD_OBJECT_METHODS",
    "ChildSessionManager",
    "register_children_tool",
]
