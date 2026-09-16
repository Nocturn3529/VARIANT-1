"""Process-owned registry for durable, chat-scoped runtime leases."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from concurrent.futures import Future
from dataclasses import dataclass, field
import threading
import time
import uuid
from typing import Any, Awaitable, Callable, Iterable

from .models import (
    BudgetExhausted,
    ChatRuntimeRecord,
    InputTicket,
    RuntimeDeleted,
    RuntimeIdentity,
    TICKET_TERMINAL_STATES,
)
from .repository import SessionRuntimeRepository, chat_id_value


@dataclass
class _Attachment:
    attachment_id: str
    chat_id: str
    session: Any
    transport: Any = None


@dataclass
class _RunAdmission:
    admission_id: str
    chat_id: str
    attachment_id: str
    reserved_at: float
    task: asyncio.Task | None = None
    run_id: str = ""
    thread_id: str = ""
    background: bool = False
    accepting_inputs: bool = True
    budget_baseline: dict[str, float] = field(default_factory=dict)
    owner_session: Any = None
    owner_transport: Any = None
    pause_requested: bool = False
    paused: bool = False
    pause_revision: int = 0
    resume_event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _LiveRuntime:
    chat_id: str
    writer_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    attachment_ids: set[str] = field(default_factory=set)
    admission: _RunAdmission | None = None
    kernel_lease: Any = None
    settings_change_id: str = ""
    kernel_retirement: Future[None] | None = None
    queue_park_reason: str = ""


class SessionRuntimeRegistry:
    """Owns all live objects while SQLite owns durable runtime facts."""

    def __init__(
        self,
        repository: SessionRuntimeRepository,
        *,
        identity_factory: Callable[[str, bool], RuntimeIdentity] | None = None,
        snapshot_store: Any = None,
    ) -> None:
        self.repository = repository
        self.identity_factory = identity_factory or (
            lambda _chat_id, _is_new: RuntimeIdentity()
        )
        self._guard = threading.RLock()
        self._live: dict[str, _LiveRuntime] = {}
        self._attachments: dict[str, _Attachment] = {}
        self._reservations: dict[str, _RunAdmission] = {}
        self._chat_cleanup: list[Callable[[str], Any]] = []
        self._chat_tombstone_cleanup: list[Callable[[str], Any]] = []
        self._snapshot_store = snapshot_store

    @property
    def snapshot_store(self):
        with self._guard:
            if self._snapshot_store is None:
                from agent_engine.sqlite_snapshot_store import SQLiteRunSnapshotStore
                self._snapshot_store = SQLiteRunSnapshotStore()
            return self._snapshot_store

    async def _delete_thread_snapshots(self, chat_id: str) -> list[str]:
        thread_ids = self.repository.thread_refs(chat_id)
        if thread_ids:
            store = self.snapshot_store
            for thread_id in thread_ids:
                await store.delete_thread(thread_id)
            self.repository.mark_threads_deleted(chat_id)
        return thread_ids

    def register_chat_cleanup(self, callback: Callable[[str], Any]) -> None:
        if not callable(callback):
            raise TypeError("chat cleanup callback must be callable")
        with self._guard:
            if callback not in self._chat_cleanup:
                self._chat_cleanup.append(callback)

    def register_chat_tombstone_cleanup(
        self, callback: Callable[[str], Any]
    ) -> None:
        if not callable(callback):
            raise TypeError("chat tombstone cleanup must be callable")
        with self._guard:
            if callback not in self._chat_tombstone_cleanup:
                self._chat_tombstone_cleanup.append(callback)

    _chat_id = staticmethod(chat_id_value)

    @staticmethod
    def _emit(event: str, **fields: Any) -> None:
        try:
            from observability.trace_events import record_trace_event

            record_trace_event(event, **fields)
        except Exception:
            pass

    def ensure_runtime(self, chat_id: str, *, is_new: bool = False) -> ChatRuntimeRecord:
        clean = self._chat_id(chat_id)
        record = self.repository.get_runtime(clean)
        created = record is None
        if record is None:
            record = self.repository.ensure_runtime(
                clean,
                self.identity_factory(clean, bool(is_new)),
            )
        if record.deleted:
            raise RuntimeDeleted(f"chat runtime is {record.lifecycle_state}: {clean}")
        with self._guard:
            self._live.setdefault(clean, _LiveRuntime(chat_id=clean))
        if created:
            self._emit(
                "runtime:profile_assigned",
                status="ok",
                chat_id=clean,
                action_surface=record.identity.action_surface,
                graph_revision=record.identity.graph_revision,
                trust_profile=record.identity.trust_profile,
            )
        return record

    def ensure_worker_runtime(
        self,
        runtime_id: str,
        *,
        source: str,
        identity: RuntimeIdentity,
    ) -> ChatRuntimeRecord:
        """Persist an explicit profile for a durable headless run source.

        Worker identities share the same runtime/kernel ownership substrate as
        chats, but are not transcript-backed chat rows. Their creation marker
        keeps startup reconciliation from treating them as missing chats.
        """
        clean = self._chat_id(runtime_id)
        worker_source = str(source or "").strip().lower()
        if worker_source != "automation":
            raise ValueError(f"unsupported durable worker source: {worker_source!r}")
        marker = f"worker:{worker_source}"
        record = self.repository.get_runtime(clean)
        created = record is None
        if record is None:
            record = self.repository.ensure_runtime(
                clean,
                identity,
                creation_saga_state=marker,
            )
        if record.deleted:
            raise RuntimeDeleted(
                f"worker runtime is {record.lifecycle_state}: {clean}"
            )
        if str(record.creation_saga_state or "") != marker:
            raise RuntimeError(
                f"runtime id {clean!r} is not owned by {marker!r}"
            )
        with self._guard:
            self._live.setdefault(clean, _LiveRuntime(chat_id=clean))
        if created:
            self._emit(
                "runtime:worker_profile_assigned",
                status="ok",
                chat_id=clean,
                source=worker_source,
                action_surface=record.identity.action_surface,
                graph_revision=record.identity.graph_revision,
                trust_profile=record.identity.trust_profile,
            )
        return record

    def link_worker_thread(
        self,
        runtime_id: str,
        thread_id: str,
        *,
        source: str,
    ) -> None:
        clean = self._chat_id(runtime_id)
        record = self.repository.get_runtime(clean)
        marker = f"worker:{str(source or '').strip().lower()}"
        if record is None or str(record.creation_saga_state or "") != marker:
            raise RuntimeError(f"worker runtime is not admitted: {clean}")
        self.repository.link_thread(clean, str(thread_id or clean), source=source)

    def runtime(self, chat_id: str) -> ChatRuntimeRecord | None:
        return self.repository.get_runtime(self._chat_id(chat_id))

    def set_mutation_write_enabled(
        self,
        chat_id: str,
        enabled: bool,
        actor: str,
        expected_revision: int | None = None,
    ) -> ChatRuntimeRecord:
        """Set per-chat mutation authority only between admitted runs."""

        clean = self._chat_id(chat_id)
        self.ensure_runtime(clean)
        with self._guard:
            live = self._live.setdefault(clean, _LiveRuntime(clean))
            admission = live.admission
            if live.settings_change_id:
                raise RuntimeError("cannot change mutation authority while session configuration is pending")
            if admission is not None:
                raise RuntimeError(
                    "cannot change mutation write authority during an active run"
                )
            # Hold the same guard used by try_reserve_run across the durable CAS.
            # A run therefore cannot become admitted between the busy check and
            # the authority commit.
            record = self.repository.mutation_authority_cas(
                clean,
                enabled,
                actor=actor,
                expected_revision=expected_revision,
            )
        self._emit(
            "runtime:mutation_authority",
            status="enabled" if record.mutation_write_enabled else "disabled",
            chat_id=clean,
            revision=record.mutation_authority_revision,
            actor=record.mutation_authority_actor,
        )
        return record

    def advance_kernel_generation(self, chat_id: str) -> int:
        clean = self._chat_id(chat_id)
        record = self.repository.advance_kernel_generation(clean)
        self._emit(
            "runtime:kernel_generation",
            status="ok",
            chat_id=clean,
            kernel_generation=record.kernel_generation,
        )
        return int(record.kernel_generation)

    def install_kernel_lease(self, chat_id: str, lease: Any) -> None:
        clean = self._chat_id(chat_id)
        self.ensure_runtime(clean)
        with self._guard:
            live = self._live.setdefault(clean, _LiveRuntime(clean))
            if live.kernel_lease is not None and live.kernel_lease is not lease:
                old_state = str(getattr(live.kernel_lease, "state", ""))
                if old_state not in {"absent", "unhealthy"}:
                    raise RuntimeError("chat already owns a live kernel lease")
            live.kernel_lease = lease

    def kernel_lease(self, chat_id: str) -> Any:
        clean = self._chat_id(chat_id)
        with self._guard:
            live = self._live.get(clean)
            return live.kernel_lease if live is not None else None

    def attached_session(self, chat_id: str) -> Any:
        clean = self._chat_id(chat_id)
        with self._guard:
            live = self._live.get(clean)
            attachment_ids = sorted(live.attachment_ids) if live is not None else []
            for attachment_id in attachment_ids:
                attachment = self._attachments.get(attachment_id)
                if attachment is not None:
                    return attachment.session
        return None

    def attached_transports(self, chat_id: str) -> list[Any]:
        """Return live transports attached to one durable chat."""
        clean = self._chat_id(chat_id)
        with self._guard:
            live = self._live.get(clean)
            attachment_ids = sorted(live.attachment_ids) if live is not None else []
            transports = [
                self._attachments[attachment_id].transport
                for attachment_id in attachment_ids
                if attachment_id in self._attachments
                and self._attachments[attachment_id].transport is not None
            ]
        unique: list[Any] = []
        for transport in transports:
            if not any(item is transport for item in unique):
                unique.append(transport)
        return unique

    def remove_kernel_lease(self, chat_id: str, lease: Any) -> bool:
        clean = self._chat_id(chat_id)
        with self._guard:
            live = self._live.get(clean)
            if live is None or live.kernel_lease is not lease:
                return False
            live.kernel_lease = None
            return True

    def assign_identity(
        self,
        chat_id: str,
        identity: RuntimeIdentity,
        *,
        expected_action_surface: str = "trusted-local.v1",
    ) -> ChatRuntimeRecord:
        """Persist an explicit profile only while no run/kernel owns the chat."""
        clean = self._chat_id(chat_id)
        self.ensure_runtime(clean)
        with self._guard:
            live = self._live.setdefault(clean, _LiveRuntime(clean))
            if live.admission is not None or live.kernel_lease is not None or live.settings_change_id:
                raise RuntimeError("cannot change profile while the chat has live work")
        if self.repository.thread_refs(clean):
            raise RuntimeError("cannot change profile after a graph checkpoint exists")
        record = self.repository.assign_identity(
            clean,
            identity,
            expected_action_surface=expected_action_surface,
        )
        self._emit(
            "runtime:profile_assigned",
            status="ok",
            chat_id=clean,
            action_surface=record.identity.action_surface,
            graph_revision=record.identity.graph_revision,
            explicit=True,
        )
        return record

    def snapshot(self, chat_id: str) -> dict[str, Any]:
        clean = self._chat_id(chat_id)
        record = self.repository.get_runtime(clean)
        if record is None:
            return {}
        with self._guard:
            live = self._live.get(clean)
            admission = live.admission if live is not None else None
            attachments = len(live.attachment_ids) if live is not None else 0
        return {
            **record.to_dict(),
            "attachments": attachments,
            "busy": bool(admission),
            "active_run_id": admission.run_id if admission else "",
            "active_admission_id": admission.admission_id if admission else "",
            "pause_state": self._pause_state(admission),
            "pause_revision": admission.pause_revision if admission else 0,
            "configuration_pending": bool(live and live.settings_change_id),
            "queued_inputs": len(self.repository.list_tickets(clean, states=("queued",))),
        }

    def reconcile_runtime_owners(
        self,
        active_runtime_chat_ids: Iterable[str],
        purge_ids: Iterable[str] = (),
    ) -> dict[str, int]:
        """Reconcile branch-owned runtimes without inferring deletion.

        ``active_runtime_chat_ids`` is the complete owner projection selected
        by the caller (for example the active conversation branches).  Missing
        owners are retained: absence from a projection is not proof that native
        snapshots, kernels, catalog state, artifacts, or tickets may be
        destroyed.  Only IDs explicitly supplied in ``purge_ids`` enter the
        existing deletion saga.

        The method intentionally does not execute the purge.  Startup recovery
        or an explicit retention worker performs the destructive steps after
        the caller has persisted that decision.
        """

        active = {
            self._chat_id(chat_id)
            for chat_id in active_runtime_chat_ids
            if str(chat_id or "").strip()
        }
        purge = {
            self._chat_id(chat_id)
            for chat_id in purge_ids
            if str(chat_id or "").strip()
        }
        overlap = active.intersection(purge)
        if overlap:
            raise ValueError(
                "runtime owner cannot be active and explicitly purged: "
                + ", ".join(sorted(overlap))
            )

        created = 0
        for chat_id in sorted(active):
            if self.repository.get_runtime(chat_id) is None:
                created += 1
            self.ensure_runtime(chat_id, is_new=False)

        retained = 0
        marked_for_purge = 0
        for record in self.repository.list_runtimes():
            marker = str(record.creation_saga_state or "")
            if marker.startswith(("child:", "worker:")):
                continue
            if record.chat_id in active:
                continue
            if record.chat_id in purge and record.lifecycle_state == "active":
                self.repository.set_lifecycle(
                    record.chat_id,
                    "deleting",
                    deletion_saga_state="explicit_owner_purge",
                )
                marked_for_purge += 1
                continue
            if record.lifecycle_state == "active":
                retained += 1

        summary = {
            "active_owners": len(active),
            "created": created,
            "retained_unowned": retained,
            "marked_for_purge": marked_for_purge,
        }
        self._emit(
            "runtime:owners_reconciled",
            status="ok",
            **summary,
        )
        return summary

    async def startup_reconcile(
        self,
        sessions: Any,
    ) -> dict[str, int]:
        session_rows = list(sessions.list_sessions() or ())
        chat_ids = [
            str(row.get("id") or "")
            for row in session_rows if isinstance(row, dict)
        ]
        self.reconcile_runtime_owners(chat_ids)
        recovered = 0
        completed = 0
        deletions_deferred = 0
        for record in self.repository.list_runtimes(include_deleted=False):
            if record.lifecycle_state == "deleting":
                # Conversation is the owner. Startup never turns an old or
                # ambiguous runtime marker into authority to purge resources;
                # an explicit retention operation must resume that cleanup.
                deletions_deferred += 1
                continue
            if record.lifecycle_state != "active":
                continue
            tickets = self.repository.list_tickets(
                record.chat_id,
                states=(
                    "queued", "selected", "preparing",
                    "transcript_committing", "running",
                ),
            )
            for ticket in tickets:
                has_proof = False
                checker = getattr(sessions, "has_message_ticket", None)
                if callable(checker):
                    has_proof = bool(checker(record.chat_id, ticket.ticket_id))
                if has_proof:
                    self.repository.transition_ticket(
                        ticket.ticket_id,
                        "completed",
                        expected=(ticket.state,),
                        proof={"chat_id": record.chat_id, "ticket_id": ticket.ticket_id},
                    )
                    completed += 1
                else:
                    self.repository.transition_ticket(
                        ticket.ticket_id,
                        "resume_queued",
                        expected=(ticket.state,),
                        error="held_for_explicit_resume_after_process_restart",
                    )
                    recovered += 1
        self._emit(
            "runtime:startup_reconciled",
            status="ok",
            chats=len(chat_ids),
            tickets_requeued=recovered,
            tickets_completed=completed,
            deletions_deferred=deletions_deferred,
        )
        return {
            "chats": len(chat_ids),
            "tickets_requeued": recovered,
            "tickets_completed": completed,
            "deletions_deferred": deletions_deferred,
        }

    def attach(
        self,
        chat_id: str,
        attachment_id: str,
        session: Any,
        transport: Any = None,
    ) -> str:
        clean = self._chat_id(chat_id)
        self.ensure_runtime(clean)
        aid = str(attachment_id or "attach_" + uuid.uuid4().hex)
        with self._guard:
            previous = self._attachments.get(aid)
            if previous is not None and previous.chat_id != clean:
                old = self._live.get(previous.chat_id)
                if old is not None:
                    old.attachment_ids.discard(aid)
            attachment = _Attachment(aid, clean, session, transport)
            self._attachments[aid] = attachment
            self._live.setdefault(clean, _LiveRuntime(clean)).attachment_ids.add(aid)
        try:
            session.attachment_id = aid
            session.viewed_session_id = clean
        except Exception:
            pass
        self._emit("runtime:attachment_joined", status="ok", chat_id=clean, attachment_id=aid)
        return aid

    def move_attachment(self, attachment_id: str, chat_id: str) -> str:
        aid = str(attachment_id or "")
        with self._guard:
            attachment = self._attachments.get(aid)
        if attachment is None:
            raise LookupError(f"unknown attachment: {aid}")
        return self.attach(chat_id, aid, attachment.session, attachment.transport)

    def detach(
        self,
        attachment_id: str,
        *,
        cancel_unobserved_foreground: bool = True,
    ) -> list[asyncio.Task]:
        aid = str(attachment_id or "")
        cancelled: list[asyncio.Task] = []
        with self._guard:
            attachment = self._attachments.pop(aid, None)
            if attachment is None:
                return cancelled
            live = self._live.get(attachment.chat_id)
            if live is not None:
                live.attachment_ids.discard(aid)
            for admission in list(self._reservations.values()):
                if (
                    admission.attachment_id == aid
                    and not admission.background
                    and admission.task is not None
                    and not admission.task.done()
                ):
                    # The session remains the in-memory turn owner until its
                    # durable terminal commit. A detached renderer transport
                    # is no longer a useful delivery target; explicit Stop can
                    # use any later requesting transport for this chat.
                    admission.owner_transport = None
                    # A durable chat can have another Deck attached. Keep the
                    # admitted run alive when an observer remains; model/tool
                    # streaming falls through to those current transports.
                    admitted_live = self._live.get(admission.chat_id)
                    remaining = bool(
                        admitted_live is not None and admitted_live.attachment_ids
                    )
                    if not remaining and cancel_unobserved_foreground:
                        admission.accepting_inputs = False
                        admission.task.cancel()
                        cancelled.append(admission.task)
        self._emit(
            "runtime:attachment_left",
            status="cancelled" if cancelled else "ok",
            chat_id=attachment.chat_id,
            attachment_id=aid,
        )
        return cancelled

    def attachment_count(self, chat_id: str) -> int:
        clean = self._chat_id(chat_id)
        with self._guard:
            live = self._live.get(clean)
            return len(live.attachment_ids) if live is not None else 0

    def writer_lock(self, chat_id: str) -> asyncio.Lock:
        clean = self._chat_id(chat_id)
        self.ensure_runtime(clean)
        with self._guard:
            return self._live.setdefault(clean, _LiveRuntime(clean)).writer_lock

    def configuration_pending(self, chat_id: str) -> bool:
        with self._guard:
            live = self._live.get(self._chat_id(chat_id))
            return bool(live and live.settings_change_id)

    @contextmanager
    def change_settings(self, chat_id: str):
        """Fence idle-chat configuration against admission across awaited work."""
        clean = self._chat_id(chat_id)
        self.ensure_runtime(clean)
        with self._guard:
            live = self._live.setdefault(clean, _LiveRuntime(clean))
            if live.admission is not None:
                raise RuntimeError("session_has_active_run")
            if live.settings_change_id:
                raise RuntimeError("session_configuration_pending")
            token = uuid.uuid4().hex
            live.settings_change_id = token
        try:
            yield
        finally:
            with self._guard:
                if live.settings_change_id == token:
                    live.settings_change_id = ""

    @staticmethod
    def _budget_allows(record: ChatRuntimeRecord) -> bool:
        if record.continuation_state == "paused_budget_exhausted":
            return False
        for key, limit in record.budget_limits.items():
            if float(limit or 0) > 0 and float(record.budget_used.get(key) or 0) >= float(limit):
                return False
        return True

    def automatic_kernel_eviction_blocked(self, chat_id: str) -> bool:
        with self._guard:
            live = self._live.get(self._chat_id(chat_id))
            return bool(live and (live.admission or live.settings_change_id))

    @contextmanager
    def claim_kernel_retirement(self, chat_id: str):
        """Linearize automatic teardown against admission without holding a lock across await."""
        with self._guard:
            live = self._live.get(self._chat_id(chat_id))
            allowed = bool(live and not live.admission and not live.settings_change_id
                           and live.kernel_retirement is None)
            future = Future() if allowed else None
            if allowed:
                live.kernel_retirement = future
        try:
            yield allowed
        finally:
            if future is not None:
                with self._guard:
                    if live.kernel_retirement is future:
                        live.kernel_retirement = None
                future.set_result(None)

    async def reserve_run(self, chat_id: str, **fields: Any) -> str | None:
        """Wait for an already-claimed retirement; ordinary busy admission stays unchanged."""
        clean = self._chat_id(chat_id)
        while True:
            admission = self.try_reserve_run(clean, **fields)
            if admission is not None:
                return admission
            with self._guard:
                live = self._live.get(clean)
                if live is None or live.admission or live.settings_change_id:
                    return None
                retirement = live.kernel_retirement
            if retirement is not None:
                # One cancelled waiter must not cancel the shared retirement
                # completion signal or unblock another waiter prematurely.
                await asyncio.shield(asyncio.wrap_future(retirement))
            # Retirement may have finished between reservation and inspection.

    def try_reserve_run(
        self,
        chat_id: str,
        *,
        attachment_id: str = "",
        background: bool = False,
    ) -> str | None:
        clean = self._chat_id(chat_id)
        record = self.ensure_runtime(clean)
        with self._guard:
            park_reason = self._live[clean].queue_park_reason
        if park_reason:
            self.park_queued_input_tickets(clean, reason=park_reason)
        if not self._budget_allows(record):
            raise BudgetExhausted(
                "chat continuation is paused because its admitted budget is exhausted"
            )
        with self._guard:
            live = self._live.setdefault(clean, _LiveRuntime(clean))
            if live.settings_change_id or live.kernel_retirement is not None:
                return None
            admission = live.admission
            if admission is not None:
                # Admission ends only at the explicit terminal commit boundary.
                # A completed/cancelled asyncio task can still be persisting its
                # visible exchange and clearing ActiveTurn.
                return None
            admission = _RunAdmission(
                admission_id="admit_" + uuid.uuid4().hex,
                chat_id=clean,
                attachment_id=str(attachment_id or ""),
                reserved_at=time.time(),
                background=bool(background),
                budget_baseline={
                    str(key): float(value or 0.0)
                    for key, value in record.budget_used.items()
                },
                owner_session=(
                    self._attachments.get(str(attachment_id or "")).session
                    if self._attachments.get(str(attachment_id or "")) is not None
                    else None
                ),
                owner_transport=(
                    self._attachments.get(str(attachment_id or "")).transport
                    if self._attachments.get(str(attachment_id or "")) is not None
                    else None
                ),
            )
            live.admission = admission
            self._reservations[admission.admission_id] = admission
        self._emit(
            "runtime:run_reserved",
            status="running",
            chat_id=clean,
            admission_id=admission.admission_id,
            attachment_id=admission.attachment_id,
        )
        return admission.admission_id

    def bind_admission_task(self, admission_id: str, task: asyncio.Task) -> None:
        with self._guard:
            admission = self._reservations.get(str(admission_id or ""))
            if admission is None:
                raise LookupError(f"unknown run admission: {admission_id}")
            admission.task = task

    def admission_chat_id(self, admission_id: str) -> str:
        """Return the immutable chat that owns a transferred admission."""
        with self._guard:
            admission = self._reservations.get(str(admission_id or ""))
            if admission is None:
                raise LookupError(f"unknown run admission: {admission_id}")
            return admission.chat_id

    def begin_run(
        self,
        admission_id: str,
        *,
        run_id: str,
        thread_id: str,
        source: str = "chat",
    ) -> None:
        with self._guard:
            admission = self._reservations.get(str(admission_id or ""))
            if admission is None:
                raise LookupError(f"unknown run admission: {admission_id}")
            admission.run_id = str(run_id or "")
            admission.thread_id = str(thread_id or run_id or "")
            chat_id = admission.chat_id
        self.repository.link_thread(chat_id, admission.thread_id, source=source)
        self._emit(
            "runtime:run_started",
            status="running",
            chat_id=chat_id,
            run_id=admission.run_id,
            thread_id=admission.thread_id,
            admission_id=admission.admission_id,
        )

    def finish_run(self, admission_id: str, *, status: str) -> None:
        key = str(admission_id or "")
        with self._guard:
            admission = self._reservations.pop(key, None)
            if admission is None:
                return
            live = self._live.get(admission.chat_id)
            if live is not None and live.admission is admission:
                live.admission = None
            admission.resume_event.set()
        self._emit(
            "runtime:run_finished",
            status=status,
            chat_id=admission.chat_id,
            run_id=admission.run_id,
            admission_id=admission.admission_id,
        )

    @staticmethod
    def _pause_state(admission: _RunAdmission | None) -> str:
        if admission is None:
            return "idle"
        return "paused" if admission.paused else "pausing" if admission.pause_requested else "running"

    def pause_snapshot(self, chat_id: str) -> dict[str, Any]:
        clean = self._chat_id(chat_id)
        with self._guard:
            live = self._live.get(clean)
            admission = live.admission if live else None
            return {
                "type": "chat:pause_state", "session_id": clean,
                "admission_id": admission.admission_id if admission else "",
                "run_id": admission.run_id if admission else "",
                "state": self._pause_state(admission),
                "pause_revision": admission.pause_revision if admission else 0,
            }

    def set_run_paused(
        self, chat_id: str, paused: bool, *,
        expected_admission_id: str = "", expected_run_id: str = "",
    ) -> dict[str, Any]:
        """Request a boundary pause/resume without cancelling the admitted run."""
        clean = self._chat_id(chat_id)
        with self._guard:
            live = self._live.get(clean)
            admission = live.admission if live else None
            if ((expected_admission_id and (admission is None or admission.admission_id != expected_admission_id))
                    or (expected_run_id and (admission is None or admission.run_id != expected_run_id))):
                raise RuntimeError("stale_run")
            if admission is None:
                raise RuntimeError("no_active_run")
            if not admission.accepting_inputs:
                raise RuntimeError("active_turn_finalizing")
            if admission.pause_requested != paused:
                admission.pause_requested = paused
                admission.pause_revision += 1
                if paused:
                    admission.resume_event.clear()
                else:
                    admission.paused = False
                    admission.resume_event.set()
            return self.pause_snapshot(clean)

    async def wait_if_paused(
        self, chat_id: str, admission_id: str,
        on_change: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Hold a canonical graph boundary; the previous node is already saved."""
        clean = self._chat_id(chat_id)
        if not admission_id:
            return
        while True:
            update = None
            with self._guard:
                live = self._live.get(clean)
                admission = live.admission if live else None
                if admission is None or admission.admission_id != admission_id:
                    raise asyncio.CancelledError("run admission released while paused")
                if not admission.pause_requested:
                    return
                if not admission.paused:
                    admission.paused = True
                    admission.pause_revision += 1
                    update = {**self.pause_snapshot(clean), "accepted": True}
                event = admission.resume_event
            if update is not None:
                try:
                    await on_change(update)
                except Exception as exc:
                    self._emit("runtime:pause_notice_failed", chat_id=clean, error=str(exc))
                # Resume/new-Pause can race an asynchronous notification. Re-read
                # the admission before waiting on an event that may be cleared.
                continue
            await event.wait()

    def is_busy(self, chat_id: str) -> bool:
        clean = self._chat_id(chat_id)
        with self._guard:
            live = self._live.get(clean)
            admission = live.admission if live is not None else None
            if admission is None:
                return False
            return True

    def active_admission(self, chat_id: str) -> str:
        clean = self._chat_id(chat_id)
        with self._guard:
            live = self._live.get(clean)
            return live.admission.admission_id if live and live.admission else ""

    def begin_run_finalization(self, admission_id: str) -> bool:
        """Close active-input admission while terminal persistence is fenced."""
        key = str(admission_id or "")
        with self._guard:
            admission = self._reservations.get(key)
            if admission is None:
                return False
            admission.accepting_inputs = False
            return True

    def run_usage_delta(self, admission_id: str) -> dict[str, float]:
        key = str(admission_id or "")
        with self._guard:
            admission = self._reservations.get(key)
            if admission is None:
                return {}
            chat_id = admission.chat_id
            baseline = dict(admission.budget_baseline)
        record = self.repository.get_runtime(chat_id)
        if record is None:
            return {}
        keys = set(baseline) | set(record.budget_used)
        return {
            str(name): max(
                0.0,
                float(record.budget_used.get(name) or 0.0)
                - float(baseline.get(name) or 0.0),
            )
            for name in keys
        }

    def accepts_inputs(self, chat_id: str) -> bool:
        clean = self._chat_id(chat_id)
        with self._guard:
            live = self._live.get(clean)
            admission = live.admission if live is not None else None
            return bool(admission is not None and admission.accepting_inputs)

    def active_run_owner(self, chat_id: str) -> tuple[Any, Any] | None:
        """Return the session and transport that own the active chat run.

        A stop request may arrive from any window attached to a durable chat.
        The admission's attachment id, rather than the requesting attachment,
        is the authority for which in-memory turn must be finalized.
        """
        clean = self._chat_id(chat_id)
        with self._guard:
            live = self._live.get(clean)
            admission = live.admission if live is not None else None
            if admission is None:
                return None
            if admission.owner_session is not None:
                return admission.owner_session, admission.owner_transport
            if not admission.attachment_id:
                return None
            attachment = self._attachments.get(admission.attachment_id)
            if attachment is None:
                return None
            return attachment.session, attachment.transport

    def cancel_active_run(self, chat_id: str) -> asyncio.Task | None:
        """Cancel the one run owned by this durable chat, if any."""
        clean = self._chat_id(chat_id)
        with self._guard:
            live = self._live.get(clean)
            admission = live.admission if live is not None else None
            task = admission.task if admission is not None else None
            if task is None or task.done():
                return None
            task.cancel()
            return task

    def enqueue_input(
        self,
        chat_id: str,
        text: str,
        *,
        delivery: str,
        client_id: str = "",
        source: str = "",
        attachment_id: str = "",
        ticket_id: str = "",
        expected_admission_id: str = "",
        expected_run_id: str = "",
    ) -> InputTicket:
        clean = self._chat_id(chat_id)
        self.ensure_runtime(clean)
        with self._guard:
            live = self._live.get(clean)
            admission = live.admission if live is not None else None
            if live is not None and live.settings_change_id:
                raise RuntimeError("session_configuration_pending")
            if ((expected_admission_id and (admission is None or admission.admission_id != expected_admission_id))
                    or (expected_run_id and (admission is None or admission.run_id != expected_run_id))):
                raise RuntimeError("stale_run")
            if admission is not None and not admission.accepting_inputs:
                raise RuntimeError("active chat turn is finalizing")
            ticket = self.repository.create_ticket(
                clean,
                text,
                delivery=delivery,
                client_id=client_id,
                source=source,
                attachment_id=attachment_id,
                ticket_id=ticket_id,
            )
        self._emit(
            "runtime:input_ticket",
            status="queued",
            chat_id=clean,
            ticket_id=ticket.ticket_id,
            delivery=ticket.delivery,
        )
        return ticket

    def queued_input_count(self, chat_id: str) -> int:
        return len(self.repository.list_tickets(self._chat_id(chat_id), states=("queued",)))

    def queue_snapshot(self, chat_id: str) -> dict[str, Any]:
        return self.repository.queue_snapshot(self._chat_id(chat_id))

    def park_queued_input_tickets(self, chat_id: str, *, reason: str) -> list[InputTicket]:
        clean = self._chat_id(chat_id)
        with self._guard:
            live = self._live.get(clean)
            if live:
                live.queue_park_reason = reason
        tickets = self.repository.park_tickets(clean, reason=reason)
        with self._guard:
            if live and live.queue_park_reason == reason:
                live.queue_park_reason = ""
        for ticket in tickets:
            self._emit("runtime:input_ticket", status="parked", chat_id=clean,
                       ticket_id=ticket.ticket_id, reason=reason)
        return tickets

    def continue_parked_input(self, chat_id: str, ticket_id: str, *, expected_revision: int,
                             admission_id: str) -> InputTicket:
        clean = self._chat_id(chat_id)
        with self._guard:
            live = self._live.get(clean)
            if not live or not live.admission or live.admission.admission_id != admission_id:
                raise RuntimeError("queue_admission_changed")
            return self.repository.queued_ticket_command(clean, ticket_id,
                expected_revision=expected_revision, operation="continue")

    def promote_recovered_inputs(
        self,
        chat_id: str,
        *,
        delivered: Iterable[dict] = (),
        include_undelivered: bool = True,
    ) -> int:
        """Reconcile restart-held input against the selected resume snapshot.

        Snapshot-delivered tickets await transcript commit; only tickets absent
        from that checkpoint may be delivered to the model again. A terminal
        transcript recovery restores delivery proof without admitting new work.
        """

        clean = self._chat_id(chat_id)
        delivered_ids = {
            str(row.get("id") or "")
            for row in delivered
            if isinstance(row, dict) and str(row.get("id") or "")
        }
        promoted = 0
        for ticket in self.repository.list_tickets(
            clean, states=("resume_queued",)
        ):
            already_delivered = ticket.ticket_id in delivered_ids
            if not already_delivered and not include_undelivered:
                continue
            updated = self.repository.transition_ticket(
                ticket.ticket_id,
                "running" if already_delivered else "queued",
                expected=("resume_queued",),
                error="",
            )
            if updated is not None and updated.state == "queued":
                promoted += 1
        return promoted

    def claim_input(self, chat_id: str, delivery: str, *, run_id: str) -> dict | None:
        clean = self._chat_id(chat_id)
        with self._guard:
            live = self._live.get(clean)
            admission = live.admission if live is not None else None
            if admission is not None and not admission.accepting_inputs:
                return None
            ticket = self.repository.claim_ticket(
                clean, delivery, run_id=str(run_id or "")
            )
        if ticket is None:
            return None
        ticket = self.repository.transition_ticket(
            ticket.ticket_id,
            "preparing",
            expected=("selected",),
        ) or ticket
        self._emit(
            "runtime:input_ticket",
            status="preparing",
            chat_id=ticket.chat_id,
            ticket_id=ticket.ticket_id,
            run_id=run_id,
        )
        return ticket.as_state()

    def record_input_delivery(
        self,
        chat_id: str,
        session: Any,
        row: dict,
        assistant_text: str | None,
    ) -> None:
        recorder = getattr(session, "record_active_input", None)
        if callable(recorder):
            recorder(row, assistant_text)
        ticket_id = str((row or {}).get("id") or "")
        if ticket_id:
            self.repository.transition_ticket(
                ticket_id,
                "running",
                expected=("selected", "preparing"),
            )

    def begin_transcript_commit(self, delivered: Iterable[dict]) -> None:
        for row in delivered or ():
            ticket_id = str((row or {}).get("id") or "")
            if ticket_id:
                self.repository.transition_ticket(
                    ticket_id,
                    "transcript_committing",
                    expected=("running", "preparing", "selected", "parked"),
                )

    def complete_transcript_commit(self, chat_id: str, delivered: Iterable[dict]) -> None:
        clean = self._chat_id(chat_id)
        for row in delivered or ():
            ticket_id = str((row or {}).get("id") or "")
            if not ticket_id:
                continue
            self.repository.transition_ticket(
                ticket_id,
                "completed",
                expected=("transcript_committing", "running", "preparing"),
                proof={"chat_id": clean, "ticket_id": ticket_id},
            )
            self._emit(
                "runtime:input_ticket",
                status="completed",
                chat_id=clean,
                ticket_id=ticket_id,
            )

    def fail_transcript_commit(
        self,
        chat_id: str,
        delivered: Iterable[dict],
        *,
        error: str,
    ) -> list[InputTicket]:
        """Terminally fence visible-but-unpersisted delivered input."""

        clean = self._chat_id(chat_id)
        failed: list[InputTicket] = []
        for row in delivered or ():
            ticket_id = str((row or {}).get("id") or "")
            if not ticket_id:
                continue
            terminal = self.repository.transition_ticket(
                ticket_id,
                "transcript_failed",
                expected=("transcript_committing", "running", "preparing"),
                proof={
                    "chat_id": clean,
                    "ticket_id": ticket_id,
                    "terminal_reply_visible": True,
                    "transcript_persisted": False,
                },
                error=str(error or "transcript persistence failed")[:1000],
            )
            if terminal is None or terminal.state != "transcript_failed":
                continue
            failed.append(terminal)
            self._emit(
                "runtime:input_ticket",
                status="transcript_failed",
                chat_id=clean,
                ticket_id=ticket_id,
                error=terminal.error,
            )
        return failed

    def cancel_queued_inputs(self, chat_id: str, *, reason: str) -> int:
        return len(self.cancel_queued_input_tickets(chat_id, reason=reason))

    def cancel_queued_input_tickets(
        self, chat_id: str, *, reason: str
    ) -> list[InputTicket]:
        clean = self._chat_id(chat_id)
        tickets = self.repository.list_tickets(
            clean,
            states=("queued", "resume_queued", "selected", "preparing"),
        )
        cancelled: list[InputTicket] = []
        for ticket in tickets:
            terminal = self.repository.transition_ticket(
                ticket.ticket_id,
                "cancelled",
                expected=(ticket.state,),
                error=reason,
            )
            if terminal is None or terminal.state != "cancelled":
                continue
            cancelled.append(terminal)
            self._emit(
                "runtime:input_ticket",
                status="cancelled",
                chat_id=clean,
                ticket_id=terminal.ticket_id,
                reason=reason,
            )
        return cancelled

    def set_budget(self, chat_id: str, limits: dict[str, float]) -> ChatRuntimeRecord:
        record = self.ensure_runtime(chat_id)
        updated = self.repository.update_continuation(
            record.chat_id, "ready", limits=limits
        )
        return updated

    def record_run_usage(self, chat_id: str, run_id: str, receipt: dict[str, Any]) -> ChatRuntimeRecord:
        usage = {
            "provider_calls": float(receipt.get("llm_calls") or 0),
            "tokens": float(receipt.get("total_tokens") or 0),
            "cost_usd": float(receipt.get("cost_usd") or 0),
            "wall_time_s": float(receipt.get("wall_time_s") or 0),
        }
        return self.repository.record_usage(self._chat_id(chat_id), str(run_id or ""), usage)

    async def delete_chat(self, chat_id: str, sessions: Any) -> str:
        clean = self._chat_id(chat_id)
        # Tombstone the canonical owner before touching subordinate runtime
        # resources. A failed SQL mutation must never leave runtime cleanup
        # reporting a successful conversation deletion.
        fallback = sessions.delete(clean)
        self.ensure_runtime(fallback, is_new=True)
        record = self.repository.get_runtime(clean)
        if record is None:
            return fallback
        self.repository.set_lifecycle(clean, "deleting", deletion_saga_state="draining")
        tasks: list[asyncio.Task] = []
        kernel = None
        with self._guard:
            live = self._live.setdefault(clean, _LiveRuntime(clean))
            admission = live.admission
            if admission and admission.task and not admission.task.done():
                admission.task.cancel()
                tasks.append(admission.task)
            kernel = live.kernel_lease
            live.kernel_lease = None
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if kernel is not None:
            closer = getattr(kernel, "close", None) or getattr(kernel, "shutdown", None)
            if callable(closer):
                result = closer(reason="chat_deleted", hard=True)
                if asyncio.iscoroutine(result):
                    await result
        async with self.writer_lock_allow_deleted(clean):
            self.repository.set_lifecycle(
                clean, "deleting", deletion_saga_state="deleting_threads"
            )
            thread_ids = await self._delete_thread_snapshots(clean)
            for ticket in self.repository.list_tickets(clean):
                if ticket.state not in TICKET_TERMINAL_STATES:
                    self.repository.transition_ticket(
                        ticket.ticket_id,
                        "cancelled",
                        expected=(ticket.state,),
                        error="chat_deleted",
                    )
            self.repository.set_lifecycle(
                clean, "deleting", deletion_saga_state="deleting_transcript"
            )
            for cleanup in tuple(self._chat_cleanup):
                result = cleanup(clean)
                if asyncio.iscoroutine(result):
                    await result
            self.repository.set_lifecycle(
                clean, "deleted", deletion_saga_state="complete"
            )
        with self._guard:
            live = self._live.pop(clean, None)
            attachment_ids = list(live.attachment_ids) if live else []
        for attachment_id in attachment_ids:
            with self._guard:
                attachment = self._attachments.get(attachment_id)
            if attachment is not None:
                self.attach(fallback, attachment_id, attachment.session, attachment.transport)
        self._emit(
            "runtime:chat_deleted",
            status="ok",
            chat_id=clean,
            fallback_chat_id=fallback,
            threads=len(thread_ids),
        )
        return fallback

    async def tombstone_chat_owner(self, chat_id: str, sessions: Any) -> str:
        """Tombstone a canonical Conversation owner without purging runtime state.

        The current writer is drained and queued inputs are cancelled, but the
        runtime record, native snapshot ancestry, kernel generation, catalog
        state, artifact grants, and thread references remain recoverable. Hard
        deletion is a separate explicit retention operation.
        """

        clean = self._chat_id(chat_id)
        record = self.repository.get_runtime(clean)
        tasks: list[asyncio.Task] = []
        with self._guard:
            live = self._live.setdefault(clean, _LiveRuntime(clean))
            admission = live.admission
            if admission and admission.task and not admission.task.done():
                admission.task.cancel()
                tasks.append(admission.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        stop_failures: list[BaseException] = []
        for cleanup in tuple(self._chat_tombstone_cleanup):
            try:
                result = cleanup(clean)
                if asyncio.iscoroutine(result):
                    await result
            except BaseException as exc:
                stop_failures.append(exc)
        if stop_failures:
            raise RuntimeError(
                f"chat tombstone left {len(stop_failures)} active owner(s) retryable"
            ) from stop_failures[0]
        # Stop live subordinate owners before hiding their only control
        # surface. If any stop fails above, the chat remains visible/retryable.
        fallback = sessions.delete(clean)
        self.ensure_runtime(fallback, is_new=True)
        if record is not None and record.lifecycle_state == "active":
            async with self.writer_lock(clean):
                for ticket in self.repository.list_tickets(clean):
                    if ticket.state not in TICKET_TERMINAL_STATES:
                        self.repository.transition_ticket(
                            ticket.ticket_id,
                            "cancelled",
                            expected=(ticket.state,),
                            error="conversation_owner_tombstoned",
                        )
                self.repository.update_continuation(
                    clean, "owner_tombstoned"
                )
        with self._guard:
            live = self._live.get(clean)
            attachment_ids = list(live.attachment_ids) if live else []
        for attachment_id in attachment_ids:
            with self._guard:
                attachment = self._attachments.get(attachment_id)
            if attachment is not None:
                self.attach(
                    fallback,
                    attachment_id,
                    attachment.session,
                    attachment.transport,
                )
        self._emit(
            "runtime:chat_owner_tombstoned",
            status="ok",
            chat_id=clean,
            fallback_chat_id=fallback,
            runtime_preserved=True,
        )
        return fallback

    async def delete_worker_runtime(self, runtime_id: str, *, source: str) -> bool:
        """Drain and tombstone one non-chat durable worker runtime."""
        clean = self._chat_id(runtime_id)
        worker_source = str(source or "").strip().lower()
        marker = f"worker:{worker_source}"
        record = self.repository.get_runtime(clean)
        if record is None:
            return False
        if str(record.creation_saga_state or "") != marker:
            raise RuntimeError(
                f"refusing to delete non-{marker} runtime {clean!r}"
            )
        if record.lifecycle_state == "deleted":
            return False
        self.repository.set_lifecycle(
            clean, "deleting", deletion_saga_state="draining_worker"
        )
        tasks: list[asyncio.Task] = []
        kernel = None
        with self._guard:
            live = self._live.setdefault(clean, _LiveRuntime(clean))
            admission = live.admission
            if admission and admission.task and not admission.task.done():
                admission.task.cancel()
                tasks.append(admission.task)
            kernel = live.kernel_lease
            live.kernel_lease = None
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if kernel is not None:
            closer = getattr(kernel, "close", None) or getattr(kernel, "shutdown", None)
            if callable(closer):
                result = closer(reason="worker_deleted", hard=True)
                if asyncio.iscoroutine(result):
                    await result
        async with self.writer_lock_allow_deleted(clean):
            self.repository.set_lifecycle(
                clean, "deleting", deletion_saga_state="deleting_worker_threads"
            )
            thread_ids = await self._delete_thread_snapshots(clean)
            for cleanup in tuple(self._chat_cleanup):
                result = cleanup(clean)
                if asyncio.iscoroutine(result):
                    await result
            self.repository.set_lifecycle(
                clean, "deleted", deletion_saga_state="complete"
            )
        with self._guard:
            self._live.pop(clean, None)
        self._emit(
            "runtime:worker_deleted",
            status="ok",
            chat_id=clean,
            source=worker_source,
            threads=len(thread_ids),
        )
        return True

    async def delete_child_runtime(
        self, runtime_id: str, *, parent_chat_id: str
    ) -> bool:
        """Drain one child runtime and its cleanup callbacks as a retryable saga."""

        clean = self._chat_id(runtime_id)
        marker = f"child:{self._chat_id(parent_chat_id)}"
        record = self.repository.get_runtime(clean)
        if record is None:
            return False
        if str(record.creation_saga_state or "") != marker:
            raise RuntimeError(
                f"refusing to delete runtime {clean!r} not owned by {marker!r}"
            )
        if record.lifecycle_state == "deleted":
            return False
        cleanup_prefix = (
            "orphan_child_creation:"
            if str(record.deletion_saga_state or "").startswith("orphan_child_creation")
            else ""
        )
        self.repository.set_lifecycle(
            clean, "deleting", deletion_saga_state=cleanup_prefix + "draining_child"
        )
        tasks: list[asyncio.Task] = []
        kernel = None
        with self._guard:
            live = self._live.setdefault(clean, _LiveRuntime(clean))
            admission = live.admission
            if admission and admission.task and not admission.task.done():
                admission.task.cancel()
                tasks.append(admission.task)
            kernel = live.kernel_lease
            live.kernel_lease = None
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if kernel is not None:
            closer = getattr(kernel, "close", None) or getattr(kernel, "shutdown", None)
            if callable(closer):
                result = closer(reason="child_deleted", hard=True)
                if asyncio.iscoroutine(result):
                    await result
        async with self.writer_lock_allow_deleted(clean):
            self.repository.set_lifecycle(
                clean, "deleting", deletion_saga_state=cleanup_prefix + "deleting_child_threads"
            )
            thread_ids = await self._delete_thread_snapshots(clean)
            for cleanup in tuple(self._chat_cleanup):
                result = cleanup(clean)
                if asyncio.iscoroutine(result):
                    await result
            self.repository.set_lifecycle(
                clean, "deleted", deletion_saga_state=cleanup_prefix + "complete"
            )
        with self._guard:
            self._live.pop(clean, None)
        self._emit(
            "runtime:child_deleted",
            status="ok",
            chat_id=clean,
            parent_chat_id=str(parent_chat_id),
            threads=len(thread_ids),
        )
        return True

    def writer_lock_allow_deleted(self, chat_id: str) -> asyncio.Lock:
        clean = self._chat_id(chat_id)
        with self._guard:
            return self._live.setdefault(clean, _LiveRuntime(clean)).writer_lock

    async def shutdown(self) -> None:
        tasks: list[asyncio.Task] = []
        kernels: list[Any] = []
        with self._guard:
            for live in self._live.values():
                admission = live.admission
                if admission and admission.task and not admission.task.done():
                    admission.task.cancel()
                    tasks.append(admission.task)
                if live.kernel_lease is not None:
                    kernels.append(live.kernel_lease)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for kernel in kernels:
            closer = getattr(kernel, "close", None) or getattr(kernel, "shutdown", None)
            if callable(closer):
                result = closer()
                if asyncio.iscoroutine(result):
                    await result
