"""Direct system commands and the compact native tool catalog."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import chat_commands
import desktop_control
from desktop import registry as desktop_registry
import host_prompt
import tools


def test_local_datetime_prompt_value_is_timezone_aware():
    value = datetime(2026, 8, 4, 12, 30, tzinfo=timezone(timedelta(hours=2), "CEST"))
    rendered = host_prompt.format_local_datetime(value)
    assert "Tuesday, August 04, 2026, 12:30 PM" in rendered
    assert "CEST (UTC+02:00)" in rendered


def test_direct_command_parsers_are_exact():
    assert chat_commands.parse_system_status_command("/system-status") == ""
    assert chat_commands.parse_system_status_command("show status") is None


@pytest.mark.asyncio
async def test_system_status_route_bypasses_model(monkeypatch):
    finish = AsyncMock()
    monkeypatch.setattr(chat_commands, "finish_chat_turn", finish)
    ports = SimpleNamespace(commands=SimpleNamespace(
        system_status=AsyncMock(return_value="System status\n- CPU: 7%"),
    ))
    decision = await chat_commands.CommandChatRoute().before_intent(
        ports, object(), object(), "/system-status",
        is_resume=False, resume_state=None, reserved=False,
    )
    assert decision.handled is True
    ports.commands.system_status.assert_awaited_once_with()
    assert finish.await_args.args[5].startswith("System status")


def test_complete_builtin_registry_has_no_retired_public_schemas():
    import server

    registry = server.APP.require_runtime().registry
    names = {tool.name for tool in registry.all()}
    assert {"ipython", "read_file", "run_command", "ask_user"} <= names
    assert "more_tools" not in names
    assert {
        "skill", "retrieve_memory", "delegate_task",
        "get_datetime", "system_status", "use_skill", "save_skill",
        "detect_modal", "dismiss_modal", "read_clipboard", "set_clipboard",
        "list_processes",
    }.isdisjoint(names)
