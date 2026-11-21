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


def turn_binding(context, chat_id):
    binding = (getattr(context, "metadata", None) or {}).get("_chat_goal_binding")
    return binding if isinstance(binding, GoalBinding) and binding.chat_id == chat_id else None


def attributed_scope(context, scope):
    """A goal created inside this turn also belongs to its later cells and children."""
    scope = coerce_work_scope(scope)
    binding = turn_binding(context, scope.chat_id)
    if binding is not None and scope.goal_id != binding.attributed_id:
        return scope.with_updates(goal_id=binding.attributed_id)
    return scope


def owner_eligible(record):
    return not str(getattr(record, "creation_saga_state", "") or "").startswith(("child:", "worker:"))
