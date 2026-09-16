"""Task-turn ports contract for the native main-chat path.

Nested groups only (``ports.setup`` / ``ports.loop`` / ``ports.context``).
No flat construction adapters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from assistant_turn import AssistantTurn
from agent_types import ToolBatchResult
from agent_task import Task
from .shared_ports import AgentContextPorts


def task_progress_emit_fields(*, step: int | None = None) -> dict:
    """Step field for overlay progress activity."""
    out: dict = {}
    if step is not None:
        out["step"] = step
    return out


@dataclass
class TaskSetupPorts:
    """Plan, tool selection, and run identity for one task turn."""

    build_tools_block: Callable[[set, list], str]
    tool_lines: Callable[[list], str]
    registry_get: Callable[[str], Any]
    make_task: Callable[[str], Task]
    new_run: Callable[[str, str], Optional[dict]]
    install_image_sink: Callable[[dict], Any]
    use_reasoning: Callable[[], bool]
    current_model: Callable[[], str]
    continuation_context: Callable[[str], str] | None = None


@dataclass
class TaskLoopCorePorts:
    """I/O used by the typed model/tool loop."""

    stream: Callable[[list, int, Optional[str]], Awaitable[AssistantTurn]]
    run_actions: Callable[[list], Awaitable[ToolBatchResult]]
    emit: Callable[..., Awaitable[None]]
    should_stop: Callable[[], bool]
    clip: Callable[[str, int], str]
    state_block: Callable[[Any], str]
    drain_steering: Callable[[], Optional[dict]]
    drain_follow_up: Callable[[], Optional[dict]]
    record_active_input: Callable[[dict, Optional[str]], None]
    wait_if_paused: Optional[Callable[[], Awaitable[None]]] = None


@dataclass
class TaskTurnPorts:
    """Injected dependencies for task-path turn setup + loop wiring (nested only)."""

    setup: TaskSetupPorts
    loop: TaskLoopCorePorts
    context: AgentContextPorts
    snapshot_store: Any = None


@dataclass
class LoopPorts:
    """Injected dependencies for one task-loop run (flat subset used by nodes).

    Prefer :func:`loop_ports_from_task_turn` so the loop subset is derived from
    ``TaskTurnPorts`` in one place.
    """

    stream: Callable[[list, int, Optional[str]], Awaitable[AssistantTurn]]
    run_actions: Callable[[list], Awaitable[ToolBatchResult]]
    emit: Callable[..., Awaitable[None]]
    compress: Callable[..., Awaitable[list]]
    should_stop: Callable[[], bool]
    clip: Callable[[str, int], str]
    state_block: Callable[[Any], str]
    drain_steering: Callable[[], Optional[dict]]
    drain_follow_up: Callable[[], Optional[dict]]
    record_active_input: Callable[[dict, Optional[str]], None]
    progressive_disclose: Callable[[list, set, set, list], None]
    checkpoint: Optional[Callable[[str], Awaitable[None]]] = None
    wait_if_paused: Optional[Callable[[], Awaitable[None]]] = None


def loop_ports_from_task_turn(
    ports: TaskTurnPorts,
    *,
    compress: Callable[..., Awaitable[list]],
    progressive_disclose: Callable[[list, set, set, list], None],
    checkpoint: Optional[Callable[[str], Awaitable[None]]] = None,
) -> LoopPorts:
    """Project nested TaskTurnPorts → LoopPorts (single source of truth)."""
    setup, loop = ports.setup, ports.loop
    return LoopPorts(
        stream=loop.stream,
        run_actions=loop.run_actions,
        emit=loop.emit,
        compress=compress,
        should_stop=loop.should_stop,
        clip=loop.clip,
        state_block=loop.state_block,
        drain_steering=loop.drain_steering,
        drain_follow_up=loop.drain_follow_up,
        record_active_input=loop.record_active_input,
        progressive_disclose=progressive_disclose,
        checkpoint=checkpoint,
        wait_if_paused=loop.wait_if_paused,
    )


@dataclass
class LoopResult:
    mood: str
    reply: str
    interrupted: bool
    completion_status: str = "ok"
    stop_reason: str = ""
    terminal_reason: str = ""
    length_recoveries: int = 0


@dataclass
class TaskTurnResult:
    loop_result: LoopResult
    messages: list
    run: Optional[dict]
    img_token: Any
    task: Optional[Task]
    # Stable across a crash/resume of the same native run. Transcript storage
    # uses this identity to make its final append idempotent.
    transcript_id: str = ""
    # Interactive chat snapshots remain resumable until the outer transcript
    # store proves the visible exchange durable. The runner installs this
    # acknowledgement callback; non-checkpointed/test turns leave it unset.
    commit_transcript_terminal: Optional[Callable[[], Awaitable[dict | None]]] = None
