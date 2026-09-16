"""Accept stopped-run evidence through the existing native snapshot projection.

No action is dispatched here. Immutable receipts settle old call IDs; canonical
history and an existing-projection CAS fence publication of the derived snapshot.
"""
from __future__ import annotations

import copy
from dataclasses import asdict
import hashlib
import json
import logging
import time

from capability_broker import capability_request_fingerprint, uncertain_outer_call_outcome
from core_invariants import canonical_digest
from agent_engine.snapshot_utils import interrupted_messages_for_follow_up, pending_tool_loop_from_run_state
from model_runtime.message_graph import build_message_graph
from message_context_extents import (
    HOST_CONTEXT_EXTENTS_REVISION,
    canonical_user_overlay,
    strip_recorded_host_context,
)

_LOG = logging.getLogger(__name__)
SCHEMA = "variant1.stopped-evidence.acceptance.v1"


class EvidenceIdentityError(Exception):
    """A receipt belongs to a different action; reject the projection."""


def portable_messages(messages: list[dict]) -> list[dict]:
    """Retain visible conversation and function boundaries, never opaque provider state."""
    result = []
    for row in messages:
        role = row.get("role")
        if role not in {"user", "assistant", "tool"}:
            continue
        source = strip_recorded_host_context(row) if role == "user" else dict(row)
        content = source.get("content") or ""
        if isinstance(content, list):
            content = "\n".join(part["text"] for part in content
                                if isinstance(part, dict) and part.get("type") in {"text","input_text","output_text"}
                                and isinstance(part.get("text"), str))
        if not isinstance(content, str):
            raise ValueError("unsupported stopped message content")
        clean = {"role": role, "content": content}
        if role == "assistant" and source.get("tool_calls"):
            clean["tool_calls"] = [
                {"id": call["id"], "type": "function",
                 "function": {"name": call["function"]["name"], "arguments": call["function"]["arguments"]}}
                for call in source["tool_calls"]
            ]
        if role == "tool":
            clean["tool_call_id"] = source["tool_call_id"]
        if source.get("variant1_compaction") is True:
            clean["variant1_compaction"] = True
            if source.get("variant1_compaction_revision") == 2:
                clean["variant1_compaction_revision"] = 2
        if content or clean.get("tool_calls") or role == "tool":
            result.append(clean)
    return result


