from __future__ import annotations

import json
import ntpath

from execution_hosts.command_environment import noninteractive_environment, windows_system_executable
import shell_tool
import transcript_economy as economy


def test_command_defaults_allow_explicit_override_without_affecting_interactive_profiles():
    delta = noninteractive_environment({"GIT_EDITOR": "my-editor", "TERM": "xterm"})
    assert delta["GIT_TERMINAL_PROMPT"] == "0"
    assert delta["GIT_EDITOR"] == "my-editor" and delta["TERM"] == "xterm"
    interactive, _, _ = shell_tool._project_environment({"environment": {"PAGER": "user-pager"}})
    assert interactive["PAGER"] == "user-pager" and "GIT_TERMINAL_PROMPT" not in interactive
    command, _, _ = shell_tool._project_environment({}, noninteractive=True)
    assert command["GIT_TERMINAL_PROMPT"] == "0"


def test_windows_helpers_are_absolute_and_not_resolved_from_project_path(monkeypatch):
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    assert windows_system_executable("powershell.exe") == r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    assert ntpath.isabs(windows_system_executable("taskkill.exe"))


def test_summary_labels_pair_named_calls_and_keep_parallel_unknowns_unpaired():
    call = {"id": "call-a", "function": {"name": "run_command", "arguments": "{}"}}
    result = {"role": "tool", "tool_call_id": "call-a", "content": "failed", "is_error": True}
    messages = [{"role": "assistant", "tool_calls": [call]}, result]
    calls, results = economy._summary_labels(messages)
    text = economy._serialize_message_for_summary(result, call_labels=calls, result_label=results[id(result)])
    assert "run_command" in text and "[FAILED]" in text and "call-a" in text
    anonymous = [{"function": {"name": "run_command"}}, {"function": {"name": "run_command"}}]
    unknown = {"role": "tool", "content": "no ID"}
    _, labels = economy._summary_labels([{"role": "assistant", "tool_calls": anonymous}, unknown])
    assert labels[id(unknown)] == "tool result unpaired"


def test_file_state_is_bounded_prefers_edits_and_can_recover_full_prior_state():
    state = {"read_files": [{"path": "x" * 100 + str(i), "ranges": [[1, 20]]} for i in range(500)],
             "modified_files": ["edited.py"]}
    rendered = economy._render_file_state(state, full_state_ref="artifact://sha256/" + "a" * 64)
    assert len(rendered) <= 6000
    value = json.loads(rendered.split("\n", 1)[1].rsplit("\n", 1)[0])
    assert value["modified_files"] == ["edited.py"] and value["omitted_read_files"] > 0
    recovered = economy._collect_file_state(
        [{"role": "assistant", "content": rendered}], resolve_state=lambda _: state,
    )
    assert len(recovered["read_files"]) == 500
