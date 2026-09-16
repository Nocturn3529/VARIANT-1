"""ASTB Operate object and durable peer/message handle routing."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from capability_broker import current_capability_invocation
from object_api import dispatch_object, register_object_tool
from tools import ToolError
from work_fabric.handles import remote_handle_envelope

_KIND = {"type": "string", "required": False, "enum": ["request", "notice", "result"],
         "description": "Requests ask for work; notices/results do not automatically start a new turn."}

PEER_OBJECT_METHODS: tuple[dict[str, Any], ...] = (
    {
        "name": "list", "description": "List compact native-chat and external-harness peers.",
        "effect_class": "read",
        "params": {
            "kind": {"type": "string", "required": False, "enum": ["variant_chat", "external_harness"]},
            "status": {"type": "string", "required": False},
            "limit": {"type": "integer", "required": False, "minimum": 1, "maximum": 500},
        },
    },
    {
        "name": "get", "description": "Return a bound peer handle.",
        "effect_class": "read",
        "params": {"peer_id": {"type": "string", "required": True}},
    },
    {
        "name": "send", "description": "Persist an attributed peer message; default kind is request. Return immediately.",
        "effect_class": "external_side_effect",
        "params": {
            "target_peer_id": {"type": "string", "required": True},
            "text": {"type": "string", "required": True},
            "in_reply_to": {"type": "string", "required": False},
            "message_kind": dict(_KIND),
            "delivery": {"type": "string", "required": False, "enum": ["follow_up", "steer"]},
            "request_id": {"type": "string", "required": False},
        },
    },
    {
        "name": "inbox", "description": "Read this chat's incoming, outgoing, or combined peer messages.",
        "effect_class": "read",
        "params": {
            "after": {"type": "integer", "required": False, "minimum": 0},
            "limit": {"type": "integer", "required": False, "minimum": 1, "maximum": 100},
            "direction": {"type": "string", "required": False, "enum": ["incoming", "outgoing", "all"]},
        },
    },
    {
        "name": "inspect_message", "description": "Return a bound message handle by canonical message ID.",
        "effect_class": "read",
        "params": {"message_id": {"type": "string", "required": True}},
    },
    {
        "name": "inspect_request", "description": "Reconcile a lost send acknowledgement by sender-scoped request ID.",
        "effect_class": "read",
        "params": {"request_id": {"type": "string", "required": True}},
    },
    {
        "name": "reply", "description": "Reply with preserved exchange routing; default kind is result. Use request to ask for further work.",
        "effect_class": "external_side_effect",
        "params": {
            "message_id": {"type": "string", "required": True},
            "text": {"type": "string", "required": True},
            "message_kind": dict(_KIND),
            "request_id": {"type": "string", "required": False},
        },
    },
)

PEER_HANDLE_METHODS: tuple[dict[str, Any], ...] = (
    {"name": "refresh", "description": "Refresh this peer descriptor.", "params": [], "returns": "peer"},
    {"name": "inspect", "description": "Inspect current peer identity and capabilities.", "params": [], "returns": "dict"},
    {
        "name": "send", "description": "Persist and send a message to this peer.",
        "params": [
            {"name": "text", "type": "string", "required": True},
            {"name": "message_kind", **_KIND},
            {"name": "delivery", "type": "string", "required": False, "default": "follow_up"},
            {"name": "request_id", "type": "string", "required": False, "default": ""},
        ],
        "returns": "message",
    },
    {
        "name": "inbox", "description": "Read exchanges between this chat and the bound peer.",
        "params": [
            {"name": "after", "type": "int", "required": False, "default": 0},
            {"name": "limit", "type": "int", "required": False, "default": 50},
        ],
        "returns": "dict",
    },
)

MESSAGE_HANDLE_METHODS: tuple[dict[str, Any], ...] = (
    {"name": "refresh", "description": "Refresh this durable message.", "params": [], "returns": "message"},
    {"name": "inspect", "description": "Inspect complete message and delivery evidence.", "params": [], "returns": "dict"},
    {
        "name": "reply", "description": "Reply to this incoming message.",
        "params": [
            {"name": "text", "type": "string", "required": True},
            {"name": "message_kind", **_KIND},
            {"name": "request_id", "type": "string", "required": False, "default": ""},
        ],
        "returns": "message",
    },
    {
        "name": "wait", "description": "Wait briefly for a reply or new inbound request; never cancels peer work.",
        "params": [{"name": "timeout_s", "type": "number", "required": False, "default": 30.0}],
        "returns": "dict",
    },
)


def _service(runtime_provider):
    runtime = runtime_provider()
    service = getattr(runtime, "peers", None)
    if service is None:
        raise ToolError("peer communication runtime is unavailable")
    return service


def _peer_handle(service, context, row: Mapping[str, Any]) -> dict[str, Any]:
    generation = max(1, int(row.get("connection_epoch") or 1))
    return remote_handle_envelope(
        service="peers", kind="peer", handle_id=str(row.get("peer_id") or ""),
        generation=generation, revision=max(0, int(row.get("revision") or 0)),
        metadata={
            "peer_id": str(row.get("peer_id") or ""),
            "display_name": str(row.get("display_name") or "")[:200],
            "peer_kind": str(row.get("kind") or ""),
            "status": str(row.get("status") or ""),
            "capabilities": dict(row.get("capabilities") or {}),
        },
        methods=PEER_HANDLE_METHODS,
        broker=service.host.require_runtime().broker,
        context=context,
    )


def _message_handle(service, context, row: Mapping[str, Any]) -> dict[str, Any]:
    return remote_handle_envelope(
        service="peers", kind="message", handle_id=str(row.get("message_id") or ""),
        generation=1, revision=max(0, int(row.get("revision") or 0)),
        metadata={
            "message_id": str(row.get("message_id") or ""),
            "exchange_id": str(row.get("exchange_id") or ""),
            "sender_peer_id": str(row.get("sender_peer_id") or ""),
            "target_peer_id": str(row.get("target_peer_id") or ""),
            "state": str(row.get("state") or ""),
            "message_kind": str(row.get("message_kind") or "request"),
            "content_preview": str(row.get("content") or "")[:1_000],
            "sequence": int(row.get("sequence") or 0),
            "request_id": str(row.get("request_id") or ""),
        },
        methods=MESSAGE_HANDLE_METHODS,
        broker=service.host.require_runtime().broker,
        context=context,
    )


def _message_page(service, context, page: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **dict(page),
        "messages": [
            _message_handle(service, context, row)
            for row in page.get("messages") or ()
            if isinstance(row, Mapping)
        ],
    }


async def _handle_router(
    runtime_provider, context, identity, method, arguments, *, control_only=False,
):
    service = _service(runtime_provider)
    caller = "chat:" + str(context.chat_id)
    kind = str(identity.get("kind") or "")
    handle_id = str(identity.get("id") or "")
    args = dict(arguments or {})
    if kind == "peer":
        row = service.get_peer(handle_id)
        expected_generation = max(1, int(row.get("connection_epoch") or 1))
        if method != "refresh" and int(identity.get("generation") or 0) != expected_generation:
            raise ToolError("stale peer handle; call refresh()")
        if control_only:
            return (
                method == "send"
                and set(args).issubset({"text", "delivery", "request_id", "message_kind"})
                and isinstance(args.get("text"), str)
                and bool(args["text"].strip())
            )
        if method == "refresh":
            if args:
                raise ToolError("peers.peer.refresh takes no arguments")
            return _peer_handle(service, context, row)
        if method == "inspect":
            if args:
                raise ToolError("peers.peer.inspect takes no arguments")
            return row
        if method == "send":
            unknown = sorted(set(args) - {"text", "delivery", "request_id", "message_kind"})
            if unknown or not str(args.get("text") or "").strip():
                raise ToolError("peers.peer.send needs text and optional delivery/request_id")
            sent = await service.send(
                caller, handle_id, str(args["text"]),
                delivery=str(args.get("delivery") or "follow_up"),
                request_id=str(args.get("request_id") or ""),
                _invocation=context,
                message_kind=args.get("message_kind"),
            )
            return _message_handle(service, context, sent)
        if method == "inbox":
            unknown = sorted(set(args) - {"after", "limit"})
            if unknown:
                raise ToolError("peers.peer.inbox accepts after and limit")
            return _message_page(service, context, service.history(
                caller, handle_id, after=int(args.get("after") or 0),
                limit=int(args.get("limit") or 50),
            ))
        raise ToolError(f"unsupported peers.peer method: {method}")
    if kind == "message":
        row = service.inspect_message(caller, handle_id)
        if control_only:
            return (
                method == "reply"
                and set(args).issubset({"text", "request_id", "message_kind"})
                and isinstance(args.get("text"), str)
                and bool(args["text"].strip())
            )
        if method == "refresh":
            if args:
                raise ToolError("peers.message.refresh takes no arguments")
            return _message_handle(service, context, row)
        if method == "inspect":
            if args:
                raise ToolError("peers.message.inspect takes no arguments")
            return row
        if method == "reply":
            unknown = sorted(set(args) - {"text", "request_id", "message_kind"})
            if unknown or not str(args.get("text") or "").strip():
                raise ToolError("peers.message.reply needs text and optional request_id")
            reply = await service.reply(
                caller, handle_id, str(args["text"]),
                request_id=str(args.get("request_id") or ""),
                _invocation=context,
                message_kind=args.get("message_kind"),
            )
            return _message_handle(service, context, reply)
        if method == "wait":
            unknown = sorted(set(args) - {"timeout_s"})
            if unknown:
                raise ToolError("peers.message.wait accepts timeout_s")
            return await service.wait_message(
                caller, handle_id, timeout_s=float(args.get("timeout_s", 30.0)),
            )
        raise ToolError(f"unsupported peers.message method: {method}")
    raise ToolError("unsupported peers handle kind")


def register_peers_tool(registry: Any, runtime_provider: Any, host: Any) -> None:
    if registry.get("peers") is not None:
        return

    async def peers(args: dict[str, Any]):
        invocation = current_capability_invocation()
        if invocation is None or not invocation.chat_id:
            raise ToolError("peers require an active admitted Python cell")
        service = _service(runtime_provider)
        caller = "chat:" + str(invocation.chat_id)
        operation = str(args.get("operation") or "")

        async def send(payload):
            row = await service.send(
                caller, str(payload.get("target_peer_id") or ""),
                str(payload.get("text") or ""),
                in_reply_to=str(payload.get("in_reply_to") or ""),
                delivery=str(payload.get("delivery") or "follow_up"),
                request_id=str(payload.get("request_id") or ""),
                _invocation=invocation,
                message_kind=payload.get("message_kind"),
            )
            return _message_handle(service, invocation, row)

        async def reply(payload):
            row = await service.reply(
                caller, str(payload.get("message_id") or ""),
                str(payload.get("text") or ""),
                request_id=str(payload.get("request_id") or ""),
                _invocation=invocation,
                message_kind=payload.get("message_kind"),
            )
            return _message_handle(service, invocation, row)

        handlers = {
            "list": lambda payload: [
                _peer_handle(service, invocation, row)
                for row in service.list_peers(
                    kind=str(payload.get("kind") or ""),
                    status=str(payload.get("status") or ""),
                    limit=int(payload.get("limit") or 100),
                )
            ],
            "get": lambda payload: _peer_handle(
                service, invocation,
                service.get_peer(str(payload.get("peer_id") or "")),
            ),
            "send": send,
            "inbox": lambda payload: _message_page(
                service, invocation,
                service.inbox(
                    caller, after=int(payload.get("after") or 0),
                    limit=int(payload.get("limit") or 50),
                    direction=str(payload.get("direction") or "incoming"),
                ),
            ),
            "inspect_message": lambda payload: _message_handle(
                service, invocation,
                service.inspect_message(
                    caller, str(payload.get("message_id") or ""),
                ),
            ),
            "inspect_request": lambda payload: _message_handle(
                service, invocation,
                service.inspect_request(
                    caller, str(payload.get("request_id") or ""),
                ),
            ),
            "reply": reply,
        }
        return await dispatch_object(
            PEER_OBJECT_METHODS, args, api_name="peers", handlers=handlers,
        )

    register_object_tool(
        registry,
        name="peers",
        description=(
            "Discover independent native chats and connected harness sessions; "
            "send attributed durable messages and explicit replies without merging context."
        ),
        methods=PEER_OBJECT_METHODS,
        handler=peers,
        category="session_infrastructure",
        schema_revision="variant1.peers.v2",
        handler_revision="variant1.peers-handler.v2",
        may_return_secrets=False,
    )
    # A reply/send can resolve a peer wait already occupying the ordinary
    # single-capability lane. Keep bounded waits on the ordinary lane and admit
    # only these non-blocking message writes through the reserved control lane.
    root_tool = registry.get("peers")
    if root_tool is not None:
        root_tool.control_admission = lambda _context, args: (
            str((args or {}).get("operation") or "") in {"send", "reply"}
        )

    routers = getattr(host, "remote_handle_routers", None)
    if routers is None:
        routers = {}
        setattr(host, "remote_handle_routers", routers)
    routers["peers"] = (
        lambda context, identity, method, arguments:
        _handle_router(
            runtime_provider, context, identity, method, dict(arguments or {}),
        )
    )
    routers["peers"].control_admission = (
        lambda context, identity, method, arguments:
        _handle_router(
            runtime_provider, context, identity, method,
            dict(arguments or {}), control_only=True,
        )
    )


__all__ = [
    "MESSAGE_HANDLE_METHODS", "PEER_HANDLE_METHODS", "PEER_OBJECT_METHODS",
    "register_peers_tool",
]
