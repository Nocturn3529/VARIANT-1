"""Per-chat mutation write authority for the single VARIANT-1 action surface."""

from __future__ import annotations

from typing import Any

from .profiles import ACTION_SURFACE, is_action_surface
from .support import UnsupportedModelRoute


class MutationAuthorityController:
    """Control reversible per-chat mutation write authority.

    Runtime identity remains pinned; only write authority changes.
    """

    def __init__(self, host: Any, catalog_service: Any):
        self.host = host
        self.catalog_service = catalog_service

    @property
    def runtimes(self):
        return self.host.require_runtime().session_runtimes

    def mutation_toggle_status(
        self,
        chat_id: str,
        *,
        provider: str,
        model: str,
        adapter: str,
    ) -> dict[str, Any]:
        record = self.runtimes.ensure_runtime(str(chat_id))
        current = str(record.identity.action_surface or "")
        enabled = bool(record.mutation_write_enabled)
        revision = int(record.mutation_authority_revision or 0)
        route = {
            "provider": str(provider or ""),
            "model": str(model or ""),
            "adapter": str(adapter or ""),
        }
        if not is_action_surface(current):
            reason = (
                f"This chat uses the retired action surface {current!r}. "
                "Start a new VARIANT-1 chat to use tools or mutation."
            )
            return {
                "schema": "variant1.astb.mutation-toggle.v1",
                "chat_id": str(chat_id),
                "enabled": False,
                "available": False,
                "locked": True,
                "reason": reason,
                "current_profile": current,
                "target_profile": "",
                "route": route,
                "authority_revision": revision,
                "effective_enabled": False,
                "blockers": [{
                    "code": "unsupported_legacy_surface",
                    "message": reason,
                }],
            }

        blockers: list[dict[str, str]] = []
        creation_marker = str(record.creation_saga_state or "")
        if not enabled and creation_marker.startswith(("child:", "worker:")):
            blockers.append({
                "code": "runtime_restricted",
                "message": (
                    "Mutation authoring is unavailable to child and unattended "
                    "runtimes."
                ),
            })
        if self.runtimes.is_busy(str(chat_id)):
            blockers.append({
                "code": "active_run",
                "message": (
                    "Finish or cancel the active turn before changing mutation "
                    "authority."
                ),
            })
        controls = self.host.require_runtime().session_control
        mutation_frozen = bool(
            controls is not None and not controls.mutation_allowed()
        )
        if not enabled and mutation_frozen:
            blockers.append({
                "code": "mutation_frozen",
                "message": "Mutation is frozen by the operator control.",
            })
        if not enabled and not blockers:
            from agent_engine import mutation_elevation_blocked_by_threads

            blocked, blocked_reason = mutation_elevation_blocked_by_threads(
                self.runtimes.repository.thread_refs(str(chat_id))
            )
            if blocked:
                blockers.append({
                    "code": "pending_checkpoint_authority",
                    "message": blocked_reason,
                })
        if not enabled and not blockers:
            try:
                self.host.router._support_matrix.validate(
                    profile=ACTION_SURFACE,
                    provider=route["provider"],
                    model=route["model"],
                    adapter=route["adapter"],
                )
            except UnsupportedModelRoute as exc:
                blockers.append({
                    "code": "unsupported_model_route",
                    "message": str(exc),
                })

        reason = "; ".join(row["message"] for row in blockers).strip("; ")
        if not reason:
            if enabled and mutation_frozen:
                reason = "Mutation authoring is on but temporarily frozen by the operator."
            elif enabled:
                reason = "Mutation authoring is on for this chat."
            else:
                reason = (
                    "Mutation authoring is off; activated session tools remain mounted."
                )
        return {
            "schema": "variant1.astb.mutation-toggle.v1",
            "chat_id": str(chat_id),
            "enabled": enabled,
            "available": not blockers,
            "locked": any(
                row["code"] in {"unsupported_legacy_surface", "runtime_restricted"}
                for row in blockers
            ),
            "reason": reason,
            "current_profile": current,
            "target_profile": current,
            "route": route,
            "blockers": blockers,
            "authority_revision": revision,
            "effective_enabled": bool(enabled and not mutation_frozen),
        }

    def set_mutation(
        self,
        chat_id: str,
        *,
        enabled: bool,
        provider: str,
        model: str,
        adapter: str,
        expected_revision: int,
        actor: str = "chat_composer:user",
    ) -> dict[str, Any]:
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise ValueError("mutation authority revision must be an integer")
        if expected_revision < 0:
            raise ValueError("mutation authority revision cannot be negative")
        record = self.runtimes.ensure_runtime(str(chat_id))
        current = str(record.identity.action_surface or "")
        if not is_action_surface(current):
            raise RuntimeError(
                f"mutation requires {ACTION_SURFACE!r}; this chat uses {current!r}"
            )
        requested = bool(enabled)
        current_enabled = bool(record.mutation_write_enabled)
        if current_enabled == requested:
            # Even an idempotent request must prove that it was composed from
            # the current authority snapshot. Otherwise an old Deck click can
            # silently settle after another window changed the authority.
            selected = self.runtimes.set_mutation_write_enabled(
                str(chat_id),
                requested,
                actor=str(actor or "chat_composer:user"),
                expected_revision=expected_revision,
            )
            return {
                "ok": True,
                "already_selected": True,
                "chat_id": str(chat_id),
                "mutation_enabled": requested,
                "authority_revision": int(selected.mutation_authority_revision),
                "current_profile": current,
                "identity_changed": False,
            }
        status = self.mutation_toggle_status(
            str(chat_id), provider=provider, model=model, adapter=adapter
        )
        blockers = list(status.get("blockers") or ())
        if blockers:
            raise RuntimeError(
                "mutation authority change is blocked: "
                + "; ".join(
                    str(row.get("message") or row.get("code") or "blocked")
                    for row in blockers
                    if isinstance(row, dict)
                )
            )
        updated = self.runtimes.set_mutation_write_enabled(
            str(chat_id),
            requested,
            actor=str(actor or "chat_composer:user"),
            expected_revision=expected_revision,
        )
        return {
            "ok": True,
            "already_selected": False,
            "chat_id": str(chat_id),
            "mutation_enabled": bool(updated.mutation_write_enabled),
            "authority_revision": int(updated.mutation_authority_revision),
            "current_profile": str(updated.identity.action_surface),
            "identity_changed": False,
            "runtime": updated.to_dict(),
            "transcript_mutated": False,
        }
