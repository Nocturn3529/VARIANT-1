"""Correlated UI requests into the canonical peer communication service."""
from __future__ import annotations

import inspect


def _text(value, field, *, optional=False):
    if not isinstance(value, str) or (not optional and not value.strip()):
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip() if field != "text" else value


def _number(value, default, maximum):
    if value is None:
        return default
    if type(value) is not int or value < 0:
        raise ValueError("cursor and limit must be non-negative integers")
    return min(value, maximum)


async def _resolved(value):
    return await value if inspect.isawaitable(value) else value


def register(on):
    @on("peers:list", "peers:get", "peers:inbox", "peers:send", "peers:reply", "peers:inspect",
        "peers:grok:status", "peers:grok:setup", "peers:grok:sessions", "peers:grok:launch", "peers:grok:connect", "peers:grok:permission", "peers:grok:delivery")
    async def peer_command(host, websocket, session, message):
        operation = str(message.get("type") or "").removeprefix("peers:")
        chat_id = str(message.get("chat_id") or "").strip()
        request_id = str(message.get("request_id") or "").strip()
        reply = {"type": "peers:result", "chat_id": chat_id, "request_id": request_id,
                 "operation": operation, "ok": False}
        entered_service = False
        try:
            _text(message.get("chat_id"), "chat_id")
            _text(message.get("request_id"), "request_id")
            if len(request_id) > 512:
                raise ValueError("request_id exceeds 512 characters")
            runtime = host.require_runtime()
            if not runtime.sessions.has_session(chat_id):
                raise ValueError("Unknown chat")
            service = runtime.peers
            source = f"chat:{chat_id}"
            if operation == "list":
                value = service.list_peers(kind=str(message.get("kind") or ""),
                                           status=str(message.get("status") or ""),
                                           limit=_number(message.get("limit"), 100, 100))
            elif operation == "get":
                value = service.get_peer(_text(message.get("peer_id"), "peer_id"))
            elif operation == "inbox":
                direction = str(message.get("direction") or "incoming")
                if direction not in {"incoming", "outgoing", "all"}:
                    raise ValueError("Unknown inbox direction")
                value = service.inbox(source, after=_number(message.get("after"), 0, 2**63 - 1),
                                      limit=_number(message.get("limit"), 50, 100), direction=direction)
            elif operation == "inspect":
                if message.get("lookup_request_id"):
                    value = service.inspect_request(source, _text(message["lookup_request_id"], "lookup_request_id"))
                else:
                    value = service.inspect_message(source, _text(message.get("message_id"), "message_id"))
            elif operation in {"send", "reply"}:
                text = _text(message.get("text"), "text")
                target = _text(message.get("peer_id" if operation == "send" else "message_id"),
                               "peer_id" if operation == "send" else "message_id")
                delivery = str(message.get("delivery") or "follow_up")
                if delivery not in {"follow_up", "steer"}:
                    raise ValueError("delivery must be follow_up or steer")
                entered_service = True
                if operation == "send":
                    value = service.send(source, target, text, delivery=delivery, request_id=request_id,
                                         in_reply_to=str(message.get("in_reply_to") or ""),
                                         message_kind=message.get("message_kind"))
                else:
                    value = service.reply(source, target, text, request_id=request_id,
                                          message_kind=message.get("message_kind"))
            else:
                from peers.grok import get_grok_integration
                grok = get_grok_integration(host)
                entered_service = operation in {"grok:setup", "grok:launch", "grok:connect", "grok:permission"}
                if operation == "grok:status":
                    value = grok.status(chat_id)
                elif operation == "grok:setup":
                    value = grok.setup(chat_id, replace_profile_id=str(message.get("replace_profile_id") or ""))
                elif operation == "grok:sessions":
                    value = grok.sessions(chat_id, cursor=str(message.get("cursor") or ""))
                elif operation == "grok:launch":
                    value = grok.launch(chat_id, request_id=request_id, cwd=str(message.get("cwd") or ""), session_id=str(message.get("session_id") or ""))
                elif operation == "grok:permission":
                    value = grok.answer_permission(chat_id, _text(message.get("binding_id"), "binding_id"),
                        _text(message.get("permission_id"), "permission_id"), _text(message.get("option_id"), "option_id"))
                elif operation == "grok:delivery":
                    entered_service = True
                    value = grok.set_delivery_mode(chat_id, _text(message.get("peer_id"), "peer_id"),
                        _text(message.get("delivery_mode"), "delivery_mode"),
                        expected=_text(message.get("expected_delivery_mode"), "expected_delivery_mode"))
                else:
                    value = grok.connect(chat_id, _text(message.get("binding_id"), "binding_id"))
            result = await _resolved(value)
            reply.update(ok=True, result={"items": result} if isinstance(result, list) else result)
        except Exception as error:
            reply["error"] = {"code": str(getattr(error, "code", "peer_request_failed")),
                              "message": str(error),
                              "commit_state": str(getattr(error, "commit_state",
                                  "unknown" if entered_service else "not_committed"))}
        # A transport failure after success cannot become a contradictory rejection.
        await websocket.send_json(reply)
