"""VARIANT-1 product policies for the shared model-step engine."""

from __future__ import annotations

from dataclasses import dataclass
import inspect
from typing import TYPE_CHECKING

import tool_discovery
from assistant_turn import AssistantTurn
from run_context import current_run_context

if TYPE_CHECKING:
    from .agent_runtime import HeadlessWorkerRuntime
    from .task_ports import LoopPorts


@dataclass
class MainModelStepPolicy:
    ports: "LoopPorts"
    max_output_tokens: int
    image_input: list[dict] | None
    contract: str = "task"
    max_clean_replays: int = 1

    def should_stop(self) -> bool:
        return self.ports.should_stop()

    async def prepare_messages(self, messages: list) -> list:
        return await self.ports.compress(
            messages,
            max_output_tokens=self.max_output_tokens,
            image_input=self.image_input,
        )

    async def invoke(self, messages: list) -> AssistantTurn:
        return await self.ports.stream(
            messages,
            self.max_output_tokens,
            self.image_input,
        )


@dataclass
class HeadlessModelStepPolicy:
    runtime: "HeadlessWorkerRuntime"
    max_output_tokens: int
    contract: str = "worker"
    max_clean_replays: int = 1

    def should_stop(self) -> bool:
        return self.runtime.should_stop()

    async def prepare_messages(self, messages: list) -> list:
        drain = getattr(self.runtime, "drain_inbound", None)
        if callable(drain):
            known_ids = {
                str(item.get("_variant1_inbound_message_id") or "")
                for item in messages
                if isinstance(item, dict)
                and item.get("_variant1_inbound_message_id")
            }
            inbound = drain()
            if inspect.isawaitable(inbound):
                inbound = await inbound
            for item in inbound or ():
                message_id = str(
                    item.get("message_id") if isinstance(item, dict) else ""
                ).strip()
                if message_id and message_id in known_ids:
                    continue
                text = str(
                    item.get("text") if isinstance(item, dict) else item
                ).strip()
                if text:
                    projected = {
                        "role": "user",
                        "content": "[PARENT MESSAGE]\n" + text,
                    }
                    if message_id:
                        projected["_variant1_inbound_message_id"] = message_id
                        known_ids.add(message_id)
                    messages.append(projected)
        return messages

    async def invoke(self, messages: list) -> AssistantTurn:
        if callable(self.runtime.stream_tools):
            run_ctx = current_run_context()
            disclosed_specs = list(
                getattr(run_ctx, "disclosed_tool_specs", None)
                or self.runtime.disclosed_tool_specs
            )
            self.runtime.disclosed_tool_specs = disclosed_specs
            return await self.runtime.stream_tools(
                messages,
                self.max_output_tokens,
                tool_discovery.provider_tool_specs(disclosed_specs),
            )
        return await self.runtime.stream(messages, self.max_output_tokens)