def reconcile_call(runtime, chat_id: str, run_id: str, action: dict) -> dict:
    repository = runtime.session_runtimes.repository
    name, call_id, args = action["tool"], action["id"], dict(action.get("args") or {})
    fingerprint = capability_request_fingerprint(name, args)
    outer = repository.get_outer_tool_call(chat_id, run_id, call_id)
    if outer:
        if outer["tool_name"] != name or outer["request_fingerprint"] != fingerprint:
            raise ValueError("stopped outer-call fingerprint mismatch")
        if outer["state"] in {"succeeded", "failed", "unknown_effect"}:
            outcome = copy.deepcopy(outer["outcome"])
            if outcome.get("call_id") != call_id or outcome.get("tool") != name:
                raise ValueError("stopped terminal call identity mismatch")
            return outcome

    evidence = runtime.kernel.cell_ledger.for_outer_call(chat_id, run_id, call_id)
    cells, admissions = evidence["cells"], evidence["admissions"]
    operations = runtime.work.repository.operations_for_outer_call(chat_id, run_id, call_id)
    if not outer and not cells and not admissions and not operations:
        text = "Not executed; the prior run stopped before dispatch."
        return {"tool": name, "call_id": call_id, "ok": False, "executed": False,
                "status": "cancelled_before_start", "result": text, "model_result": text}

    outcome = uncertain_outer_call_outcome(name, args, call_id)
    settled_ids = {cell.execution_id for cell in cells}
    unresolved = any(item.get("execution_id") not in settled_ids for item in admissions)
    work_settled = all(op.status in {"succeeded", "failed", "rejected", "cancelled"} for op in operations)
    if outer and name == "ipython" and len(cells) == 1 and not unresolved and work_settled:
        cell = cells[0]
        if (cell.chat_id != chat_id or cell.run_id != run_id or cell.outer_tool_call_id != call_id
                or cell.source_sha256 != hashlib.sha256(str(args.get("code") or "").encode()).hexdigest()):
            raise ValueError("stopped kernel call identity mismatch")
        try:
            raw = runtime.kernel.artifact_store.read_bytes_scoped(cell.result_ref, chat_id)
            if hashlib.sha256(raw).hexdigest() != cell.result_sha256:
                raise ValueError("cell result checksum mismatch")
            body = json.loads(raw)
            if (body.get("execution_id") != cell.execution_id or body.get("chat_id") != chat_id
                    or body.get("status") != cell.status):
                raise EvidenceIdentityError("cell result identity mismatch")
            if cell.status in {"ok", "error", "interrupted"}:
                text = str(body.get("text") or "")
                error = (body.get("error") or {}).get("message")
                if error and str(error) not in text:
                    text += "\n" + str(error)
                visible = text[:12_000]
                if len(text) > len(visible):
                    visible += f"\n[Full saved cell result: {cell.result_ref}]"
                outcome = {"tool": name, "call_id": call_id, "ok": cell.status == "ok",
                           "executed": True, "status": cell.status, "result": visible,
                           "model_result": visible, "source_chars": len(text),
                           "visible_chars": len(visible), "truncated": len(text) > 12_000,
                           "recovered_cell_result": cell.result_ref}
        except (OSError, ValueError, KeyError, TypeError):
            # A missing/corrupt result is evidence uncertainty, never evidence
            # that the already-dispatched Python cell did not run.
            pass
    if outcome["status"] == "needs_reconciliation":
        refs = {"cells": [cell.execution_id for cell in cells[:8]],
                "operations": [op.operation_id for op in operations[:8]],
                "unsettled_admission": unresolved}
        outcome["model_result"] += "\nSaved evidence: " + json.dumps(refs, separators=(",", ":"))
        outcome["result"] = outcome["model_result"]
    if outer:
        terminal = "unknown_effect" if outcome["status"] == "needs_reconciliation" else "succeeded" if outcome["ok"] else "failed"
        committed = repository.finish_outer_tool_call(chat_id=chat_id, run_id=run_id, call_id=call_id,
            request_fingerprint=fingerprint, state=terminal, outcome=outcome)
        return copy.deepcopy(committed["outcome"])
    return outcome


def validate_accepted_snapshot(snapshot, projection: dict, chat_id: str) -> list[dict]:
    graph = snapshot.state
    marker = graph.get("evidence_acceptance") or {}
    source = marker.get("source_snapshot") or {}
    extent_revision = projection.get("host_context_extents_revision")
    if extent_revision is not None and (
        type(extent_revision) is not int
        or extent_revision != HOST_CONTEXT_EXTENTS_REVISION
    ):
        raise ValueError("invalid stopped host-context extent revision")
    if (projection.get("schema") != "variant1.context-projection.native.v2"
            or marker.get("schema") != SCHEMA or snapshot.source != "chat" or snapshot.status != "cancelled"
            or graph.get("status") != "cancelled" or graph.get("chat_id") != chat_id
            or graph.get("output", {}).get("transcript_committed") is not True
            or snapshot.run_id != projection.get("snapshot", {}).get("run_id")
            or source.get("run_id") != snapshot.run_id
            or snapshot.cursor.thread_id != source.get("thread_id")
            or snapshot.parent_snapshot_id != source.get("snapshot_id")
            or snapshot.cursor.sequence != source.get("sequence", 0) + 1
            or graph.get("host_context_extents_revision") != extent_revision
            or marker.get("host_context_extents_revision") != extent_revision
            or marker.get("canonical_coverage") != projection.get("coverage")
            or projection.get("evidence") != marker):
        raise ValueError("invalid stopped-evidence projection lineage")
    messages = copy.deepcopy(graph.get("messages") or [])
    if portable_messages(messages) != messages or canonical_digest(messages) != marker.get("settlement_sha256"):
        raise ValueError("invalid stopped-evidence messages")
    build_message_graph(messages)
    return messages


