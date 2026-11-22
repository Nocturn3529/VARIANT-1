"""Host-owned Goal identity shared by the model and kernel during one chat admission."""
from __future__ import annotations

from dataclasses import dataclass

from work_fabric.scope import coerce_work_scope


@dataclass
class GoalBinding:
    chat_id: str
    admission_id: str
    goal_id: str
    attributed_id: str
    objective_revision: int
    show_objective: bool = False

    def observation(self) -> dict:
        return {"goal_id": self.goal_id, "objective_revision": self.objective_revision}

    def restore_observation(self, saved, *, prior_goal_id: str, pending_tools: bool) -> None:
        # An unmatched checkpointed call came from the old request, even if
        # Resume admitted it after an edit or a replacement. Older snapshots
        # lack an objective fence and need a fresh model step to change a Goal.
        saved = saved if isinstance(saved, dict) else {}
        seen_id = saved.get("goal_id")
        if not isinstance(seen_id, str):
            seen_id = prior_goal_id
            saved = {}
        if pending_tools:
            self.goal_id = seen_id
        revision = saved.get("objective_revision")
        self.objective_revision = revision if seen_id == self.goal_id and type(revision) is int and revision > 0 else 0
        self.show_objective = self.objective_revision == 0


def turn_binding(context, chat_id):
    binding = (getattr(context, "metadata", None) or {}).get("_chat_goal_binding")
    return binding if isinstance(binding, GoalBinding) and binding.chat_id == chat_id else None


def current_turn_binding():
    from run_context import current_run_context
    context = current_run_context()
    return turn_binding(context, context.work_scope.chat_id) if context is not None else None


def attributed_scope(context, scope):
    """A goal created inside this turn also belongs to its later cells and children."""
    scope = coerce_work_scope(scope)
    binding = turn_binding(context, scope.chat_id)
    if binding is not None and scope.goal_id != binding.attributed_id:
        return scope.with_updates(goal_id=binding.attributed_id)
    return scope


def owner_eligible(record):
    return not str(getattr(record, "creation_saga_state", "") or "").startswith(("child:", "worker:"))
