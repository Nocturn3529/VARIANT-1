"""Bounded child-set observation over the canonical child manager."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
from collections.abc import Mapping
from contextlib import closing

from tools import ToolError


class ChildObservations:
    @staticmethod
    def _wait_seen(parent: str, after_cursor: str) -> dict[str, str]:
        if after_cursor == "":
            return {}
        try:
            if not isinstance(after_cursor, str) or len(after_cursor) > 131072:
                raise ValueError("cursor exceeds its bound")
            payload = json.loads(base64.b64decode(after_cursor.encode("ascii"), altchars=b"-_", validate=True))
            if not isinstance(payload, dict):
                raise ValueError("cursor must be an object")
            seen = payload["seen"]
            if (payload.get("schema") != "variant1.child-wait-cursor.v1"
                    or payload.get("parent") != parent or not isinstance(seen, dict)
                    or len(seen) > 100 or any(not isinstance(k, str) or len(k) > 512
                    or not isinstance(v, str) or len(v) != 64 for k, v in seen.items())):
                raise ValueError("cursor scope or shape mismatch")
            return seen
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise ToolError("children.wait requires a valid cursor from this parent chat") from exc

    def _wait_snapshot(self, parent: str, targets, seen: dict[str, str], limit: int) -> dict:
        selectors = None
        if targets is not None:
            if not isinstance(targets, (list, tuple)) or len(targets) > 100:
                raise ToolError("children.wait targets must be at most 100 child IDs or handles")
            selectors = {}
            for target in targets:
                generation = None
                if isinstance(target, Mapping):
                    if target.get("service") != "children" or target.get("kind") != "child":
                        raise ToolError("children.wait accepts only child handles")
                    generation = target.get("generation")
                    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
                        raise ToolError("children.wait handle generation is invalid")
                    target = target.get("id")
                if not isinstance(target, str) or not target.strip() or len(target) > 512:
                    raise ToolError("children.wait target must be a child ID or handle")
                clean = target.strip()
                if (clean in selectors and selectors[clean] is not None
                        and generation is not None and selectors[clean] != generation):
                    raise ToolError("children.wait targets disagree on handle generation")
                selectors[clean] = generation if generation is not None else selectors.get(clean)
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute("BEGIN")
            if selectors is None:
                rows = conn.execute(
                    "SELECT * FROM astb_child_handle WHERE parent_chat_id=? AND deletion_state<>'deleted' "
                    "ORDER BY created_at DESC,child_id LIMIT ?", (parent, limit + 1),
                ).fetchall()
                truncated = len(rows) > limit
                rows = rows[:limit]
                roster_total = conn.execute(
                    "SELECT COUNT(*) FROM astb_child_handle WHERE parent_chat_id=? AND deletion_state<>'deleted'",
                    (parent,),
                ).fetchone()[0]
            else:
                rows, truncated = [], False
                for child_id, generation in selectors.items():
                    row = conn.execute("SELECT * FROM astb_child_handle WHERE child_id=?", (child_id,)).fetchone()
                    if row is None or str(row["parent_chat_id"]) != parent:
                        raise ToolError("unknown child handle for this parent")
                    if generation is not None and generation != max(1, int(row["run_generation"] or 1)):
                        raise ToolError("stale child handle in children.wait; call refresh()")
                    rows.append(row)
                roster_total = len(rows)
            revision = self._clock_revision(conn)
        items, updates, fingerprints = [], [], {}
        settled = True
        report_remaining = 16000
        for row in rows:
            public = self._public(row)
            child_id = str(row["child_id"])
            attention = (row["status"] == "interrupted" or row["deletion_state"] == "failed")
            terminal = row["status"] in {"completed", "failed", "cancelled"} or row["deletion_state"] == "deleted"
            settled = settled and (terminal or attention)
            identity = [row[key] for key in ("run_generation", "status", "result_text", "artifact_ref", "error", "terminal_reason", "outcome_json")] + [attention, terminal]
            fingerprint = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
            fingerprints[child_id] = fingerprint
            items.append({key: public.get(key) for key in (
                "child_id", "child_chat_id", "parent_chat_id", "name", "run_generation", "status",
                "deletion_state", "work_job_id", "started_at", "completed_at", "updated_at",
            )} | {"terminal": terminal, "attention": attention,
                  "result_available": bool(public.get("reported_text") or public.get("artifact_ref"))})
            if (terminal or attention) and seen.get(child_id) != fingerprint:
                text = str(public.get("reported_text") or "")
                shown = text[:min(2000, report_remaining)]
                report_remaining -= len(shown)
                updates.append({"child_id": child_id, "parent_chat_id": parent,
                    "run_generation": public["run_generation"], "status": public["status"],
                    "deletion_state": public.get("deletion_state"), "reported_text": shown,
                    "report_truncated": len(text) > len(shown), "artifact_ref": public.get("artifact_ref"),
                    "error": str(public.get("error") or "")[:300], "outcome": public.get("outcome"),
                    "terminal_reason": str(public.get('terminal_reason') or '')[:80],
                    "error_truncated": len(str(public.get("error") or "")) > 300,
                    "provenance": "committed_child_outcome_self_report_not_independent_verification"})
        cursor = base64.urlsafe_b64encode(json.dumps({"schema": "variant1.child-wait-cursor.v1",
            "parent": parent, "seen": fingerprints}, sort_keys=True, separators=(",", ":")).encode()).decode("ascii")
        return {"schema": "variant1.child-wait.v1", "parent_chat_id": parent,
                "items": items, "updates": updates, "cursor": cursor, "revision": revision,
                "truncated": truncated, "settled": settled, "selected_count": len(items),
                "roster_total": roster_total, "selection": "explicit" if selectors is not None else "newest_roster"}

    async def wait(self, parent_chat_id: str, *, targets=None, timeout_s: float = 0,
                   after_cursor: str = "", limit: int = 20) -> dict:
        """One observation budget; committed changes wake without polling a model."""
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not math.isfinite(timeout_s):
            raise ToolError("children.wait timeout_s must be a finite number")
        if not 0 <= timeout_s <= 30:
            raise ToolError("children.wait timeout_s must be between 0 and 30")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ToolError("children.wait limit must be between 1 and 100")
        parent = str(parent_chat_id)
        seen = self._wait_seen(parent, after_cursor)
        loop, event = asyncio.get_running_loop(), asyncio.Event()
        subscription = (loop, event)
        with self._change_lock:
            self._wait_subscribers.setdefault(parent, set()).add(subscription)
        deadline = loop.time() + timeout_s
        try:
            while True:
                event.clear()
                snapshot = self._wait_snapshot(parent, targets, seen, limit)
                if snapshot["updates"] or snapshot["settled"] or timeout_s == 0:
                    return snapshot | {"reason": "update" if snapshot["updates"] else "snapshot"}
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return snapshot | {"reason": "timeout"}
                try:
                    await asyncio.wait_for(event.wait(), remaining)
                except asyncio.TimeoutError:
                    return self._wait_snapshot(parent, targets, seen, limit) | {"reason": "timeout"}
        finally:
            with self._change_lock:
                subscriptions = self._wait_subscribers.get(parent)
                if subscriptions is not None:
                    subscriptions.discard(subscription)
                    if not subscriptions:
                        self._wait_subscribers.pop(parent, None)
