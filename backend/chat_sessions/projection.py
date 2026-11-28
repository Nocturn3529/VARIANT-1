"""Canonical coverage for disposable model projections; no model execution."""
from __future__ import annotations

import hashlib
import heapq
import json
import re
from typing import Mapping


ANCESTRY_SQL = (
    "WITH RECURSIVE ancestry(node_id) AS (SELECT ? UNION "
    "SELECT e.from_node_id FROM ancestry a CROSS JOIN conversation_edge e "
    "WHERE e.to_node_id=a.node_id AND e.conversation_id=? AND e.from_node_id IS NOT NULL) "
)


def display_origin_text(role, origin, recorded_peer, resolve_peer=None):
    """Replace host-authored input with its public transcript representation."""
    if role != "user" or not isinstance(origin, dict):
        return None
    if origin.get("kind") == "goal" and origin.get("event") == "continuation":
        return "Continuing goal"
    if origin.get("kind") == "peer":
        display = recorded_peer
        if not isinstance(display, dict) and callable(resolve_peer):
            try:
                display = resolve_peer(origin)
            except Exception:
                display = None
        content = display.get("content") if isinstance(display, dict) else None
        return content if isinstance(content, str) and content else "Peer message"
    return None


def search_terms(query: str) -> list[str]:
    return re.findall(r"[\w-]+", query, flags=re.UNICODE)[:20]


def display_excerpt(text: str, query: str, limit: int, *, terms=()) -> tuple[str, int]:
    folded = text.casefold()
    pos = folded.find(query.casefold()) if query else -1
    if pos < 0 and terms:
        pos = min((i for term in terms if (i := folded.find(term.casefold())) >= 0), default=-1)
    if pos > 0 and len(folded) != len(text):
        count = 0
        for index, char in enumerate(text):
            count += len(char.casefold())
            if count > pos:
                pos = index
                break
    offset = max(0, pos - min(160, limit // 3))
    return text[offset:offset + limit], offset


def history_order(conn, conversation_id: str, head_node_id: str, *, display_only: bool = False) -> list[str]:
    """Stable topological order. Wall time breaks ties only between independent nodes."""
    if not head_node_id:
        return []
    rows = conn.execute(
        ANCESTRY_SQL + "SELECT n.node_id,n.created_at,n.role,e.from_node_id "
        "FROM ancestry a CROSS JOIN conversation_node n ON n.node_id=a.node_id "
        "LEFT JOIN conversation_edge e ON e.to_node_id=n.node_id AND e.conversation_id=n.conversation_id "
        "WHERE n.conversation_id=?", (head_node_id, conversation_id, conversation_id))
    nodes = {}
    parents = {}
    children = {}
    for row in rows:
        node, parent = row["node_id"], row["from_node_id"]
        nodes[node] = (row["created_at"], row["role"])
        if parent:
            parents.setdefault(node, set()).add(parent)
            children.setdefault(parent, set()).add(node)
    for incoming in parents.values():
        incoming.difference_update([parent for parent in incoming if parent not in nodes])
    ready = [(stamp, node) for node, (stamp, _) in nodes.items() if not parents.get(node)]
    heapq.heapify(ready)
    ordered = []
    visited = 0
    while ready:
        _, node = heapq.heappop(ready)
        visited += 1
        if not display_only or nodes[node][1] in {"user", "assistant"}:
            ordered.append(node)
        for child in children.get(node, ()):
            parents[child].remove(node)
            if not parents[child]:
                heapq.heappush(ready, (nodes[child][0], child))
    if visited != len(nodes):
        raise ValueError("Session context ancestry contains a cycle")
    return ordered


def rows_for_ids(conn, conversation_id: str, ids: list[str], *, full: bool = False) -> list:
    if not ids:
        return []
    columns = "n.*" if full else "n.node_id,n.role,n.content_json,n.metadata_json,n.created_at"
    return conn.execute(
        f"SELECT {columns} FROM json_each(?) wanted CROSS JOIN conversation_node n "
        "ON n.node_id=wanted.value WHERE n.conversation_id=? ORDER BY wanted.key",
        (json.dumps(ids), conversation_id),
    ).fetchall()


def history_rows(conn, conversation_id: str, head_node_id: str) -> list:
    return rows_for_ids(conn, conversation_id, history_order(conn, conversation_id, head_node_id))


def digest_rows(rows: list) -> str:
    content = [(r["node_id"], r["role"], r["content_json"]) for r in rows]
    digest = hashlib.sha256()
    for chunk in json.JSONEncoder(ensure_ascii=False, separators=(",", ":")).iterencode(content):
        digest.update(chunk.encode("utf-8"))
    return digest.hexdigest()


def coverage(owner, rows: list) -> dict:
    return {"conversation_id": owner["conversation_id"], "branch_id": owner["branch_id"],
            "head_node_id": owner["head_node_id"] or "", "prefix_sha256": digest_rows(rows),
            "node_count": len(rows)}


def verified_prefix(conn, owner, expected: Mapping) -> list | None:
    if (not isinstance(expected, Mapping)
            or expected.get("conversation_id") != owner["conversation_id"]
            or expected.get("branch_id") != owner["branch_id"]):
        return None
    head = str(expected.get("head_node_id") or "")
    if head != str(owner["head_node_id"] or ""):
        return None
    rows = history_rows(conn, owner["conversation_id"], head)
    if digest_rows(rows) != expected.get("prefix_sha256"):
        return None
    return rows
