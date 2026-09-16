"""Native agent execution stage for an ordinary interactive chat turn."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

from chat_context_stage import ChatAgentRequest
from chat_stage_result import ChatStageContinue, ChatStageFail, ChatStageResult
from llm_router import LocalEngineError
from observability.activity import clip
from run_context import current_run_context

if TYPE_CHECKING:
    from chat_pipeline import ChatPorts


@dataclass(frozen=True)
class ChatAgentTurn:
    text: str
    mood: str
    reply: str
    interrupted: bool
    run: dict | None
    completion_status: str
    stop_reason: str
    terminal_reason: str
    length_recoveries: int
    transcript_id: str = ""
    commit_transcript_terminal: Callable[[], Awaitable[dict | None]] | None = None


async def run_chat_agent_stage(
    ports: "ChatPorts",
    session,
    request: ChatAgentRequest,
    *,
    reserved: bool,
) -> ChatStageResult[ChatAgentTurn]:
    """Run the unified persistent-Python agent and project its terminal turn."""
    image_token = None
    run = None
    try:
        session.active.task = None
        from agent_engine.executor import execute_main_chat
        from agent_engine.presets import chat_task_default

        run_config = chat_task_default().with_overrides(
            unified_conversation=not request.prepared.is_resume,
            graph_revision=request.graph_revision,
            action_surface=request.action_surface,
            provider_tool_schema_revision=request.provider_tool_schema_revision,
        )
        turn = await execute_main_chat(
            config=run_config,
            text=request.agent_text,
            base_system=request.base_system,
            full_tspec=request.full_tool_specs,
            convo_tail=session.convo,
            images=request.model_images,
            is_resume=request.prepared.is_resume,
            resume_snap=request.prepared.plan.resume.resume_state,
            ports=request.turn_ports,
            context_receipt=request.context_receipt,
            graph_revision=request.graph_revision,
            session_capabilities=request.session_capabilities,
        )
        if turn is None:
            return ChatStageFail(
                "native agent engine returned no task result",
                "I couldn't complete that turn safely. Please try again.",
            )
        session.active.task = turn.task
        image_token = turn.img_token
        run = turn.run
        if run is not None:
            run_ctx = current_run_context()
            tools = list(
                (run_ctx.metadata or {}).get("tools_used") or []
            ) if run_ctx is not None else []
            run["tools_used"] = sorted(set(tools))
        return ChatStageContinue(ChatAgentTurn(
            text=request.prepared.text,
            mood=turn.loop_result.mood,
            reply=turn.loop_result.reply,
            interrupted=turn.loop_result.interrupted,
            run=run,
            completion_status=turn.loop_result.completion_status,
            stop_reason=turn.loop_result.stop_reason,
            terminal_reason=turn.loop_result.terminal_reason,
            length_recoveries=turn.loop_result.length_recoveries,
            transcript_id=turn.transcript_id,
            commit_transcript_terminal=turn.commit_transcript_terminal,
        ))
    except LocalEngineError as exc:
        if run:
            await ports.io.emit(
                "task:done",
                run_id=run.get("id"),
                source=run.get("source"),
                status="error",
                text=f"Model error: {clip(str(exc), 200)}",
            )
        return ChatStageFail(
            f"local model failed: {exc}",
            "The model request failed. Please try again.",
            cause=exc,
        )
    except Exception as exc:
        return ChatStageFail(
            f"chat agent execution failed: {type(exc).__name__}: {exc}",
            "I couldn't complete that turn safely. Please try again.",
            cause=exc,
        )
    finally:
        if image_token is not None:
            from desktop.service import reset_image_sink

            reset_image_sink(image_token)
        if not reserved:
            session.busy = False
            session.active.task = None
