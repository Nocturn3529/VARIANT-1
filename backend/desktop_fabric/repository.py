"""SQLite authority for Desktop Fabric identity, evidence, and delivery state."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
import os
import sqlite3
import time
import uuid
from typing import Any, Iterable, Iterator, Mapping

from core_invariants import (
    request_fingerprint,
    sqlite_read_connection,
    sqlite_unit_of_work,
    sqlite_wal_connection,
    sqlite_writer_lock,
)
from work_fabric.scope import (
    WorkScope,
    append_json_scope_visibility,
    work_scope_visible,
)

from .models import (
    AppRecord,
    DesktopCapture,
    DesktopConflict,
    DesktopElement,
    DesktopEvent,
    DesktopNotFound,
    DesktopObservation,
    DesktopOperation,
    DesktopScopeMismatch,
    DesktopValidationError,
    DELIVERY_MODES,
    OPERATION_STATES,
    ProcessIdentity,
    WindowRecord,
    canonical_json,
    coerce_scope,
)


def default_desktop_fabric_path(*, data_dir: str | None = None) -> str:
    if data_dir:
        root = os.path.abspath(data_dir)
    else:
        backend_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
        root = os.path.abspath(os.environ.get("VARIANT1_DATA_DIR") or backend_root)
        root = os.path.join(root, "data")
    return os.path.abspath(
        os.environ.get("VARIANT1_DESKTOP_FABRIC_DB")
        or os.path.join(root, "desktop", "desktop-fabric.sqlite3")
    )


def new_desktop_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _object(value: str | None, field: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except Exception as exc:
        raise DesktopValidationError(f"invalid persisted JSON in {field}") from exc
    if not isinstance(parsed, dict):
        raise DesktopValidationError(f"persisted {field} must be an object")
    return parsed


def _array(value: str | None, field: str) -> list[Any]:
    try:
        parsed = json.loads(value or "[]")
    except Exception as exc:
        raise DesktopValidationError(f"invalid persisted JSON in {field}") from exc
    if not isinstance(parsed, list):
        raise DesktopValidationError(f"persisted {field} must be an array")
    return parsed


class DesktopFabricRepository:
    """Durable registry and append-only event cursor.

    Transactions are intentionally short.  In particular, no UIA, capture, or
    input call is made while a database transaction is open.
    """

    def __init__(self, path: str | None = None, *, data_dir: str | None = None) -> None:
        if path and data_dir:
            raise ValueError("pass path or data_dir, not both")
        self.path = os.path.abspath(path or default_desktop_fabric_path(data_dir=data_dir))
        self._write_lock = sqlite_writer_lock(self.path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._initialize()

    @staticmethod
    def _append_scope_filter(
        clauses: list[str], params: list[Any], column: str,
        scope: WorkScope | Mapping[str, Any] | None,
    ) -> None:
        append_json_scope_visibility(clauses, params, column, scope)

    @staticmethod
    def _assert_scope(
        owner: WorkScope, scope: WorkScope | Mapping[str, Any] | None,
    ) -> None:
        if scope is not None and not work_scope_visible(owner, scope):
            raise DesktopScopeMismatch("desktop record is outside the owning WorkScope")

    def _connect(self) -> sqlite3.Connection:
        return sqlite_wal_connection(self.path)

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with sqlite_read_connection(self._connect) as conn:
            yield conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with sqlite_unit_of_work(
            self._connect, self._write_lock, fault_name="desktop.before_commit"
        ) as conn:
            yield conn

    def _initialize(self) -> None:
        with self._write_lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS desktop_app (
                      app_id TEXT PRIMARY KEY,
                      executable TEXT NOT NULL,
                      package_family TEXT NOT NULL,
                      app_user_model_id TEXT NOT NULL,
                      display_name TEXT NOT NULL,
                      processes_json TEXT NOT NULL,
                      installed INTEGER,
                      running INTEGER NOT NULL,
                      launch_identity_json TEXT NOT NULL,
                      revision INTEGER NOT NULL,
                      created_at REAL NOT NULL,
                      updated_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS desktop_app_running
                      ON desktop_app(running, updated_at DESC);

                    CREATE TABLE IF NOT EXISTS desktop_window (
                      window_id TEXT PRIMARY KEY,
                      app_id TEXT NOT NULL,
                      hwnd INTEGER NOT NULL,
                      pid INTEGER NOT NULL,
                      pid_started_at REAL NOT NULL,
                      executable TEXT NOT NULL,
                      package_family TEXT NOT NULL,
                      app_user_model_id TEXT NOT NULL,
                      class_name TEXT NOT NULL,
                      title TEXT NOT NULL,
                      owner_hwnd INTEGER NOT NULL,
                      root_owner_hwnd INTEGER NOT NULL,
                      monitor TEXT NOT NULL,
                      virtual_desktop TEXT NOT NULL,
                      dpi INTEGER NOT NULL,
                      bounds_json TEXT NOT NULL,
                      visible INTEGER NOT NULL,
                      minimized INTEGER NOT NULL,
                      cloaked INTEGER,
                      occluded INTEGER,
                      foreground INTEGER NOT NULL,
                      generation INTEGER NOT NULL,
                      backend_instance_id TEXT NOT NULL,
                      recovery_json TEXT NOT NULL,
                      revision INTEGER NOT NULL,
                      created_at REAL NOT NULL,
                      updated_at REAL NOT NULL,
                      missing_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS desktop_window_app
                      ON desktop_window(app_id, missing_at, updated_at DESC);
                    CREATE INDEX IF NOT EXISTS desktop_window_identity
                      ON desktop_window(hwnd, pid, pid_started_at);

                    CREATE TABLE IF NOT EXISTS desktop_capture (
                      capture_id TEXT PRIMARY KEY,
                      window_id TEXT NOT NULL,
                      window_generation INTEGER NOT NULL,
                      artifact_ref TEXT NOT NULL,
                      bytes INTEGER NOT NULL,
                      width INTEGER NOT NULL,
                      height INTEGER NOT NULL,
                      provenance TEXT NOT NULL,
                      occlusion_independent INTEGER NOT NULL,
                      minimized INTEGER NOT NULL,
                      stale INTEGER NOT NULL,
                      coordinate_transform_json TEXT NOT NULL,
                      created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS desktop_capture_window
                      ON desktop_capture(window_id, created_at DESC);

                    CREATE TABLE IF NOT EXISTS desktop_observation (
                      observation_id TEXT PRIMARY KEY,
                      window_id TEXT NOT NULL,
                      window_generation INTEGER NOT NULL,
                      mode TEXT NOT NULL,
                      elements_json TEXT NOT NULL,
                      capture_id TEXT NOT NULL,
                      image_ref TEXT NOT NULL,
                      capture_json TEXT NOT NULL,
                      completeness TEXT NOT NULL,
                      uia_generation INTEGER NOT NULL,
                      fingerprint TEXT NOT NULL,
                      created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS desktop_observation_window
                      ON desktop_observation(window_id, created_at DESC);

                    CREATE TABLE IF NOT EXISTS desktop_operation (
                      operation_id TEXT PRIMARY KEY,
                      window_id TEXT NOT NULL,
                      window_generation INTEGER NOT NULL,
                      scope_json TEXT NOT NULL,
                      idempotency_key TEXT NOT NULL,
                      request_digest TEXT NOT NULL,
                      action TEXT NOT NULL,
                      delivery TEXT NOT NULL,
                      target_json TEXT NOT NULL,
                      arguments_json TEXT NOT NULL,
                      expectation_json TEXT NOT NULL,
                      state TEXT NOT NULL,
                      before_observation_id TEXT NOT NULL,
                      after_observation_id TEXT NOT NULL,
                      dispatch_json TEXT NOT NULL,
                      evidence_json TEXT NOT NULL,
                      error TEXT NOT NULL,
                      backend_instance_id TEXT NOT NULL,
                      revision INTEGER NOT NULL,
                      created_at REAL NOT NULL,
                      dispatched_at REAL NOT NULL,
                      observed_at REAL NOT NULL,
                      completed_at REAL NOT NULL,
                      updated_at REAL NOT NULL
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS desktop_operation_idempotency
                      ON desktop_operation(idempotency_key)
                      WHERE idempotency_key <> '';
                    CREATE INDEX IF NOT EXISTS desktop_operation_state
                      ON desktop_operation(state, updated_at);
                    CREATE INDEX IF NOT EXISTS desktop_operation_scope
                      ON desktop_operation(window_id, created_at DESC);

                    CREATE TABLE IF NOT EXISTS desktop_event (
                      sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                      event_id TEXT NOT NULL UNIQUE,
                      entity_kind TEXT NOT NULL,
                      entity_id TEXT NOT NULL,
                      event_type TEXT NOT NULL,
                      revision INTEGER NOT NULL,
                      payload_json TEXT NOT NULL,
                      created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS desktop_event_entity
                      ON desktop_event(entity_kind, entity_id, sequence);
                    """
                )
                columns = {row[1] for row in conn.execute("PRAGMA table_info(desktop_observation)")}
                if "scope_json" not in columns:
                    conn.execute("ALTER TABLE desktop_observation ADD COLUMN scope_json TEXT NOT NULL DEFAULT '{}'")
            finally:
                conn.close()

    @staticmethod
    def _event(
        conn: sqlite3.Connection, *, entity_kind: str, entity_id: str,
        event_type: str, revision: int, payload: Mapping[str, Any] | None = None,
        at: float | None = None,
    ) -> None:
        conn.execute(
            """INSERT INTO desktop_event(
                 event_id, entity_kind, entity_id, event_type, revision,
                 payload_json, created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (new_desktop_id("evt"), entity_kind, entity_id, event_type,
             int(revision), canonical_json(dict(payload or {})),
             float(at if at is not None else time.time())),
        )

    @staticmethod
    def _app(row: sqlite3.Row) -> AppRecord:
        processes = tuple(
            ProcessIdentity(pid=int(item["pid"]), started_at=float(item["started_at"]))
            for item in _array(row["processes_json"], "processes")
        )
        installed = row["installed"]
        return AppRecord(
            app_id=row["app_id"], executable=row["executable"],
            package_family=row["package_family"],
            app_user_model_id=row["app_user_model_id"],
            display_name=row["display_name"], processes=processes,
            installed=None if installed is None else bool(installed),
            running=bool(row["running"]),
            launch_identity=_object(row["launch_identity_json"], "launch_identity"),
            revision=int(row["revision"]), created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _window(row: sqlite3.Row) -> WindowRecord:
        bounds_raw = _array(row["bounds_json"], "bounds")
        bounds = tuple(int(value) for value in bounds_raw) if bounds_raw else None
        if bounds is not None and len(bounds) != 4:
            raise DesktopValidationError("persisted bounds must have four integers")
        def tri(value: Any) -> bool | None:
            return None if value is None else bool(value)
        return WindowRecord(
            window_id=row["window_id"], app_id=row["app_id"],
            hwnd=int(row["hwnd"]), pid=int(row["pid"]),
            pid_started_at=float(row["pid_started_at"]),
            executable=row["executable"], package_family=row["package_family"],
            app_user_model_id=row["app_user_model_id"],
            class_name=row["class_name"], title=row["title"],
            owner_hwnd=int(row["owner_hwnd"]),
            root_owner_hwnd=int(row["root_owner_hwnd"]), monitor=row["monitor"],
            virtual_desktop=row["virtual_desktop"], dpi=int(row["dpi"]),
            bounds=bounds, visible=bool(row["visible"]),
            minimized=bool(row["minimized"]), cloaked=tri(row["cloaked"]),
            occluded=tri(row["occluded"]), foreground=bool(row["foreground"]),
            generation=int(row["generation"]),
            backend_instance_id=row["backend_instance_id"],
            recovery=_object(row["recovery_json"], "recovery"),
            revision=int(row["revision"]), created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]), missing_at=float(row["missing_at"]),
        )

    @staticmethod
    def _element(raw: Mapping[str, Any], observation_id: str) -> DesktopElement:
        bounds_raw = raw.get("bounds")
        bounds = tuple(int(v) for v in bounds_raw) if bounds_raw else None
        return DesktopElement(
            element_ref=str(raw.get("ref") or ""), observation_id=observation_id,
            window_id=str(raw.get("window_id") or ""),
            window_generation=int(raw.get("window_generation") or 0),
            element_generation=int(raw.get("element_generation") or 0),
            role=str(raw.get("role") or ""), name=str(raw.get("name") or ""),
            text=str(raw.get("text") or ""), value=str(raw.get("value") or ""),
            states=dict(raw.get("states") or {}),
            patterns=tuple(str(v) for v in raw.get("patterns") or ()), bounds=bounds,
            runtime_id=tuple(int(v) for v in raw.get("runtime_id") or ()),
            automation_id=str(raw.get("automation_id") or ""),
            semantic_path=str(raw.get("semantic_path") or ""),
            confidence=float(raw.get("confidence", 1.0)),
            actionable=bool(raw.get("actionable", False)),
            provenance=tuple(str(v) for v in raw.get("provenance") or ()),
            fingerprint=str(raw.get("fingerprint") or ""),
            backend_key=str(raw.get("backend_key") or ""),
        )

    @classmethod
    def _observation(cls, row: sqlite3.Row, *, event_cursor: int = 0) -> DesktopObservation:
        raw_elements = _array(row["elements_json"], "elements")
        observation_id = row["observation_id"]
        return DesktopObservation(
            observation_id=observation_id, window_id=row["window_id"],
            window_generation=int(row["window_generation"]), mode=row["mode"],
            elements=tuple(cls._element(item, observation_id) for item in raw_elements),
            capture_id=row["capture_id"], image_ref=row["image_ref"],
            capture=_object(row["capture_json"], "capture"),
            completeness=row["completeness"],
            uia_generation=int(row["uia_generation"]), event_cursor=int(event_cursor),
            fingerprint=row["fingerprint"], created_at=float(row["created_at"]),
            scope=coerce_scope(_object(row["scope_json"], "scope")),
        )

    @staticmethod
    def _capture(row: sqlite3.Row) -> DesktopCapture:
        return DesktopCapture(
            capture_id=row["capture_id"], window_id=row["window_id"],
            window_generation=int(row["window_generation"]),
            artifact_ref=row["artifact_ref"], bytes=int(row["bytes"]),
            width=int(row["width"]), height=int(row["height"]),
            provenance=row["provenance"],
            occlusion_independent=bool(row["occlusion_independent"]),
            minimized=bool(row["minimized"]), stale=bool(row["stale"]),
            coordinate_transform=_object(
                row["coordinate_transform_json"], "coordinate_transform"),
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _operation(row: sqlite3.Row) -> DesktopOperation:
        return DesktopOperation(
            operation_id=row["operation_id"], window_id=row["window_id"],
            window_generation=int(row["window_generation"]),
            scope=WorkScope.from_mapping(_object(row["scope_json"], "scope")),
            idempotency_key=row["idempotency_key"], action=row["action"],
            delivery=row["delivery"], target=_object(row["target_json"], "target"),
            arguments=_object(row["arguments_json"], "arguments"),
            expectation=_object(row["expectation_json"], "expectation"),
            state=row["state"], before_observation_id=row["before_observation_id"],
            after_observation_id=row["after_observation_id"],
            dispatch=_object(row["dispatch_json"], "dispatch"),
            evidence=_object(row["evidence_json"], "evidence"), error=row["error"],
            backend_instance_id=row["backend_instance_id"],
            revision=int(row["revision"]), created_at=float(row["created_at"]),
            dispatched_at=float(row["dispatched_at"]),
            observed_at=float(row["observed_at"]),
            completed_at=float(row["completed_at"]), updated_at=float(row["updated_at"]),
        )

    def upsert_catalog(
        self, apps: Iterable[AppRecord], windows: Iterable[WindowRecord], *,
        backend_instance_id: str,
    ) -> tuple[list[AppRecord], list[WindowRecord]]:
        now = time.time()
        app_items = list(apps)
        window_items = list(windows)
        seen_apps = {item.app_id for item in app_items}
        seen_windows = {item.window_id for item in window_items}
        with self._write() as conn:
            for item in app_items:
                previous = conn.execute(
                    "SELECT revision, created_at FROM desktop_app WHERE app_id=?",
                    (item.app_id,),
                ).fetchone()
                revision = int(previous["revision"]) + 1 if previous else 1
                created = float(previous["created_at"]) if previous else now
                conn.execute(
                    """INSERT INTO desktop_app VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(app_id) DO UPDATE SET
                         executable=excluded.executable,
                         package_family=excluded.package_family,
                         app_user_model_id=excluded.app_user_model_id,
                         display_name=excluded.display_name,
                         processes_json=excluded.processes_json,
                         installed=excluded.installed, running=excluded.running,
                         launch_identity_json=excluded.launch_identity_json,
                         revision=excluded.revision, updated_at=excluded.updated_at""",
                    (item.app_id, item.executable, item.package_family,
                     item.app_user_model_id, item.display_name,
                     canonical_json([p.to_dict() for p in item.processes]),
                     None if item.installed is None else int(item.installed),
                     int(item.running), canonical_json(item.launch_identity),
                     revision, created, now),
                )
                self._event(conn, entity_kind="app", entity_id=item.app_id,
                            event_type="app.catalogued", revision=revision, at=now)

            prior_apps = conn.execute(
                "SELECT app_id, revision FROM desktop_app WHERE running=1"
            ).fetchall()
            for prior in prior_apps:
                if prior["app_id"] in seen_apps:
                    continue
                revision = int(prior["revision"]) + 1
                conn.execute(
                    """UPDATE desktop_app SET running=0, processes_json='[]',
                         revision=?, updated_at=? WHERE app_id=?""",
                    (revision, now, prior["app_id"]),
                )
                self._event(
                    conn, entity_kind="app", entity_id=prior["app_id"],
                    event_type="app.stopped", revision=revision, at=now,
                )

            for item in window_items:
                previous = conn.execute(
                    "SELECT * FROM desktop_window WHERE window_id=?", (item.window_id,),
                ).fetchone()
                if previous and (
                    int(previous["hwnd"]) != item.hwnd
                    or int(previous["pid"]) != item.pid
                    or float(previous["pid_started_at"]) != item.pid_started_at
                ):
                    raise DesktopConflict(
                        f"window id {item.window_id} changed strong identity")
                revision = int(previous["revision"]) + 1 if previous else 1
                generation = int(previous["generation"]) if previous else max(1, item.generation)
                created = float(previous["created_at"]) if previous else now
                recovery: dict[str, Any] = {}
                if previous and previous["backend_instance_id"] != backend_instance_id:
                    recovery = {
                        "status": "rebound_live",
                        "previous_backend_instance_id": previous["backend_instance_id"],
                        "reconciled_at": now,
                    }
                conn.execute(
                    """INSERT INTO desktop_window VALUES(
                         ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                       ) ON CONFLICT(window_id) DO UPDATE SET
                         app_id=excluded.app_id, executable=excluded.executable,
                         package_family=excluded.package_family,
                         app_user_model_id=excluded.app_user_model_id,
                         class_name=excluded.class_name, title=excluded.title,
                         owner_hwnd=excluded.owner_hwnd,
                         root_owner_hwnd=excluded.root_owner_hwnd,
                         monitor=excluded.monitor,
                         virtual_desktop=excluded.virtual_desktop,
                         dpi=excluded.dpi, bounds_json=excluded.bounds_json,
                         visible=excluded.visible, minimized=excluded.minimized,
                         cloaked=excluded.cloaked, occluded=excluded.occluded,
                         foreground=excluded.foreground,
                         backend_instance_id=excluded.backend_instance_id,
                         recovery_json=excluded.recovery_json,
                         revision=excluded.revision, updated_at=excluded.updated_at,
                         missing_at=0""",
                    (item.window_id, item.app_id, item.hwnd, item.pid,
                     item.pid_started_at, item.executable, item.package_family,
                     item.app_user_model_id, item.class_name, item.title,
                     item.owner_hwnd, item.root_owner_hwnd, item.monitor,
                     item.virtual_desktop, item.dpi,
                     canonical_json(list(item.bounds) if item.bounds else []),
                     int(item.visible), int(item.minimized),
                     None if item.cloaked is None else int(item.cloaked),
                     None if item.occluded is None else int(item.occluded),
                     int(item.foreground), generation, backend_instance_id,
                     canonical_json(recovery), revision, created, now, 0.0),
                )
                self._event(
                    conn, entity_kind="window", entity_id=item.window_id,
                    event_type="window.rebound" if recovery else "window.catalogued",
                    revision=revision, payload={"generation": generation}, at=now,
                )

            prior_rows = conn.execute(
                "SELECT window_id, revision, generation FROM desktop_window WHERE missing_at=0"
            ).fetchall()
            for prior in prior_rows:
                if prior["window_id"] in seen_windows:
                    continue
                revision = int(prior["revision"]) + 1
                generation = int(prior["generation"]) + 1
                conn.execute(
                    """UPDATE desktop_window SET missing_at=?, generation=?,
                         revision=?, updated_at=?, recovery_json=? WHERE window_id=?""",
                    (now, generation, revision, now,
                     canonical_json({"status": "not_in_latest_catalog", "at": now}),
                     prior["window_id"]),
                )
                self._event(
                    conn, entity_kind="window", entity_id=prior["window_id"],
                    event_type="window.missing", revision=revision,
                    payload={"generation": generation}, at=now,
                )
        return self.list_apps(), self.list_windows(include_missing=False)

    def get_app(self, app_id: str) -> AppRecord:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM desktop_app WHERE app_id=?", (app_id,)).fetchone()
        if not row:
            raise DesktopNotFound(f"desktop app not found: {app_id}")
        return self._app(row)

    def list_apps(self, *, running: bool | None = None) -> list[AppRecord]:
        sql = "SELECT * FROM desktop_app"
        params: tuple[Any, ...] = ()
        if running is not None:
            sql += " WHERE running=?"
            params = (int(running),)
        sql += " ORDER BY display_name COLLATE NOCASE, app_id"
        with self._read() as conn:
            return [self._app(row) for row in conn.execute(sql, params).fetchall()]

    def get_window(self, window_id: str) -> WindowRecord:
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM desktop_window WHERE window_id=?", (window_id,)
            ).fetchone()
        if not row:
            raise DesktopNotFound(f"desktop window not found: {window_id}")
        return self._window(row)

    def list_windows(
        self, *, app_id: str = "", include_missing: bool = False,
    ) -> list[WindowRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if app_id:
            clauses.append("app_id=?")
            params.append(app_id)
        if not include_missing:
            clauses.append("missing_at=0")
        sql = "SELECT * FROM desktop_window"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY foreground DESC, updated_at DESC, window_id"
        with self._read() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._window(row) for row in rows]

    def record_capture(self, capture: DesktopCapture) -> DesktopCapture:
        with self._write() as conn:
            conn.execute(
                """INSERT INTO desktop_capture VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (capture.capture_id, capture.window_id, capture.window_generation,
                 capture.artifact_ref, capture.bytes, capture.width, capture.height,
                 capture.provenance, int(capture.occlusion_independent),
                 int(capture.minimized), int(capture.stale),
                 canonical_json(capture.coordinate_transform), capture.created_at),
            )
            self._event(conn, entity_kind="capture", entity_id=capture.capture_id,
                        event_type="capture.recorded", revision=1,
                        payload={"window_id": capture.window_id}, at=capture.created_at)
        return capture

    def get_capture(self, capture_id: str) -> DesktopCapture:
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM desktop_capture WHERE capture_id=?", (capture_id,)
            ).fetchone()
        if not row:
            raise DesktopNotFound(f"desktop capture not found: {capture_id}")
        return self._capture(row)

    def record_observation(self, observation: DesktopObservation) -> DesktopObservation:
        with self._write() as conn:
            conn.execute(
                """INSERT INTO desktop_observation VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (observation.observation_id, observation.window_id,
                 observation.window_generation, observation.mode,
                 canonical_json([item.to_dict() for item in observation.elements]),
                 observation.capture_id, observation.image_ref,
                 canonical_json(observation.capture), observation.completeness,
                 observation.uia_generation, observation.fingerprint,
                 observation.created_at, canonical_json(observation.scope.to_dict())),
            )
            self._event(
                conn, entity_kind="observation", entity_id=observation.observation_id,
                event_type="observation.recorded", revision=1,
                payload={"window_id": observation.window_id,
                         "fingerprint": observation.fingerprint},
                at=observation.created_at,
            )
            cursor = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        return replace(observation, event_cursor=cursor)

    def get_observation(self, observation_id: str, *, scope: Any = None) -> DesktopObservation:
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM desktop_observation WHERE observation_id=?",
                (observation_id,),
            ).fetchone()
            event = conn.execute(
                "SELECT sequence FROM desktop_event WHERE entity_kind='observation' "
                "AND entity_id=? ORDER BY sequence DESC LIMIT 1", (observation_id,),
            ).fetchone()
        if not row:
            raise DesktopNotFound(f"desktop observation not found: {observation_id}")
        observation = self._observation(row, event_cursor=int(event[0]) if event else 0)
        if scope is not None:
            observation.require_scope(scope)
        return observation

    def latest_observation(self, window_id: str) -> DesktopObservation | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM desktop_observation WHERE window_id=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1", (window_id,),
            ).fetchone()
        return self._observation(row) if row else None

    def prepare_operation(
        self, *, window: WindowRecord, scope: WorkScope | Mapping[str, Any] | None,
        idempotency_key: str = "", action: str, delivery: str,
        target: Mapping[str, Any] | None = None,
        arguments: Mapping[str, Any] | None = None,
        expectation: Mapping[str, Any] | None = None,
        before_observation_id: str = "", backend_instance_id: str,
    ) -> DesktopOperation:
        if delivery not in DELIVERY_MODES:
            raise DesktopValidationError(
                f"invalid desktop delivery mode: {delivery}")
        if not str(action or "").strip():
            raise DesktopValidationError("desktop action is required")
        resolved_scope = coerce_scope(scope)
        target_value = dict(target or {})
        digest_target = {
            key: value for key, value in target_value.items()
            if key not in {"element_generation", "resolved_fingerprint", "observation_id"}
        }
        arguments_value = dict(arguments or {})
        expectation_value = dict(expectation or {})
        request_digest = request_fingerprint(f"desktop.{action}", {
            "window_id": window.window_id,
            "window_generation": window.generation,
            "scope": resolved_scope.to_dict(),
            "delivery": delivery,
            "target": digest_target,
            "arguments": arguments_value,
            "expectation": expectation_value,
        })
        now = time.time()
        with self._write() as conn:
            if idempotency_key:
                existing = conn.execute(
                    "SELECT * FROM desktop_operation WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if existing:
                    if existing["request_digest"] != request_digest:
                        raise DesktopConflict(
                            "idempotency key already names a different desktop request")
                    return self._operation(existing)
            operation_id = new_desktop_id("dop")
            conn.execute(
                """INSERT INTO desktop_operation VALUES(
                     ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                   )""",
                (operation_id, window.window_id, window.generation,
                 canonical_json(resolved_scope.to_dict()), idempotency_key,
                 request_digest, action, delivery, canonical_json(target_value),
                 canonical_json(arguments_value), canonical_json(expectation_value),
                 "prepared", before_observation_id, "", "{}", "{}", "",
                 backend_instance_id, 1, now, 0.0, 0.0, 0.0, now),
            )
            self._event(
                conn, entity_kind="operation", entity_id=operation_id,
                event_type="operation.prepared", revision=1,
                payload={"delivery_possible": False, "window_id": window.window_id}, at=now,
            )
            row = conn.execute(
                "SELECT * FROM desktop_operation WHERE operation_id=?", (operation_id,),
            ).fetchone()
        return self._operation(row)

    def get_operation(
        self, operation_id: str, *,
        scope: WorkScope | Mapping[str, Any] | None = None,
    ) -> DesktopOperation:
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM desktop_operation WHERE operation_id=?", (operation_id,),
            ).fetchone()
        if not row:
            raise DesktopNotFound(f"desktop operation not found: {operation_id}")
        record = self._operation(row)
        self._assert_scope(record.scope, scope)
        return record

    def transition_operation(
        self, operation_id: str, *, expected: str | Iterable[str], state: str,
        dispatch: Mapping[str, Any] | None = None,
        after_observation_id: str | None = None,
        evidence: Mapping[str, Any] | None = None, error: str | None = None,
    ) -> DesktopOperation:
        if state not in OPERATION_STATES:
            raise DesktopValidationError(f"invalid desktop operation state: {state}")
        allowed = {expected} if isinstance(expected, str) else set(expected)
        now = time.time()
        with self._write() as conn:
            row = conn.execute(
                "SELECT * FROM desktop_operation WHERE operation_id=?", (operation_id,),
            ).fetchone()
            if not row:
                raise DesktopNotFound(f"desktop operation not found: {operation_id}")
            if row["state"] not in allowed:
                raise DesktopConflict(
                    f"operation {operation_id} is {row['state']}, expected {sorted(allowed)}")
            legal = {
                "prepared": {"dispatched", "failed"},
                "dispatched": {"observed", "no_effect", "failed", "unknown_effect"},
                "observed": {"verified", "no_effect", "failed", "unknown_effect"},
            }
            if state not in legal.get(row["state"], set()):
                raise DesktopConflict(
                    f"illegal desktop operation transition: {row['state']} -> {state}")
            revision = int(row["revision"]) + 1
            dispatched_at = float(row["dispatched_at"])
            observed_at = float(row["observed_at"])
            completed_at = float(row["completed_at"])
            if state == "dispatched":
                dispatched_at = now
            if state == "observed":
                observed_at = now
            if state in {"verified", "no_effect", "failed", "unknown_effect"}:
                completed_at = now
            dispatch_value = (
                dict(dispatch) if dispatch is not None
                else _object(row["dispatch_json"], "dispatch"))
            evidence_value = (
                dict(evidence) if evidence is not None
                else _object(row["evidence_json"], "evidence"))
            after_value = (
                str(after_observation_id) if after_observation_id is not None
                else row["after_observation_id"])
            error_value = str(error) if error is not None else row["error"]
            conn.execute(
                """UPDATE desktop_operation SET state=?, dispatch_json=?,
                     after_observation_id=?, evidence_json=?, error=?, revision=?,
                     dispatched_at=?, observed_at=?, completed_at=?, updated_at=?
                   WHERE operation_id=? AND revision=?""",
                (state, canonical_json(dispatch_value), after_value,
                 canonical_json(evidence_value), error_value, revision,
                 dispatched_at, observed_at, completed_at, now, operation_id,
                 int(row["revision"])),
            )
            self._event(
                conn, entity_kind="operation", entity_id=operation_id,
                event_type=f"operation.{state}", revision=revision,
                payload={"previous_state": row["state"],
                         "delivery_possible": state != "failed" or bool(dispatched_at)},
                at=now,
            )
            updated = conn.execute(
                "SELECT * FROM desktop_operation WHERE operation_id=?", (operation_id,),
            ).fetchone()
        return self._operation(updated)

    def list_operations(
        self, *, window_id: str = "", states: Iterable[str] | None = None,
        scope: WorkScope | Mapping[str, Any] | None = None,
        limit: int = 100,
    ) -> list[DesktopOperation]:
        clauses: list[str] = []
        params: list[Any] = []
        if window_id:
            clauses.append("window_id=?")
            params.append(window_id)
        state_values = tuple(states or ())
        if state_values:
            clauses.append("state IN (" + ",".join("?" for _ in state_values) + ")")
            params.extend(state_values)
        self._append_scope_filter(clauses, params, "scope_json", scope)
        sql = "SELECT * FROM desktop_operation"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self._read() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._operation(row) for row in rows]

    def reconcile_backend(self, backend_instance_id: str) -> dict[str, int]:
        """Disclose interrupted delivery without replaying any desktop input."""
        now = time.time()
        counts = {"failed_before_dispatch": 0, "unknown_effect": 0,
                  "windows_need_rebind": 0}
        with self._write() as conn:
            rows = conn.execute(
                """SELECT * FROM desktop_operation
                   WHERE backend_instance_id<>? AND state IN ('prepared','dispatched','observed')""",
                (backend_instance_id,),
            ).fetchall()
            for row in rows:
                revision = int(row["revision"]) + 1
                if row["state"] == "prepared":
                    state = "failed"
                    error = "backend restarted before dispatch; input was not delivered"
                    evidence = {"recovery": "not_dispatched", "automatic_retry": False}
                    counts["failed_before_dispatch"] += 1
                else:
                    state = "unknown_effect"
                    error = "backend restarted after possible input delivery; effect is unknown"
                    evidence = {
                        "recovery": "possible_delivery",
                        "previous_state": row["state"],
                        "automatic_retry": False,
                    }
                    counts["unknown_effect"] += 1
                conn.execute(
                    """UPDATE desktop_operation SET state=?, evidence_json=?, error=?,
                         revision=?, completed_at=?, updated_at=? WHERE operation_id=?""",
                    (state, canonical_json(evidence), error, revision, now, now,
                     row["operation_id"]),
                )
                self._event(
                    conn, entity_kind="operation", entity_id=row["operation_id"],
                    event_type=f"operation.{state}", revision=revision,
                    payload=evidence, at=now,
                )
            windows = conn.execute(
                """SELECT window_id, revision FROM desktop_window
                   WHERE missing_at=0 AND backend_instance_id<>?""",
                (backend_instance_id,),
            ).fetchall()
            for row in windows:
                revision = int(row["revision"]) + 1
                recovery = {
                    "status": "needs_live_rebind",
                    "reason": "backend_restarted",
                    "automatic_input_allowed": False,
                    "at": now,
                }
                conn.execute(
                    """UPDATE desktop_window SET recovery_json=?, revision=?,
                         updated_at=? WHERE window_id=?""",
                    (canonical_json(recovery), revision, now, row["window_id"]),
                )
                self._event(
                    conn, entity_kind="window", entity_id=row["window_id"],
                    event_type="window.rebind_required", revision=revision,
                    payload=recovery, at=now,
                )
                counts["windows_need_rebind"] += 1
        return counts

    def events(
        self, *, after: int = 0, limit: int = 200,
        entity_kind: str = "", entity_id: str = "",
        scope: WorkScope | Mapping[str, Any] | None = None,
    ) -> list[DesktopEvent]:
        clauses = ["sequence>?"]
        params: list[Any] = [max(0, int(after))]
        if entity_kind:
            clauses.append("entity_kind=?")
            params.append(entity_kind)
        if entity_id:
            clauses.append("entity_id=?")
            params.append(entity_id)
        self._append_scope_filter(
            clauses, params, "desktop_operation.scope_json", scope,
        )
        params.append(max(1, min(int(limit), 1000)))
        with self._read() as conn:
            rows = conn.execute(
                "SELECT desktop_event.* FROM desktop_event "
                "LEFT JOIN desktop_operation ON "
                "desktop_event.entity_kind='operation' AND "
                "desktop_operation.operation_id=desktop_event.entity_id WHERE "
                + " AND ".join(clauses)
                + " ORDER BY sequence LIMIT ?", tuple(params),
            ).fetchall()
        return [DesktopEvent(
            sequence=int(row["sequence"]), event_id=row["event_id"],
            entity_kind=row["entity_kind"], entity_id=row["entity_id"],
            event_type=row["event_type"], revision=int(row["revision"]),
            payload=_object(row["payload_json"], "event.payload"),
            created_at=float(row["created_at"]),
        ) for row in rows]

    def event_cursor(self) -> int:
        with self._read() as conn:
            row = conn.execute("SELECT COALESCE(MAX(sequence),0) FROM desktop_event").fetchone()
        return int(row[0])


__all__ = [
    "DesktopFabricRepository", "default_desktop_fabric_path", "new_desktop_id",
]