def accept_stopped_evidence(runtime, chat_id: str, candidate) -> dict | None:
    """Promote once; on any missing authority, preserve the canonical conversation."""
    try:
        sessions, store = runtime.sessions, runtime.session_runtimes.snapshot_store
        if store is None or candidate is None:
            return None
        existing = sessions.get_context_projection(chat_id)
        source = store.load_cursor_sync(candidate.cursor)
        if source is None or source.source != "chat" or source.state.get("chat_id") != chat_id:
            raise ValueError("stopped snapshot ownership unavailable")
        if existing.get("purpose") == "stopped_evidence" and existing.get("snapshot", {}).get("snapshot_id") == source.cursor.snapshot_id:
            validate_accepted_snapshot(source, existing, chat_id)
            return existing["snapshot"] if sessions.context_projection_view(chat_id, existing["coverage"])["valid"] else None
        marker = source.state.get("evidence_acceptance")
        if marker:
            if canonical_digest(existing) != marker.get("prior_projection_sha256"):
                return None  # A later projection won; never replace it on retry.
            accepted = source  # Recover a crash after derived snapshot, before pointer CAS.
            covered = marker["canonical_coverage"]
        else:
            receipt = sessions.get_last_run_receipt(chat_id)
            if not (receipt.get("run_id") == source.run_id and receipt.get("status") == "cancelled" and receipt.get("settled")):
                return None
            covered = sessions.get_stopped_context_coverage(chat_id, source.run_id)
            if not covered:
                return None  # Legacy Stop without coverage cannot replace a guessed prefix.
            original = sessions.canonical_context(chat_id, head_node_id=covered["head_node_id"])
            if original.get("cursor") != covered:
                return None
            stop_reply = original.get("messages", [])[-1:]
            if not stop_reply or stop_reply[0].get("role") != "assistant":
                return None
            pending = pending_tool_loop_from_run_state(source.state)
            actions = (pending or {}).get("actions", [])
            outcomes = [reconcile_call(runtime, chat_id, source.run_id, action) for action in actions]
            messages = interrupted_messages_for_follow_up(
                source.state, outcomes=outcomes)
            messages = canonical_user_overlay(
                messages,
                list(original.get("messages") or []),
                extent_revision=source.state.get(
                    "host_context_extents_revision"
                ),
            )
            messages = portable_messages(messages)
            if messages[-1:] != stop_reply:
                messages.extend(stop_reply)
            build_message_graph(messages)
            marker = {"schema": SCHEMA, "source_snapshot": {**asdict(source.cursor), "run_id": source.run_id},
                       "canonical_coverage": covered, "settlement_sha256": canonical_digest(messages),
                       "prior_projection_sha256": canonical_digest(existing),
                       "call_ids": [action["id"] for action in actions],
                       "host_context_extents_revision": HOST_CONTEXT_EXTENTS_REVISION}
            state = copy.deepcopy(source.state)
            state.update(
                messages=messages,
                status="cancelled",
                evidence_acceptance=marker,
                host_context_extents_revision=HOST_CONTEXT_EXTENTS_REVISION,
                updated_at=time.time(),
            )
            state["task"] = {**state.get("task", {}), "status": "cancelled"}
            state["output"] = {**state.get("output", {}), "snapshot_terminal_status": "cancelled",
                               "completion_status": "cancelled", "transcript_committed": True}
            cursor = store.commit_boundary_sync(state, completed_node="finalize", next_node="end",
                                                expected_head_sequence=source.cursor.sequence)
            accepted = store.load_cursor_sync(cursor)
        view = sessions.context_projection_view(chat_id, covered)
        if not view["valid"]:
            return None
        reference = {**asdict(accepted.cursor), "run_id": accepted.run_id}
        if accepted.state.get("host_context_extents_revision") == HOST_CONTEXT_EXTENTS_REVISION:
            reference["host_context_extents_revision"] = HOST_CONTEXT_EXTENTS_REVISION
        projection = {"schema": "variant1.context-projection.native.v2", "purpose": "stopped_evidence",
                       "snapshot": reference, "coverage": covered, "evidence": marker}
        if reference.get("host_context_extents_revision") == HOST_CONTEXT_EXTENTS_REVISION:
            projection["host_context_extents_revision"] = HOST_CONTEXT_EXTENTS_REVISION
        validate_accepted_snapshot(accepted, projection, chat_id)
        if sessions.set_native_context_projection(chat_id, reference, source_cursor=covered, model_route={},
                evidence=marker, expected_projection=existing, expected_head_cursor=view["cursor"]):
            return reference
    except Exception:
        _LOG.exception("stopped evidence was not accepted; retaining canonical conversation")
    return None
