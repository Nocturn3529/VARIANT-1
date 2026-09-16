"""Minimal durable identity for one main-chat agent run."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


class TaskStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Task:
    goal: str
    status: TaskStatus = TaskStatus.IN_PROGRESS
    context: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    started_at: float = field(default_factory=time.time)

    def render_static(self) -> str:
        return f"## CURRENT TASK [{self.id}]\nGOAL: {self.goal}"

    def render_state(self) -> str:
        return f"TASK STATE: {self.status.value}"

    def render(self) -> str:
        return self.render_static() + "\n" + self.render_state()
