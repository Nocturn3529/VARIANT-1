"""Canonical coverage for disposable model projections; no model execution."""
from __future__ import annotations

import hashlib
import json
from typing import Mapping


def history_rows(conn, conversation_id: str, head_node_id: str) -> list:
    if not head_node_id:
        return []
    return conn.execute(
        "WITH RECURSIVE ancestry(node_id) AS ("
        " SELECT ? UNION SELECT e.from_node_id FROM conversation_edge e "
        " JOIN ancestry a ON e.to_node_id=a.node_id "
        " WHERE e.conversation_id=? AND e.from_node_id IS NOT NULL"
        ") SELECT DISTINCT n.node_id,n.role,n.content_json,n.metadata_json,n.created_at "
        "FROM conversation_node n JOIN ancestry a ON a.node_id=n.node_id "
        "WHERE n.conversation_id=? ORDER BY n.created_at,n.node_id",
        (head_node_id, conversation_id, conversation_id),
    ).fetchall()


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
