"""Peer intent and stock-Grok delivery semantics shared by service and views."""

MESSAGE_KINDS = frozenset({"request", "notice", "result"})
GROK_DELIVERY_MODES = frozenset({"inbox", "agent_context_prompt"})
SENDER_EVIDENCE_FIELDS = frozenset({
    "sender_invocation", "sender_display_name", "target_display_name", "sender_kind",
})


def requests_work(message):
    return message.get("message_kind", "request") == "request"


def grok_delivery_status(connection, preference="inbox"):
    metadata = (connection or {}).get("metadata") or {}
    available = bool(metadata.get("leader_socket"))
    mode = preference if available else "inbox"
    active = (connection or {}).get("status") == "active"
    return {
        "delivery_mode": mode,
        "preferred_delivery_mode": preference,
        "automatic_wake_available": available,
        "native_agent_origin": False,
        "live_ingress": active and mode == "agent_context_prompt",
    }


def message_envelope(row):
    evidence = row.get("evidence") or {}
    return {
        **row,
        "schema": "variant1.peer-message.v2",
        "message_kind": row.get("message_kind", "request"),
        "sender": {
            "peer_id": row["sender_peer_id"],
            "display_name": evidence.get("sender_display_name") or row["sender_peer_id"],
            "kind": evidence.get("sender_kind") or (
                "variant_chat" if row["sender_peer_id"].startswith("chat:") else "external_harness"
            ),
        },
        # Kind controls attention/wake, not permissions or the provider role.
        "origin": {"kind": "agent", "peer_id": row["sender_peer_id"],
                   "message_id": row["message_id"], "authority": "peer"},
    }
