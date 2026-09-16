"""Grok's native session adapter behind the shared peer MCP service."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
import secrets
import time

from execution_hosts import ExecutionOwner
from project_context import chat_project_context
from work_fabric.scope import WorkScope

from .bridge_discovery import atomic_json, process_matches, profile_identity
from .grok_acp import ACPError, GrokACP
from .grok_install import GrokPeerError, grok_executable, install, installed, installation_profile
from .delivery import GROK_DELIVERY_MODES, grok_delivery_status, requests_work


class GrokIntegration:
    adapter = "grok-peer-bridge"

    def __init__(self, host):
        self.host = host
        self.root = Path(host.data_dir) / "data" / "peers" / "grok"
        self.launches = {}
        for path in self.root.glob("*/binding.json"):
            try:
                row = json.loads(path.read_text("utf-8"))
                # Keep old launch/message identities as history; MCP registration
                # is now the authority for whether the native session is live.
                self.launches[row["binding_id"]] = row
            except (ValueError, KeyError, OSError):
                continue
        self.viewers = set()
        self.controllers = {}
        self.controller_locks = {}
        self.launch_locks = {}
        self.install_lock = asyncio.Lock()
        self.tasks = {}
        self.publish_tasks = set()
        self.permissions = {}
        self.permission_answers = {}
        self.activity = {}
        self.monitor = None
        self.closed = False
        self.owned_leader = None
        self.revision = int(time.time() * 1000)
        self._setup_done = False
        self._connection_signature = ()
        self.saved_sessions = {}

    @property
    def runtime(self):
        return self.host.require_runtime()

    @property
    def peers(self):
        return self.runtime.peers

    def _owner(self):
        return ExecutionOwner("service", "peer-grok:" + profile_identity(self.host.data_dir), WorkScope())

    def _save(self, row):
        atomic_json(self.root / row["binding_id"] / "binding.json", row)

    def _publish(self, binding_id=""):
        self.revision = max(self.revision + 1, int(time.time() * 1000))
        publisher = getattr(getattr(self.host, "hub", None), "broadcast", None)
        if publisher is None or self.closed:
            return
        for viewer in tuple(self.viewers):
            task = asyncio.create_task(publisher({"type": "peers:grok:changed", "chat_id": viewer,
                "binding_id": binding_id or "grok-sessions", "revision": self.revision}), name="peer-grok-publish")
            self.publish_tasks.add(task)
            task.add_done_callback(self._published)

    def _published(self, task):
        self.publish_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    def _start(self):
        if self.closed:
            return
        self.peers.register_external_hook("mcp-peer-bridge", self._wake)
        if self.monitor is None or self.monitor.done():
            self.monitor = asyncio.create_task(self._monitor(), name="peer-grok-monitor")

    def note_connection(self, connection):
        if self.closed:
            return
        self._start()
        if connection.get("harness") == "grok":
            self._publish()
            if connection.get("status") == "active":
                self._wake({"target_peer_id": connection["peer_id"]})

    def _connections(self, peer_id=""):
        return self.peers.list_connections(peer_id=peer_id, harness="grok", statuses=("active", "conflicted"), limit=500)

    def _connection(self, peer_id):
        rows = self._connections(peer_id)
        return next((row for row in rows if row.get("delivery_owner")), rows[0] if rows else None)

    def connection_status(self, connection):
        metadata = connection.get("metadata") or {}
        delivery = grok_delivery_status(connection, self.peers.repository.delivery_preference(connection["peer_id"]))
        return {"peer_id": connection["peer_id"], "native_session_id": connection["native_session_id"],
            "connection_id": connection["connection_id"], "connection_epoch": connection["epoch"],
            "status": connection["status"], "cwd": metadata.get("cwd", ""),
            **delivery,
            "origin": {"kind": "agent", "peer_id": connection["peer_id"]},
            "conflict_runtime_ids": connection.get("conflict_runtime_ids", [])}

    def _binding(self, row, viewer, connections=None):
        peer_id = row.get("peer_id") or ("grok:" + row["session_id"] if row.get("session_id") else "")
        connection = (self._connection(peer_id) if connections is None else connections.get(peer_id)) if peer_id else None
        metadata = (connection or {}).get("metadata") or {}
        status = "connected" if connection and connection["status"] == "active" else (connection or {}).get("status", "disconnected")
        delivery = grok_delivery_status(connection, self.peers.repository.delivery_preference(peer_id))
        live_ingress = delivery["live_ingress"]
        return {"viewer_chat_id": viewer, "owner_chat_id": row.get("owner_chat_id", ""),
            "terminal_chat_id": row.get("owner_chat_id", ""), "binding_id": row["binding_id"],
            "request_id": row.get("request_id", ""), "peer_id": peer_id,
            "session_id": row.get("session_id", ""), "terminal_id": row.get("terminal_id", ""),
            "status": row.get("status") if row.get("status") in {"launching", "failed"} else status,
            "error": row.get("error") or ("This session is open in multiple Grok runtimes. Messaging waits until one remains; read-only inbox inspection is available." if status == "conflicted" else ""),
            "cwd": self.activity.get(peer_id, {}).get("cwd", metadata.get("cwd", row.get("cwd", ""))),
            "connection_epoch": (connection or {}).get("epoch", 0), "process_generation": 1,
            "capabilities": {"live_ingress": live_ingress, "structured_replies": True,
                "busy_message_queueing": live_ingress, "native_agent_origin": False},
            **delivery, "agent_version": row.get("agent_version", ""),
            "mcp_ready": connection is not None, "revision": self.revision,
            "session_activity": self.activity.get(peer_id, {}).get("state", "unknown"),
            "pending_permissions": [value[0] for value in self.permissions.values() if value[0]["session_id"] == row.get("session_id")]}

    def set_delivery_mode(self, chat_id, peer_id, mode, *, expected):
        if mode not in GROK_DELIVERY_MODES or expected not in GROK_DELIVERY_MODES:
            raise GrokPeerError("peer_delivery_mode_invalid", "Choose inbox or agent_context_prompt.", commit_state="not_committed")
        self.peers.get_peer(peer_id)
        if not peer_id.startswith("grok:"):
            raise GrokPeerError("grok_peer_required", "Delivery preference requires a Grok peer.", commit_state="not_committed")
        connection = self._connection(peer_id)
        if mode == "agent_context_prompt" and not grok_delivery_status(connection)["automatic_wake_available"]:
            raise GrokPeerError("grok_wake_unavailable", "This native runtime supports inbox delivery only.", commit_state="not_committed")
        try:
            preference = self.peers.repository.set_delivery_preference(peer_id, mode, expected=expected)
        except RuntimeError as exc:
            raise GrokPeerError("peer_delivery_preference_changed", str(exc), commit_state="not_committed") from exc
        self.viewers.add(chat_id)
        self._publish()
        if mode == "agent_context_prompt":
            self._wake({"target_peer_id": peer_id})
        return {"viewer_chat_id": chat_id, **preference, **grok_delivery_status(connection, mode)}

    def status(self, chat_id):
        self.viewers.add(chat_id)
        self._start()
        connections = {}
        for connection in self._connections():
            connections.setdefault(connection["peer_id"], connection)
        items = [self._binding(row, chat_id, connections) for row in self.launches.values()]
        shown = {row["peer_id"] for row in items}
        for connection in connections.values():
            if connection["peer_id"] in shown:
                continue
            shown.add(connection["peer_id"])
            row = {"binding_id": "session_" + connection["native_session_id"], "session_id": connection["native_session_id"],
                "peer_id": connection["peer_id"]}
            items.append(self._binding(row, chat_id, connections))
        return {"available": bool(grok_executable()), "installed": installed(self.host), "adapter": self.adapter,
            "profile_id": profile_identity(self.host.data_dir), "installation_profile_id": installation_profile(), "items": items}

    async def setup(self, chat_id, *, replace_profile_id=""):
        self.viewers.add(chat_id)
        if not grok_executable():
            raise GrokPeerError("grok_not_installed", "Grok Build is not installed.", commit_state="not_committed")
        async with self.install_lock:
            result = await install(self.host, self.runtime.execution, self._owner(), chat_project_context(self.host, chat_id).cwd,
                replace_profile_id=replace_profile_id)
            self._setup_done = True
        self._start()
        self._publish()
        return {"viewer_chat_id": chat_id, **result}

    async def _new_client(self, runtime_id, socket, cwd, *, runtime_pid=0, runtime_started_at=0):
        async with self.controller_locks.setdefault(runtime_id, asyncio.Lock()):
            current = self.controllers.get(runtime_id)
            if current and not current["client"].closed:
                return current
            if runtime_pid and not process_matches(runtime_pid, runtime_started_at):
                raise GrokPeerError("grok_runtime_ended", "The native Grok runtime is no longer live.")
            if current:
                await self._close_controller(runtime_id)
            execution = self.runtime.execution
            process = await execution.start_process([grok_executable(), "agent", "--leader", "--leader-socket", socket, "stdio"],
                owner=self._owner(), cwd=cwd)
            client = GrokACP(execution, process.process_id, on_event=self._event, on_request=self._permission)
            row = {"client": client, "process_id": process.process_id, "socket": socket, "sessions": set(),
                "runtime_id": runtime_id, "runtime_pid": runtime_pid, "runtime_started_at": runtime_started_at}
            self.controllers[runtime_id] = row
            try:
                initial = await client.request("initialize", {"protocolVersion": 1, "clientCapabilities": {},
                    "clientInfo": {"name": "variant1-peer-receiver", "version": "0.2.0"}})
                if not initial.get("agentCapabilities", {}).get("sessionCapabilities", {}).get("resume") == {}:
                    # Older ACP servers may expose load only; don't silently change MCP config by falling back.
                    raise GrokPeerError("grok_resume_unsupported", "This Grok version does not expose native session resume.")
                row["version"] = initial.get("_meta", {}).get("agentVersion", "")
                return row
            except BaseException:
                await self._close_controller(runtime_id)
                raise

    async def _own_controller(self, cwd):
        async with self.launch_locks.setdefault("own-leader", asyncio.Lock()):
            if self.owned_leader and self.runtime.execution.processes.get(self.owned_leader["process_id"]).live:
                row = self.owned_leader
            else:
                socket = str(self.root / "shared-leader.sock")
                process = await self.runtime.execution.start_process([grok_executable(), "agent", "leader", "--leader-socket", socket,
                    "--relay-on-demand", "--no-auto-update", "--no-exit-on-disconnect"], owner=self._owner(), cwd=cwd)
                row = {"process_id": process.process_id, "pid": process.pid, "started_at": process.pid_started_at, "socket": socket}
                self.owned_leader = row
            actor = f"grok:{row['pid']}:{row['started_at']:.6f}"
            return await self._new_client(actor, row["socket"], cwd, runtime_pid=row["pid"], runtime_started_at=row["started_at"])

    async def sessions(self, chat_id, cursor=""):
        self.viewers.add(chat_id)
        cwd = chat_project_context(self.host, chat_id).cwd
        controller = await self._own_controller(cwd)
        result = await controller["client"].request("session/list", {"cursor": cursor} if cursor else {})
        for row in result.get("sessions", []):
            self.saved_sessions[row["sessionId"]] = row
        return {"viewer_chat_id": chat_id, "items": [{"session_id": row["sessionId"], "title": row.get("title") or row["sessionId"],
            "cwd": row.get("cwd", ""), "updated_at": row.get("updatedAt", "")} for row in result.get("sessions", [])],
            "cursor": result.get("nextCursor")}

    async def _saved_cwd(self, session_id, chat_id):
        live = self._connection("grok:" + session_id)
        if live and (live.get("metadata") or {}).get("cwd"):
            return str(Path(live["metadata"]["cwd"]).resolve())
        cursor, seen = "", set()
        while session_id not in self.saved_sessions:
            page = await self.sessions(chat_id, cursor)
            cursor = page.get("cursor") or ""
            if not cursor or cursor in seen:
                break
            seen.add(cursor)
        row = self.saved_sessions.get(session_id)
        if row is None or not row.get("cwd"):
            raise GrokPeerError("grok_session_not_found", "Grok did not find this saved session.", commit_state="not_committed")
        return str(Path(row["cwd"]).resolve())

    async def launch(self, chat_id, *, request_id, cwd="", session_id=""):
        self.viewers.add(chat_id)
        identity = "grok_binding_" + hashlib.sha256((chat_id + "\0" + request_id).encode()).hexdigest()[:24]
        async with self.launch_locks.setdefault(identity, asyncio.Lock()):
            if identity in self.launches:
                row = self.launches[identity]
                if row.get("requested_session_id", "") != session_id or row.get("requested_cwd", "") != cwd:
                    raise GrokPeerError("grok_launch_request_conflict", "This request ID already describes another launch.", commit_state="not_committed")
                return self._binding(row, chat_id)
            path = str(Path(cwd).expanduser().resolve()) if cwd else chat_project_context(self.host, chat_id).cwd
            if session_id:
                native_cwd = await self._saved_cwd(session_id, chat_id)
                if cwd and Path(path) != Path(native_cwd):
                    raise GrokPeerError("grok_session_cwd_mismatch", "Resume this Grok session in its saved directory: " + native_cwd,
                        commit_state="not_committed")
                path = native_cwd
            if not Path(path).is_dir():
                raise GrokPeerError("grok_cwd_invalid", "The working directory does not exist.", commit_state="not_committed")
            if not self._setup_done or not installed(self.host):
                await self.setup(chat_id)
            row = {"binding_id": identity, "owner_chat_id": chat_id, "request_id": request_id,
                "requested_session_id": session_id, "requested_cwd": cwd, "cwd": path, "session_id": session_id,
                "status": "launching", "error": ""}
            self.launches[identity] = row
            self._save(row)
            try:
                live = self._connection("grok:" + session_id) if session_id else None
                if live and live["status"] == "conflicted":
                    raise GrokPeerError("grok_session_conflicted", "This session is active in more than one native runtime.")
                if live:
                    controller = await self._controller(live)
                    if controller is None:
                        raise GrokPeerError("grok_session_in_use", "This session is already open in an inbox-only Grok runtime. Resume there or close it before opening another runtime.")
                else:
                    controller = await self._own_controller(path)
                if session_id:
                    await controller["client"].request("session/resume", {"sessionId": session_id, "cwd": path, "mcpServers": []})
                else:
                    created = await controller["client"].request("session/new", {"cwd": path, "mcpServers": []})
                    row["session_id"] = str(created["sessionId"])
                controller["sessions"].add(row["session_id"])
                row["peer_id"] = "grok:" + row["session_id"]
                owner = ExecutionOwner("chat", chat_id, WorkScope(chat_id=chat_id))
                terminal = await self.runtime.execution.open_terminal(owner=owner, cwd=path, profile="custom",
                    argv=[grok_executable(), "--leader", "--leader-socket", controller["socket"], "--resume", row["session_id"], "--cwd", path])
                row.update(terminal_id=terminal.terminal_id, terminal_pid=terminal.pid, status="launched", agent_version=controller.get("version", ""))
                self._save(row)
                self._start()
            except BaseException as error:
                row.update(status="failed", error=f"{type(error).__name__}: {error}"[:1000])
                self._save(row)
                if isinstance(error, asyncio.CancelledError):
                    raise
            self._publish(identity)
            return self._binding(row, chat_id)

    async def _controller(self, connection):
        metadata = connection.get("metadata") or {}
        if connection["status"] != "active" or not metadata.get("leader_socket"):
            return None
        controller = await self._new_client(connection["runtime_id"], metadata["leader_socket"], metadata["cwd"],
            runtime_pid=connection["runtime_pid"], runtime_started_at=connection["runtime_started_at"])
        if connection["native_session_id"] not in controller["sessions"]:
            # Roster validation happens before resume, so a dead session is never recreated by attachment.
            if not await self._resident(controller, connection["native_session_id"]):
                return None
            cwd = self.activity.get(connection["peer_id"], {}).get("cwd", metadata["cwd"])
            await controller["client"].request("session/resume", {"sessionId": connection["native_session_id"], "cwd": cwd, "mcpServers": []})
            controller["sessions"].add(connection["native_session_id"])
        return controller

    async def _resident(self, controller, session_id):
        if not process_matches(controller["runtime_pid"], controller["runtime_started_at"]):
            return False
        result = await controller["client"].request("_x.ai/sessions/list", {})
        rows = (result.get("result") or {}).get("sessions")
        if not isinstance(rows, list):
            raise ACPError("Grok did not return its live session roster")
        row = next((r for r in rows if r.get("sessionId") == session_id and r.get("resident")), None)
        if row:
            self.activity.setdefault("grok:" + session_id, {}).update(state=row.get("activity", "unknown"), cwd=row.get("cwd", ""))
        return row is not None

    async def connect(self, chat_id, binding_id):
        self.viewers.add(chat_id)
        row = self.launches.get(binding_id)
        if row is None and binding_id.startswith("session_"):
            row = {"binding_id": binding_id, "session_id": binding_id[len("session_"):]}
        if not row:
            raise GrokPeerError("grok_binding_not_found", "The session binding is unavailable.", commit_state="not_committed")
        connection = self._connection("grok:" + row["session_id"])
        if connection and connection["status"] == "active":
            await self._controller(connection)
        self._publish(binding_id)
        return self._binding(row, chat_id)

    def _event(self, event):
        params = event.get("params") or {}
        if (params.get("_meta") or {}).get("isReplay") or not params.get("sessionId"):
            return
        peer_id = "grok:" + params["sessionId"]
        status = self.activity.setdefault(peer_id, {})
        update = params.get("update") or {}
        if event.get("method") == "_x.ai/queue/changed":
            status.update(prompt_id=params.get("runningPromptId"), state="working" if params.get("runningPromptId") or params.get("entries") else "idle")
        elif update.get("sessionUpdate") == "user_message_chunk":
            status["state"] = "working"
        elif update.get("sessionUpdate") == "turn_completed" and (not status.get("prompt_id") or status["prompt_id"] == update.get("prompt_id")):
            status.update(state="idle", prompt_id=None)
        else:
            return
        self._publish()
        if status.get("state") == "idle":
            self._wake({"target_peer_id": peer_id})

    async def _permission(self, message):
        if message.get("method") != "session/request_permission":
            raise ACPError("Unsupported Grok client request")
        params = message.get("params") or {}
        session_id = str(params.get("sessionId") or "")
        connection = self._connection("grok:" + session_id)
        if not connection or connection["status"] != "active":
            raise ACPError("The permission request has no live session connection")
        identity = secrets.token_hex(12)
        future = asyncio.get_running_loop().create_future()
        bindings = [row["binding_id"] for row in self.launches.values() if row.get("session_id") == session_id] or ["session_" + session_id]
        row = {"permission_id": identity, "binding_id": bindings[0], "session_id": session_id,
            "connection_id": connection["connection_id"], "connection_epoch": connection["epoch"],
            "options": params.get("options", []), "tool_call": params.get("toolCall", {})}
        self.permissions[identity] = (row, future, bindings)
        self._publish()
        try:
            return await future
        finally:
            self.permissions.pop(identity, None)
            self._publish()

    def answer_permission(self, chat_id, binding_id, permission_id, option_id):
        prior = self.permission_answers.get(permission_id)
        if prior:
            if prior != (binding_id, option_id):
                raise GrokPeerError("grok_permission_conflict", "This permission already has a different answer.", commit_state="not_committed")
            return {"viewer_chat_id": chat_id, "permission_id": permission_id, "accepted": True}
        item = self.permissions.get(permission_id)
        if not item or binding_id not in item[2] or item[1].done():
            raise GrokPeerError("grok_permission_stale", "This permission is no longer waiting.", commit_state="not_committed")
        connection = self.peers.get_connection(item[0]["connection_id"])
        if connection["status"] != "active" or connection["epoch"] != item[0]["connection_epoch"]:
            raise GrokPeerError("grok_permission_stale", "The permission's native connection changed. Check the Grok session directly.", commit_state="not_committed")
        if option_id not in {option.get("optionId") for option in item[0]["options"]}:
            raise GrokPeerError("grok_permission_invalid", "Choose an offered permission option.", commit_state="not_committed")
        item[1].set_result({"outcome": {"outcome": "selected", "optionId": option_id}})
        self.permission_answers[permission_id] = (binding_id, option_id)
        if len(self.permission_answers) > 100:
            self.permission_answers.pop(next(iter(self.permission_answers)))
        return {"viewer_chat_id": chat_id, "permission_id": permission_id, "accepted": True}

    def _wake(self, message):
        peer_id = str(message.get("target_peer_id") or "")
        if self.closed or not peer_id.startswith("grok:") or not requests_work(message):
            return
        if self.peers.repository.delivery_preference(peer_id) != "agent_context_prompt":
            return
        current = self.tasks.get(peer_id)
        if current is None or current.done():
            task = asyncio.create_task(self._deliver(peer_id), name="peer-grok-delivery")
            self.tasks[peer_id] = task
            task.add_done_callback(self._delivery_done)

    @staticmethod
    def _delivery_done(task):
        if not task.cancelled() and task.exception():
            error = task.exception()
            logging.getLogger(__name__).error("Grok peer delivery failed", exc_info=(type(error), error, error.__traceback__))

    async def _deliver(self, peer_id):
        connection = self._connection(peer_id)
        if not connection or not connection.get("delivery_owner") or connection["status"] != "active":
            return
        if not self.connection_status(connection)["live_ingress"]:
            return
        if not self.peers.repository.list_messages(peer_id, direction="incoming", states=("queued",), limit=1, message_kind="request"):
            return
        controller = await self._controller(connection)
        if controller is None or not await self._resident(controller, connection["native_session_id"]):
            return
        if self.activity.get(peer_id, {}).get("state") != "idle":
            return
        # The operator may have switched to inbox while the controller attached.
        if not self.connection_status(connection)["live_ingress"]:
            return
        for message in self.peers.claim_connection_delivery(connection["connection_id"], connection["epoch"], limit=1, requests_only=True):
            async def settle(state, evidence):
                return await self.peers.settle_connection_delivery(connection["connection_id"], connection["epoch"],
                    message["message_id"], state, evidence=evidence)
            try:
                async def written():
                    await settle("transport_written", {"transport": "ACP", "delivery_mode": "agent_context_prompt", "session_id": connection["native_session_id"]})
                # Deliver actual agent content through a structured MCP tool result.
                # Stock Grok still stores this opt-in wake as user input. The
                # peer body itself is retrieved as attributed MCP data.
                text = ("VARIANT-1 peer inbox request available. "
                    f"Incoming message ID: {message['message_id']}. Read it with peers_inspect or peers_inbox. "
                    "The tool result identifies the sending agent and its request. Reply through peers_reply when useful.")
                result = await controller["client"].request("session/prompt", {"sessionId": connection["native_session_id"],
                    "prompt": [{"type": "text", "text": text}]}, timeout=None, on_written=written)
                if self.peers.inspect_message(peer_id, message["message_id"])["state"] != "replied":
                    reason = (result or {}).get("stopReason")
                    await settle("observed" if reason == "end_turn" else "parked" if reason == "cancelled" else "unknown",
                        {"transport": "ACP", "delivery_mode": "agent_context_prompt", "stop_reason": reason, "receipt_semantics": "inbox_notification_turn_completed"})
            except BaseException as error:
                try:
                    if self.peers.inspect_message(peer_id, message["message_id"])["state"] != "replied":
                        await settle("unknown", {"error": str(error)[:500]})
                except Exception:
                    pass  # Lease rollover already fences this claim; never replay.
                if isinstance(error, asyncio.CancelledError):
                    raise

    async def _monitor(self):
        while not self.closed:
            try:
                rows = self._connections()
                signature = tuple(sorted((row["connection_id"], row["epoch"], row["status"]) for row in rows))
                if signature != self._connection_signature:
                    self._connection_signature = signature
                    self._publish()
                for row in rows:
                    if not process_matches(row["process_id"], row["process_started_at"]) or not process_matches(row["runtime_pid"], row["runtime_started_at"]):
                        self.peers.close_connection(row["connection_id"], row["epoch"], reason="native process ended")
                        self._publish()
                    elif row["status"] == "active":
                        self._wake({"target_peer_id": row["peer_id"]})
                actors = {row["runtime_id"] for row in self._connections()}
                for actor, controller in list(self.controllers.items()):
                    if not process_matches(controller["runtime_pid"], controller["runtime_started_at"]):
                        await self._close_controller(actor)
                        continue
                    owned = self.owned_leader and controller["runtime_pid"] == self.owned_leader["pid"] and controller["runtime_started_at"] == self.owned_leader["started_at"]
                    if actor in actors or owned:
                        controller.pop("orphaned_at", None)
                        continue
                    since = controller.setdefault("orphaned_at", time.monotonic())
                    if time.monotonic() - since >= 5:
                        await self._close_controller(actor)
            except Exception:
                logging.getLogger(__name__).exception("Grok peer lifecycle check failed")
            await asyncio.sleep(1)

    async def _close_controller(self, actor):
        row = self.controllers.pop(actor, None)
        if row:
            await row["client"].close()
            await self.runtime.execution.stop_process(row["process_id"], force=True)

    async def shutdown(self):
        self.closed = True
        for task in [self.monitor, *self.tasks.values()]:
            if task:
                task.cancel()
        await asyncio.gather(*(task for task in [self.monitor, *self.tasks.values()] if task), return_exceptions=True)
        for actor in list(self.controllers):
            try:
                await self._close_controller(actor)
            except Exception:
                logging.getLogger(__name__).exception("Grok controller cleanup failed")
        await asyncio.gather(*self.publish_tasks, return_exceptions=True)


def get_grok_integration(host):
    current = getattr(host, "grok_peer_integration", None)
    if current is None:
        current = GrokIntegration(host)
        host.grok_peer_integration = current
    return current
