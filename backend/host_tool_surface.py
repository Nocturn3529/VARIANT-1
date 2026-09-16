"""Host-owned tool registration and handlers."""

from __future__ import annotations

from typing import Callable

import clarification
import memory_tools
import tools


def _bind(host, fn: Callable) -> Callable:
    async def handler(args):
        return await fn(host, args)
    return handler


def _ports(host, name: str):
    factory = getattr(host, name, None)
    if not callable(factory):
        raise AttributeError(f"host has no ports factory {name!r}")
    return factory()


def register_all(host) -> None:
    """Register runtime-bound seeds after the complete graph is installed."""
    runtime = host.require_runtime()
    registry = runtime.registry

    import builtin_tools
    import shell_tool

    builtin_tools.register(registry)
    shell_tool.register(
        registry,
        shell_tool.ShellToolDeps(
            execution=runtime.execution,
            host=host,
        ),
    )

    registry.register(tools.Tool(
        "ask_user",
        "Pause this interactive task and show one to three optional clarifying questions "
        "above the composer. Use only when an unresolved user choice would materially "
        "change the result and cannot be safely inferred. The user can choose an option, "
        "type another answer, or skip. Do not use for casual conversation, information "
        "already supplied, low-impact preferences, or headless/background work. Other "
        "tool calls execute directly; never use this to ask permission for another call. "
        "Python returns {interaction_id, status, answers}. Question IDs are q1, q2, q3 "
        "in input order. For an answered single-choice question, use result['answers']['q1']; "
        "multi-select answers are lists. Skipped, timed-out or cancelled questions have no answers.",
        _bind(host, tool_ask_user),
        category="system",
        params={
            "questions": {
                "type": "array", "required": True, "minItems": 1, "maxItems": 3,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["question", "header", "options"],
                    "properties": {
                        "question": {"type": "string", "minLength": 1, "maxLength": 300},
                        "header": {"type": "string", "minLength": 1, "maxLength": 40},
                        "multiSelect": {"type": "boolean", "default": False},
                        "options": {
                            "type": "array", "minItems": 2, "maxItems": 4,
                            "items": {
                                "type": "object", "additionalProperties": False,
                                "required": ["label", "description"],
                                "properties": {
                                    "label": {"type": "string", "minLength": 1,
                                              "maxLength": 80},
                                    "description": {"type": "string", "minLength": 1,
                                                    "maxLength": 240},
                                },
                            },
                        },
                    },
                },
                "desc": "one to three concise questions with clear choices",
            },
        },
        when="a consequential ambiguity cannot be resolved from context",
        avoid="questions answerable from context or by a reasonable assumption",
        result_projection="clarification-answer-v1",
    ))
    # Catalog publication is deferred until the complete typed runtime has
    # registered every seed/object handler. Publishing here would create a
    # partial startup release on every clean composition.


async def tool_ask_user(host, args) -> str:
    return await clarification.tool_ask_user(
        host.require_runtime().work.interactions,
        args or {},
    )


async def consolidate_memory_once(host) -> int:
    return await memory_tools.consolidate_memory_once(_ports(host, "memory_ports"))
