"""Canonical peer communication service and native-chat delivery adapter."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from background_tasks import OwnedTaskSet

from .repository import MESSAGE_DIRECTIONS, PeerRepository
from .delivery import MESSAGE_KINDS, requests_work, grok_delivery_status


PORTABLE_PEER_ADAPTER = "mcp-peer-bridge"
_LOG = logging.getLogger(__name__)


class PeerError(RuntimeError):
    def __init__(
        self, code: str, message: str, *, commit_state: str = "not_committed",
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.commit_state = str(commit_state)


def _native_peer_id(chat_id: str) -> str:
    return "chat:" + str(chat_id or "").strip()


def _native_chat_id(peer_id: str) -> str:
    clean = str(peer_id or "").strip()
    return clean[5:] if clean.startswith("chat:") else ""


def _bounded_text(value: Any, *, limit: int = 80_000) -> str:
    text = str(value or "").strip()
    if not text:
        raise PeerError("peer_message_empty", "peer message text is required")
    if len(text) > limit:
        raise PeerError(
            "peer_message_too_large",
            f"peer message exceeds {limit} characters",
        )
    return text


class PeerCommunicationService:
    """Durable peer identity, messaging, delivery and reply correlation."""

    def __init__(
        self,
        host: Any,
        repository: PeerRepository,
        *,
        sessions: Any,
        session_runtimes: Any,
        chat_service: Any,
    ) -> None:
        self.host = host
        self.repository = repository
        self.sessions = sessions
        self.session_runtimes = session_runtimes
        self.chat_service = chat_service
        self._publisher: Callable[[dict[str, Any]], Any] | None = None
        self._external_hooks: dict[str, Callable[[dict[str, Any]], Any]] = {}
        self._wake_tasks: dict[str, asyncio.Task] = {}
        self._poll_task: asyncio.Task | None = None
        self._publication_tasks = OwnedTaskSet()
        self._closed = False
        self._change_event = asyncio.Event()
        bind_display = getattr(sessions, "bind_peer_display", None)
        if callable(bind_display):
            bind_display(self.message_display, self.sent_display)

    @staticmethod
    def _sent_message(row: Mapping[str, Any]) -> dict:
        evidence = row.get("evidence") or {}
        return {
            **{key: row[key] for key in (
                "message_id", "sender_peer_id", "target_peer_id", "content", "state",
            )},
            "message_kind": row.get("message_kind", "request"),
            "target_display_name": evidence.get("target_display_name", ""),
            "sender_invocation": dict(evidence.get("sender_invocation") or {}),
        }

    def sent_display(self, chat_id: str, run_id: str) -> dict | None:
        try:
            rows = self.repository.messages_for_sender_run(_native_peer_id(chat_id), run_id)
            return {"messages": [self._sent_message(row) for row in rows[:200]],
                    "has_more": len(rows) > 200}
        except Exception:
            _LOG.warning("peer sent display lookup unavailable", exc_info=True)
            return None

    def message_display(self, origin: Mapping[str, Any]) -> dict[str, str] | None:
        """Resolve presentation by durable provenance, including inherited rows."""
        if origin.get("kind") != "peer":
            return None
        try:
            row = self.repository.get_message(str(origin.get("message_id") or ""))
            if (row is None or row["sender_peer_id"] != origin.get("peer_id")
                    or not _native_chat_id(row["target_peer_id"])):
                return None
            name = str((row.get("evidence") or {}).get("sender_display_name") or "")
            if not name:
                try:
                    name = str(self.get_peer(row["sender_peer_id"]).get("display_name") or "")
                except PeerError:
                    pass
            return {"display_name": name or row["sender_peer_id"], "content": row["content"]}
        except Exception:
            # Presentation loss must not turn a committed peer message into a
            # failed transcript append or trigger a resend.
            _LOG.warning("peer display lookup unavailable", exc_info=True)
            return None

    async def _emit_sent(self, row: Mapping[str, Any]) -> None:
        evidence = row.get("evidence") or {}
        invocation = evidence.get("sender_invocation")
        emit = getattr(self.host, "emit_activity", None)
        if not isinstance(invocation, dict) or not callable(emit):
            return
        try:
            await emit(
                "peer:sent", session_id=invocation["chat_id"],
                chat_id=invocation["chat_id"], run_id=invocation["run_id"],
                call_id=invocation["nested_call_id"],
                outer_call_id=invocation["outer_tool_call_id"],
                cell_execution_id=invocation["cell_execution_id"],
                sender_invocation=dict(invocation),
                peer_message=self._sent_message(row),
            )
        except Exception:
            _LOG.warning("peer send activity unavailable; message evidence is durable", exc_info=True)

    def bind_publisher(self, publisher: Callable[[dict[str, Any]], Any]) -> None:
        if not callable(publisher):
            raise TypeError("peer publisher must be callable")
        self._publisher = publisher

    def register_external_hook(
        self, adapter: str, callback: Callable[[dict[str, Any]], Any],
    ) -> None:
        clean = str(adapter or "").strip()
        if (
            not clean or not callable(callback)
            or inspect.iscoroutinefunction(callback)
        ):
            raise TypeError("external peer hook requires adapter and callback")
        self._external_hooks[clean] = callback

    def _event(self, row: Mapping[str, Any], *, peer_id: str = "") -> None:
        revision = int(row.get("revision") or self.repository.revision())
        peer_ids = {
            str(peer_id or ""),
            str(row.get("sender_peer_id") or ""),
            str(row.get("target_peer_id") or ""),
            str(row.get("peer_id") or ""),
        }
        self._change_event.set()
        self._change_event = asyncio.Event()
        publisher = self._publisher
        if not callable(publisher):
            return
        events = []
        native_ids = sorted({item for item in peer_ids if _native_chat_id(item)})
        if native_ids:
            for owner in native_ids:
                events.append({
                    "type": "peer:changed",
                    "chat_id": _native_chat_id(owner),
                    "peer_id": owner,
                    "message_id": str(row.get("message_id") or ""),
                    "revision": revision,
                })
        else:
            events.append({
                "type": "peer:changed",
                "chat_id": "",
                "peer_id": str(peer_id or row.get("peer_id") or ""),
                "message_id": str(row.get("message_id") or ""),
                "revision": revision,
            })
        for event in events:
            try:
                result = publisher(event)
                if inspect.isawaitable(result):
                    self._publication_tasks.spawn(result, name='peer-change-publication')
            except Exception:
                continue

    def _native_descriptor(self, session: Mapping[str, Any]) -> dict[str, Any]:
        chat_id = str(session.get("id") or "")
        snapshot = self.session_runtimes.snapshot(chat_id)
        pause = str(snapshot.get("pause_state") or "idle")
        return {
            "peer_id": _native_peer_id(chat_id),
            "kind": "variant_chat",
            "display_name": str(session.get("title") or "New chat"),
            "chat_id": chat_id,
            "status": (
                "paused" if pause in {"paused", "pausing"}
                else "busy" if snapshot.get("busy") else "idle"
            ),
            "capabilities": {
                "live_ingress": True,
                "busy_message_queueing": True,
                "structured_replies": True,
                "closed_view_wake": True,
            },
            "project": session.get("project"),
            "archived": bool(session.get("archived")),
            "revision": int(snapshot.get("version") or 0),
        }

    def _apply_connection_effects(
        self, effects: Mapping[str, Any] | None,
    ) -> None:
        if not isinstance(effects, Mapping):
            return
        for row in effects.get("messages") or ():
            if isinstance(row, Mapping):
                self._event(row)
        for row in effects.get("connections") or ():
            if isinstance(row, Mapping):
                self._event(row, peer_id=str(row.get("peer_id") or ""))

    def _external_descriptor(self, row: Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        leases = self.repository.list_connections(
            peer_id=str(item.get("peer_id") or ""),
            statuses=("active", "conflicted"), limit=100,
        )
        active = [lease for lease in leases if lease.get("status") == "active"]
        conflicted = [
            lease for lease in leases if lease.get("status") == "conflicted"
        ]
        if leases:
            item["status"] = (
                "conflicted" if conflicted else "connected" if active else "disconnected"
            )
        elif item.get("adapter") == "grok-acp-mcp":
            # The retired terminal-bound adapter has no receiver in the new
            # runtime. Preserve its peer/history, but not a persisted online flag.
            item["status"] = "disconnected"
            item["capabilities"] = {**dict(item.get("capabilities") or {}), "live_ingress": False}
        item["active_connections"] = active
        item["connection_conflicts"] = conflicted
        if str(item.get("peer_id") or "").startswith("grok:"):
            connection = next((lease for lease in active if lease.get("delivery_owner")), (leases or [{}])[0])
            delivery = grok_delivery_status(connection, self.repository.delivery_preference(item["peer_id"]))
            item.update(delivery)
            item["capabilities"] = {**dict(item.get("capabilities") or {}),
                                    **{key: delivery[key] for key in ("native_agent_origin", "live_ingress", "automatic_wake_available")},
                                    "busy_message_queueing": delivery["live_ingress"]}
        return item

    def list_peers(
        self, kind: str = "", status: str = "", limit: int = 100,
    ) -> list[dict[str, Any]]:
        wanted_kind = str(kind or "").strip()
        wanted_status = str(status or "").strip()
        rows: list[dict[str, Any]] = []
        if wanted_kind in {"", "variant_chat"}:
            for session in self.sessions.list_sessions():
                if not isinstance(session, Mapping):
                    continue
                row = self._native_descriptor(session)
                if wanted_status and row["status"] != wanted_status:
                    continue
                rows.append(row)
        if wanted_kind in {"", "external_harness"}:
            self.expire_connections()
            for row in self.repository.list_endpoints(limit=max(1, int(limit))):
                row = self._external_descriptor(row)
                if wanted_status and row.get("status") != wanted_status:
                    continue
                rows.append(row)
        rows.sort(key=lambda row: (
            str(row.get("kind") or ""),
            str(row.get("display_name") or "").casefold(),
            str(row.get("peer_id") or ""),
        ))
        return rows[:max(1, min(int(limit), 500))]

    def get_peer(self, peer_id: str) -> dict[str, Any]:
        clean = str(peer_id or "").strip()
        chat_id = _native_chat_id(clean)
        if chat_id:
            session = self.sessions.get_session(chat_id)
            if session is None:
                raise PeerError("peer_not_found", f"unknown native peer: {clean}")
            return self._native_descriptor(session)
        endpoint = self.repository.get_endpoint(clean)
        if endpoint is None:
            raise PeerError("peer_not_found", f"unknown peer: {clean}")
        self.expire_connections()
        return self._external_descriptor(endpoint)

    def _message_for_peer(self, peer_id: str, message_id: str) -> dict[str, Any]:
        row = self.repository.get_message(str(message_id or ""))
        if row is None:
            raise PeerError("peer_message_not_found", "peer message was not found")
        if str(peer_id) not in {
            str(row.get("sender_peer_id") or ""),
            str(row.get("target_peer_id") or ""),
        }:
            raise PeerError("peer_message_forbidden", "message does not belong to this peer")
        return self._sync_native_state(row)

    def inspect_message(self, peer_id: str, message_id: str) -> dict[str, Any]:
        return self._message_for_peer(peer_id, message_id)

    def inspect_request(self, peer_id: str, request_id: str) -> dict[str, Any]:
        clean = str(request_id or "").strip()
        if not clean:
            raise PeerError("peer_request_id_required", "request_id is required")
        row = self.repository.get_message_by_request(peer_id, clean)
        if row is None:
            raise PeerError("peer_request_not_found", "peer request was not found")
        return self._sync_native_state(row)

    def inbox(
        self, peer_id: str, after: int = 0, limit: int = 50,
        direction: str = "incoming",
    ) -> dict[str, Any]:
        self.get_peer(peer_id)
        if direction not in MESSAGE_DIRECTIONS:
            raise PeerError(
                "peer_direction_invalid", "direction must be incoming, outgoing, or all",
            )
        rows = [
            self._sync_native_state(row)
            for row in self.repository.list_messages(
                peer_id, after=after, limit=limit, direction=direction,
            )
        ]
        return {
            "peer_id": str(peer_id),
            "direction": direction,
            "messages": rows,
            "cursor": int(rows[-1]["sequence"]) if rows else max(0, int(after)),
            "revision": self.repository.revision(),
        }

    def history(
        self, peer_id: str, other_peer_id: str, after: int = 0, limit: int = 50,
    ) -> dict[str, Any]:
        self.get_peer(peer_id)
        self.get_peer(other_peer_id)
        rows = [
            self._sync_native_state(row)
            for row in self.repository.list_history(
                peer_id, other_peer_id, after=after,
                limit=max(1, min(int(limit), 100)),
            )
        ]
        return {
            "peer_id": str(peer_id),
            "other_peer_id": str(other_peer_id),
            "messages": rows,
            "cursor": int(rows[-1]["sequence"]) if rows else max(0, int(after)),
            "revision": self.repository.revision(),
        }

    @staticmethod
    def _ticket_id(message_id: str) -> str:
        return "peer_ticket_" + hashlib.sha256(
            str(message_id).encode("utf-8")
        ).hexdigest()[:40]

    def _native_text(self, row: Mapping[str, Any]) -> str:
        sender_id = str(row.get("sender_peer_id") or "")
        try:
            sender = self.get_peer(sender_id)
            sender_name = str(sender.get("display_name") or sender_id)
        except PeerError:
            # Delivery is durable independently of the sender's later
            # lifecycle. The immutable peer id still provides true
            # attribution when a sender is deleted before ticket admission.
            sender_name = sender_id
        return (
            "[Peer message]\n"
            f"Sender: {sender_name} ({sender_id})\n"
            f"Message ID: {row.get('message_id')}\n"
            f"Exchange ID: {row.get('exchange_id')}\n\n"
            + str(row.get("content") or "")
            + "\n\nReply only when useful with peers.inspect_message(...).reply(...)."
        )

    def _sync_native_state(self, row: Mapping[str, Any]) -> dict[str, Any]:
        message = dict(row)
        ticket_id = str(message.get("delivery_ticket_id") or "")
        if not ticket_id or not _native_chat_id(str(message.get("target_peer_id") or "")):
            return message
        ticket = self.session_runtimes.repository.get_ticket(ticket_id)
        if ticket is None:
            return message
        state_map = {
            "queued": "queued", "selected": "queued", "preparing": "queued",
            "resume_queued": "parked", "parked": "parked",
            # `running` means the input was drained for injection, but the
            # recipient may still fail before a model turn consumes it. Only
            # transcript commit proves the recipient processed the input.
            "running": "queued", "transcript_committing": "observed",
            "completed": "observed", "cancelled": "parked",
            "rejected": "failed", "transcript_failed": "unknown",
        }
        projected = state_map.get(ticket.state)
        if projected and projected != message.get("state") and message.get("state") != "replied":
            message = self.repository.update_message(
                str(message["message_id"]), state=projected,
                target_run_id=str(ticket.run_id or ""),
                evidence={
                    "ticket_state": ticket.state,
                    "ticket_proof": dict(ticket.proof),
                    "receipt_semantics": (
                        "recipient_transcript_commit_started"
                        if ticket.state in {"transcript_committing", "completed"}
                        else "recipient_input_pending"
                    ),
                },
                error=str(ticket.error or ""),
            )
            self._event(message)
        return message

    async def _admit_native(self, row: Mapping[str, Any]) -> dict[str, Any]:
        message = dict(row)
        if not requests_work(message):
            updated = self.repository.update_message(str(message["message_id"]), state="queued")
            self._event(updated)
            return updated
        chat_id = _native_chat_id(str(message.get("target_peer_id") or ""))
        target = self.sessions.get_session(chat_id)
        if target is None:
            updated = self.repository.update_message(
                str(message["message_id"]), state="failed",
                error="native peer is unavailable",
            )
            self._event(updated)
            return updated
        if target.get("archived"):
            updated = self.repository.update_message(
                str(message["message_id"]), state="parked",
                error="native peer is archived",
            )
            self._event(updated)
            return updated
        ticket_id = str(message.get("delivery_ticket_id") or self._ticket_id(
            str(message["message_id"])
        ))
        try:
            ticket = self.session_runtimes.enqueue_input(
                chat_id,
                self._native_text(message),
                delivery=str(message.get("delivery") or "follow_up"),
                client_id="peer-message:" + str(message["message_id"]),
                source="peer:" + str(message.get("sender_peer_id") or ""),
                ticket_id=ticket_id,
            )
        except RuntimeError as exc:
            if str(exc) in {
                "active chat turn is finalizing", "session_configuration_pending",
            }:
                self._schedule_native_wake(chat_id)
                return message
            raise
        queued = self.repository.update_message(
            str(message["message_id"]), state="queued",
            delivery_ticket_id=ticket.ticket_id,
            evidence={"ticket_state": ticket.state},
        )
        self._event(queued)
        self._schedule_native_wake(chat_id)
        return queued

    def _schedule_native_wake(self, chat_id: str) -> None:
        clean = str(chat_id or "")
        task = self._wake_tasks.get(clean)
        if task is not None and not task.done():
            return
        self._wake_tasks[clean] = asyncio.create_task(
            self._wake_native(clean), name=f"peer-native-wake:{clean[:48]}"
        )

    async def _wake_native(self, chat_id: str) -> None:
        idle_delay = 0.1
        try:
            while not self._closed:
                pending = self.repository.pending_native(chat_id=chat_id)
                if not pending:
                    return
                for row in pending:
                    if not row.get("delivery_ticket_id"):
                        try:
                            await self._admit_native(row)
                        except Exception as exc:
                            # Admission can commit its deterministic ticket before
                            # a notification fails. Reconcile that fact before
                            # stopping retries; never enqueue a second message.
                            ticket_id = self._ticket_id(str(row["message_id"]))
                            state, ticket = "unknown", None
                            try:
                                ticket = self.session_runtimes.repository.get_ticket(ticket_id)
                                state = "queued" if ticket is not None else "failed"
                            except Exception:
                                _LOG.exception("Native peer ticket reconciliation failed")
                            updated = self.repository.update_message(
                                str(row["message_id"]), state=state,
                                delivery_ticket_id=ticket_id if ticket is not None else "",
                                error=f"native admission failed: {type(exc).__name__}: {exc}"[:1000],
                                evidence={**dict(row.get("evidence") or {}),
                                          "admission_error": type(exc).__name__,
                                          "ticket_state": getattr(ticket, "state", "")},
                            )
                            self._event(updated)
                            _LOG.warning("Native peer admission %s: %s", state, type(exc).__name__)
                if not self.repository.pending_native(chat_id=chat_id):
                    return
                if self.session_runtimes.is_busy(chat_id):
                    await asyncio.sleep(0.1)
                    continue
                outcome = await self.chat_service.start_next_queued_input(chat_id)
                if outcome.get("status") == "started":
                    turn_task = outcome.get("_task")
                    if not isinstance(turn_task, asyncio.Task):
                        return
                    await asyncio.gather(turn_task, return_exceptions=True)
                    self._resume_chainable_native_tickets(chat_id)
                    idle_delay = 0.1
                    continue
                if outcome.get("status") in {"parked", "unavailable"}:
                    return
                await asyncio.sleep(idle_delay)
                idle_delay = min(idle_delay * 2, 2.0)
        finally:
            self._wake_tasks.pop(chat_id, None)

    def _resume_chainable_native_tickets(self, chat_id: str) -> int:
        """Continue peer work parked only by a successful prior turn boundary."""

        resumed = 0
        for row in self.repository.pending_native(chat_id=chat_id):
            ticket_id = str(row.get("delivery_ticket_id") or "")
            ticket = self.session_runtimes.repository.get_ticket(ticket_id)
            if (
                ticket is None
                or ticket.state != "parked"
                or str(ticket.error or "") != "turn_ok_before_input_delivery"
            ):
                continue
            updated = self.session_runtimes.repository.transition_ticket(
                ticket_id, "queued", expected=("parked",), error="",
            )
            if updated is None or updated.state != "queued":
                continue
            message = self.repository.update_message(
                str(row["message_id"]), state="queued",
                evidence={
                    "ticket_state": "queued",
                    "receipt_semantics": "continued_after_successful_peer_turn",
                },
                error="",
            )
            self._event(message)
            resumed += 1
        return resumed

    def _message_kind(self, sender, request_id, value, default):
        if value is None:
            prior = self.repository.get_message_by_request(sender, str(request_id).strip()[:512]) if request_id else None
            return prior["message_kind"] if prior else default
        if not isinstance(value, str) or value not in MESSAGE_KINDS:
            raise PeerError("peer_message_kind_invalid", "message_kind must be request, notice, or result")
        return value

    async def send(
        self,
        sender_peer_id: str,
        target_peer_id: str,
        text: str,
        in_reply_to: str = "",
        delivery: str = "follow_up",
        request_id: str = "",
        *, message_kind: str | None = None, _invocation: Any = None,
    ) -> dict[str, Any]:
        sender = self.get_peer(sender_peer_id)
        target = self.get_peer(target_peer_id)
        evidence = {
            "sender_display_name": str(sender.get("display_name") or sender_peer_id),
            "target_display_name": str(target.get("display_name") or target_peer_id),
            "sender_kind": str(sender["kind"]),
        }
        message_kind = self._message_kind(sender_peer_id, request_id, message_kind, "request")
        if _invocation is not None:
            invocation = {key: str(getattr(_invocation, key, "") or "") for key in (
                "chat_id", "run_id", "outer_tool_call_id", "cell_execution_id", "nested_call_id",
            )}
            if all(invocation.values()) and _native_peer_id(invocation["chat_id"]) == sender_peer_id:
                evidence["sender_invocation"] = invocation
        clean_text = _bounded_text(text)
        mode = str(delivery or "follow_up").strip().lower()
        if mode not in {"follow_up", "steer"}:
            raise PeerError("peer_delivery_invalid", "delivery must be follow_up or steer")
        if message_kind != "request" and mode == "steer":
            raise PeerError("peer_message_delivery_invalid", "Only a request can steer work")
        parent = None
        if in_reply_to:
            parent = self._message_for_peer(sender_peer_id, in_reply_to)
            if str(parent.get("target_peer_id")) != str(sender_peer_id):
                raise PeerError("peer_reply_direction_invalid", "reply target is not the current peer")
            if str(parent.get("sender_peer_id")) != str(target_peer_id):
                raise PeerError("peer_reply_target_invalid", "reply target does not match the exchange sender")
        clean_request = str(request_id or "").strip()[:512]
        message_id = (
            "peer_message_" + hashlib.sha256(
                (str(sender_peer_id) + "\0" + clean_request).encode("utf-8")
            ).hexdigest()[:32]
            if clean_request else ""
        )
        try:
            row, created = self.repository.persist_message({
                "message_id": message_id,
                "exchange_id": str((parent or {}).get("exchange_id") or ""),
                "sender_peer_id": str(sender_peer_id),
                "target_peer_id": str(target_peer_id),
                "in_reply_to": str(in_reply_to or ""),
                "content": clean_text,
                "delivery": mode,
                "state": "persisted",
                "request_id": clean_request,
                "evidence": evidence,
                "message_kind": message_kind,
            })
        except RuntimeError as exc:
            if "identity conflicts" in str(exc):
                raise PeerError(
                    "peer_request_conflict", str(exc),
                    commit_state="not_committed",
                ) from exc
            raise
        if not created:
            if requests_work(row) and target.get("kind") == "variant_chat" and row.get("state") in {
                "persisted", "queued",
            }:
                self._schedule_native_wake(
                    _native_chat_id(str(row.get("target_peer_id") or ""))
                )
            elif (
                target.get("kind") == "external_harness"
                and requests_work(row)
                and target.get("status") == "connected"
                and row.get("state") == "queued"
            ):
                callback = self._external_hooks.get(str(target.get("adapter") or ""))
                if callback is not None:
                    callback(dict(row))
            return self._sync_native_state(row)
        self._event(row)
        try:
            if target.get("kind") == "variant_chat":
                row = await self._admit_native(row)
            else:
                # Logical external peers survive presentation/transport loss.
                # Unclaimed work remains durably queued for a later lease; an
                # ambiguous claimed effect is fenced separately and never replayed.
                row = self.repository.update_message(
                    str(row["message_id"]), state="queued",
                )
                if requests_work(row) and str(target.get("status") or "") == "connected":
                    callback = self._external_hooks.get(str(target.get("adapter") or ""))
                    if callback is not None:
                        result = callback(dict(row))
                        if inspect.isawaitable(result):
                            closer = getattr(result, "close", None)
                            if callable(closer):
                                closer()
                            raise TypeError(
                                "external peer hook must schedule synchronously"
                            )
                self._event(row)
        except Exception as exc:
            row = self.repository.update_message(
                str(row["message_id"]), state="unknown",
                error=f"delivery wake failed after commit: {type(exc).__name__}: {exc}"[:1000],
            )
            self._event(row)
        await self._emit_sent(row)
        return row

    async def reply(
        self, peer_id: str, message_id: str, text: str, request_id: str = "",
        *, message_kind: str | None = None, _invocation: Any = None,
    ) -> dict[str, Any]:
        original = self._message_for_peer(peer_id, message_id)
        if str(original.get("target_peer_id")) != str(peer_id):
            raise PeerError("peer_reply_direction_invalid", "only the recipient can reply")
        return await self.send(
            peer_id,
            str(original["sender_peer_id"]),
            text,
            in_reply_to=str(original["message_id"]),
            delivery="follow_up",
            request_id=request_id,
            _invocation=_invocation,
            message_kind=self._message_kind(peer_id, request_id, message_kind, "result"),
        )

    async def wait_message(
        self, peer_id: str, message_id: str, timeout_s: float = 30.0,
    ) -> dict[str, Any]:
        message = self._message_for_peer(peer_id, message_id)
        deadline = asyncio.get_running_loop().time() + max(
            0.0, min(float(timeout_s), 30.0)
        )
        while True:
            reply = self.repository.find_reply(message_id)
            if reply is not None:
                return {
                    "status": "replied",
                    "message": self._message_for_peer(peer_id, message_id),
                    "reply": reply,
                }
            # Any still-unanswered inbound request is attention, including a
            # mutual request that committed just before this wait began. This
            # is the deadlock escape for two chats that both ask then wait.
            attention = [
                self._sync_native_state(row)
                for row in self.repository.list_messages(
                    peer_id, after=0, limit=50, direction="incoming",
                    states=("persisted", "queued", "transport_written", "observed"),
                )
                if str(row.get("message_id") or "") != str(message_id) and requests_work(row)
                and not str(row.get("in_reply_to") or "")
            ][:1]
            if attention:
                return {
                    "status": "attention", "message": message,
                    "incoming": attention[0],
                }
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return {"status": "pending", "message": self._message_for_peer(peer_id, message_id)}
            event = self._change_event
            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except TimeoutError:
                return {"status": "pending", "message": self._message_for_peer(peer_id, message_id)}

    def register_external(
        self,
        peer_id: str,
        display_name: str,
        adapter: str,
        session_id: str,
        terminal_id: str,
        process_id: str,
        process_generation: int,
        connection_epoch: int,
        capabilities: Mapping[str, Any],
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_peer = str(peer_id or "").strip()
        if not clean_peer or clean_peer.startswith("chat:"):
            raise PeerError("external_peer_id_invalid", "external peer_id is invalid")
        if not all(str(value or "").strip() for value in (adapter, session_id)):
            raise PeerError(
                "external_binding_incomplete",
                "external peer requires adapter and native session identity",
            )
        if (
            int(connection_epoch) < 1
            or (str(process_id or "").strip() and int(process_generation) < 1)
            or (not str(process_id or "").strip() and int(process_generation) < 0)
        ):
            raise PeerError("external_generation_invalid", "external generations must be positive")
        row = self.repository.register_external({
            "peer_id": clean_peer,
            "display_name": str(display_name or clean_peer)[:200],
            "adapter": str(adapter),
            "external_session_id": str(session_id),
            "terminal_id": str(terminal_id),
            "process_id": str(process_id),
            "process_generation": int(process_generation),
            "connection_epoch": int(connection_epoch),
            "capabilities": dict(capabilities or {}),
            "metadata": dict(metadata or {}),
            "status": "connected",
        })
        for message in self.repository.reconcile_external_epoch(
            clean_peer, int(connection_epoch),
        ):
            self._event(message)
        self._event(row, peer_id=clean_peer)
        return row

    def register_connection(
        self,
        connection_id: str,
        harness: str,
        native_session_id: str,
        *,
        peer_id: str = "",
        display_name: str = "",
        process_id: str = "",
        process_started_at: float = 0.0,
        runtime_id: str = "",
        runtime_pid: str = "",
        runtime_started_at: float = 0.0,
        epoch: int = 1,
        lease_seconds: float = 15.0,
        capabilities: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_connection = str(connection_id or "").strip()
        clean_harness = str(harness or "").strip().lower()
        clean_session = str(native_session_id or "").strip()
        clean_peer = str(peer_id or f"{clean_harness}:{clean_session}").strip()
        clean_runtime = str(runtime_id or "").strip()
        clean_process = str(process_id or "").strip()
        process_birth = float(process_started_at or 0)
        if not clean_connection or not clean_harness or not clean_session:
            raise PeerError(
                "peer_connection_identity_required",
                "connection_id, harness, and native_session_id are required",
            )
        if not clean_runtime:
            raise PeerError(
                "peer_runtime_identity_required",
                "runtime_id is required for delivery-owner fencing",
            )
        if not clean_process or process_birth <= 0:
            raise PeerError(
                "peer_connection_process_required",
                "validated connection process id and start time are required",
            )
        if not clean_peer or clean_peer.startswith("chat:"):
            raise PeerError("external_peer_id_invalid", "external peer_id is invalid")
        if int(epoch) < 1:
            raise PeerError("peer_connection_epoch_invalid", "connection epoch must be positive")
        duration = max(3.0, min(float(lease_seconds), 300.0))
        try:
            row, effects = self.repository.register_connection({
                "connection_id": clean_connection,
                "harness": clean_harness,
                "native_session_id": clean_session,
                "peer_id": clean_peer,
                "endpoint_adapter": PORTABLE_PEER_ADAPTER,
                "display_name": str(display_name or clean_peer)[:200],
                "process_id": clean_process,
                "process_started_at": process_birth,
                "runtime_id": clean_runtime,
                "runtime_pid": str(runtime_pid or ""),
                "runtime_started_at": max(0.0, float(runtime_started_at or 0)),
                "epoch": int(epoch),
                "lease_seconds": duration,
                "capabilities": dict(capabilities or {}),
                "metadata": dict(metadata or {}),
            })
        except (LookupError, RuntimeError, ValueError) as exc:
            raise PeerError(
                "peer_connection_conflict", str(exc), commit_state="not_committed",
            ) from exc
        self._apply_connection_effects(effects)
        self._event(row, peer_id=clean_peer)
        return row

    def touch_connection(
        self, connection_id: str, expected_epoch: int, *,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            row, effects = self.repository.touch_connection(
                connection_id, expected_epoch, metadata=metadata,
            )
        except LookupError as exc:
            raise PeerError("peer_connection_not_found", str(exc)) from exc
        except RuntimeError as exc:
            raise PeerError("peer_connection_stale", str(exc)) from exc
        self._apply_connection_effects(effects)
        # A heartbeat refreshes durable liveness but is not a user-visible
        # state transition. Avoid broadcasting every lease tick; expiry,
        # conflict resolution and explicit metadata changes remain observable.
        if metadata is not None:
            self._event(row, peer_id=str(row.get("peer_id") or ""))
        return row

    def close_connection(
        self, connection_id: str, expected_epoch: int, *, reason: str = "",
        status: str = "closed",
    ) -> dict[str, Any]:
        try:
            row, effects = self.repository.close_connection(
                connection_id, expected_epoch, reason=reason, status=status,
            )
        except LookupError as exc:
            raise PeerError("peer_connection_not_found", str(exc)) from exc
        except (RuntimeError, ValueError) as exc:
            raise PeerError("peer_connection_stale", str(exc)) from exc
        self._apply_connection_effects(effects)
        self._event(row, peer_id=str(row.get("peer_id") or ""))
        return row

    def get_connection(self, connection_id: str) -> dict[str, Any]:
        self.expire_connections()
        row = self.repository.get_connection(str(connection_id or ""))
        if row is None:
            raise PeerError(
                "peer_connection_not_found", "peer connection was not found",
            )
        return row

    def list_active_connections(
        self, *, peer_id: str = "", harness: str = "", limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self.list_connections(
            peer_id=peer_id, harness=harness, statuses=("active",), limit=limit,
        )

    def list_connections(
        self, *, peer_id: str = "", harness: str = "",
        statuses: tuple[str, ...] = (), limit: int = 100,
    ) -> list[dict[str, Any]]:
        selected = tuple(str(item or "").strip() for item in statuses if str(item or "").strip())
        invalid = sorted(set(selected) - {"active", "conflicted", "closed", "expired"})
        if invalid:
            raise PeerError(
                "peer_connection_status_invalid",
                "invalid connection status: " + ", ".join(invalid),
            )
        self.expire_connections()
        return self.repository.list_connections(
            peer_id=str(peer_id or ""), harness=str(harness or "").lower(),
            statuses=selected, limit=max(1, min(int(limit), 500)),
        )

    def expire_connections(
        self, *, now: float | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        effects = self.repository.expire_connections(now=now)
        self._apply_connection_effects(effects)
        return effects

    def claim_connection_delivery(
        self, connection_id: str, expected_epoch: int, limit: int = 20,
        *, requests_only: bool = False,
    ) -> list[dict[str, Any]]:
        self.expire_connections()
        try:
            rows = self.repository.claim_connection(
                connection_id, expected_epoch, limit=limit,
                requests_only=requests_only,
            )
        except LookupError as exc:
            raise PeerError("peer_connection_not_found", str(exc)) from exc
        except RuntimeError as exc:
            code = (
                "peer_connection_conflicted"
                if "conflicting" in str(exc) else "peer_connection_stale"
            )
            raise PeerError(code, str(exc)) from exc
        for row in rows:
            self._event(row)
        return rows

    def update_external(
        self, peer_id: str, expected_connection_epoch: int, *, status: str = "",
        session_id: str = "", terminal_id: str = "", process_id: str = "",
        process_generation: int | None = None,
        capabilities: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = self.repository.update_external(
            peer_id, expected_connection_epoch,
            {
                "status": status,
                "external_session_id": session_id,
                "terminal_id": terminal_id,
                "process_id": process_id,
                "process_generation": process_generation,
                "capabilities": capabilities,
                "metadata": metadata,
            },
        )
        self._event(row, peer_id=peer_id)
        return row

    def claim_external_delivery(
        self, adapter: str, connection_epoch: int, limit: int = 20,
        peer_id: str = "",
    ) -> list[dict[str, Any]]:
        rows = self.repository.claim_external(
            adapter=adapter, connection_epoch=connection_epoch,
            limit=limit, peer_id=peer_id,
        )
        for row in rows:
            self._event(row)
        return rows

    async def settle_external_delivery(
        self, message_id: str, connection_epoch: int, state: str,
        evidence: Mapping[str, Any] | None = None,
        reply_text: str = "", request_id: str = "", connection_id: str = "",
    ) -> dict[str, Any]:
        row = self.repository.get_message(message_id)
        if row is None:
            raise PeerError("peer_message_not_found", "peer message was not found")
        if connection_id:
            connection = self.get_connection(connection_id)
            if (
                int(connection.get("epoch") or 0) != int(connection_epoch)
                or str(connection.get("peer_id") or "") != str(row["target_peer_id"])
            ):
                raise PeerError(
                    "external_connection_stale",
                    "external connection identity or epoch changed",
                )
        else:
            target = self.get_peer(str(row["target_peer_id"]))
            if int(target.get("connection_epoch") or 0) != int(connection_epoch):
                raise PeerError("external_connection_stale", "external connection epoch changed")
        if str(row.get("state") or "") == "replied":
            return {"message": row, "reply": self.repository.find_reply(message_id)}
        if connection_id and str(row.get("claim_connection_id") or "") != str(connection_id):
            raise PeerError(
                "external_delivery_not_claimed",
                "message is not claimed by this connection lease",
            )
        if int(row.get("claim_connection_epoch") or 0) != int(connection_epoch):
            raise PeerError("external_delivery_not_claimed", "message is not claimed by this connection")
        clean_state = str(state or "").strip()
        if clean_state not in {
            "transport_written", "observed", "replied", "parked", "failed", "unknown",
        }:
            raise PeerError("external_delivery_state_invalid", "invalid external delivery state")
        if clean_state == "replied" and not str(reply_text or "").strip():
            raise PeerError(
                "external_reply_text_required",
                "replied settlement requires a durable reply body",
            )
        if reply_text:
            try:
                reply = await self.reply(
                    str(row["target_peer_id"]), message_id, reply_text,
                    request_id=request_id,
                )
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                failed = self.repository.update_message(
                    message_id, state="unknown",
                    evidence=dict(evidence or {}), clear_claim=True,
                    error=(
                        "external reply was not durably committed: "
                        f"{type(exc).__name__}: {exc}"
                    )[:1000],
                )
                self._event(failed)
                return {"message": failed, "reply": None}
            # persist_message commits the reply and parent-replied transition
            # in one SQLite transaction. Attach transport evidence only after
            # that authoritative boundary; never advertise a reply first.
            settled = self.repository.update_message(
                message_id, state="replied", evidence=dict(evidence or {}),
                clear_claim=True,
            )
            self._event(settled)
            return {"message": settled, "reply": reply}
        release_claim = clean_state in {
            "replied", "parked", "failed", "unknown",
        }
        settled = self.repository.update_message(
            message_id, state=clean_state, evidence=dict(evidence or {}),
            clear_claim=release_claim,
        )
        self._event(settled)
        return {"message": settled, "reply": None}

    async def settle_connection_delivery(
        self, connection_id: str, expected_epoch: int, message_id: str,
        state: str, evidence: Mapping[str, Any] | None = None,
        reply_text: str = "", request_id: str = "",
    ) -> dict[str, Any]:
        return await self.settle_external_delivery(
            message_id, expected_epoch, state, evidence=evidence,
            reply_text=reply_text, request_id=request_id,
            connection_id=str(connection_id or ""),
        )

    async def start(self) -> None:
        self._closed = False
        for row in self.repository.reconcile_stale_external_claims():
            self._event(row)
        for row in self.repository.pending_native():
            chat_id = _native_chat_id(str(row.get("target_peer_id") or ""))
            if chat_id:
                self._schedule_native_wake(chat_id)
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(
                self._poll_native_receipts(), name="peer-native-receipts",
            )

    async def _poll_native_receipts(self) -> None:
        connection_tick = 0
        while not self._closed:
            for row in self.repository.pending_native():
                self._sync_native_state(row)
            connection_tick += 1
            if connection_tick >= 4:
                self.expire_connections()
                connection_tick = 0
            await asyncio.sleep(0.25)

    async def shutdown(self) -> None:
        self._closed = True
        tasks = [
            task for task in [self._poll_task, *self._wake_tasks.values()]
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._wake_tasks.clear()
        self._poll_task = None
        await self._publication_tasks.cancel_all()

    async def delete_chat(self, chat_id: str) -> int:
        count = self.repository.retire_chat(chat_id)
        if count:
            self._event({"revision": self.repository.revision()}, peer_id=_native_peer_id(chat_id))
        return count


__all__ = ["PeerCommunicationService", "PeerError"]
