"""Short chat labels derived from finalized operations inside one IPython cell."""
from __future__ import annotations

from typing import TYPE_CHECKING

from tool_core import CapabilityReceipt

if TYPE_CHECKING:
    from .contracts import ExecutionAdmission


_FILE_ACTIONS = {
    "read_file": "Read file", "grep": "Searched files", "glob": "Listed files",
    "apply_patch": "Applied patch",
}
_OBJECT_ACTIONS = {
    "browser": {
        "navigate": "Opened browser page", "read": "Read browser page", "screenshot": "Captured browser screenshot",
        "click": "Interacted with browser", "fill": "Interacted with browser",
    },
    "peers": dict.fromkeys(("get", "list", "inbox", "inspect_message", "inspect_request"), "Checked peers"),
    "children": {"spawn": "Started subagent", "list": "Checked subagents", "tree": "Checked subagents"},
}
_COMMAND_MODES = {
    "once": "Ran command", "processes": "Listed processes", "terminals": "Listed terminals",
    "process": "Opened process handle", "terminal": "Opened terminal handle",
}
_HANDLE_METHODS = {
    ("execution", "process"): {
        "inspect": "Checked process state", "read": "Read process logs",
        "refresh": "Refreshed process handle", "wait": "Checked process condition",
        "write": "Queued process input", "signal": "Requested process signal",
    },
    ("execution", "terminal"): {
        "inspect": "Checked terminal state", "read": "Read terminal output",
        "refresh": "Refreshed terminal handle", "wait": "Checked terminal state",
        "write": "Queued terminal input", "signal": "Requested terminal signal",
        "resize": "Requested terminal resize", "close": "Requested terminal close",
        "detach": "Detached terminal",
    },
    ("browser", "session"): {"new_page": "Opened browser page", "pages": "Listed browser pages"},
    ("browser", "page"): {"observe": "Read browser page", "navigate": "Navigated browser page"},
    ("browser", "element"): dict.fromkeys(("click", "fill", "select", "hover"), "Interacted with browser"),
}


def _action(capability: str, args: dict, receipt: CapabilityReceipt) -> str:
    if capability == "run_command":
        mode = args.get("mode") or "once"
        mode = mode.strip().casefold() if isinstance(mode, str) else ""
        result = receipt.result_value
        if (mode == "once" and isinstance(result, dict) and result.get("schema") == "variant1.command-result.v1"
                and result.get("ok") is False and type(result.get("exit_code")) is int):
            return "Command returned a nonzero exit" if result["exit_code"] else "Checked command result"
        return _COMMAND_MODES.get(mode, "")
    if capability in _OBJECT_ACTIONS:
        operation = args.get("operation")
        return _OBJECT_ACTIONS[capability].get(operation, "") if isinstance(operation, str) else ""
    if capability != "remote_handle_dispatch":
        return _FILE_ACTIONS.get(capability, "")
    handle, method = args.get("handle"), args.get("method")
    if not isinstance(handle, dict) or not isinstance(method, str):
        return ""
    service, kind = handle.get("service"), handle.get("kind")
    if not isinstance(service, str) or not isinstance(kind, str):
        return ""
    if (service, kind, method) != ("execution", "process", "stop"):
        return _HANDLE_METHODS.get((service, kind), {}).get(method, "")
    # Only the returned service record can establish the outcome. A successful
    # stop on a previously exited process is also a valid broker receipt.
    value = receipt.result_value
    saved = value.get("$variant1_handle") if isinstance(value, dict) else None
    if isinstance(saved, dict) and handle.get("id") and all(
        saved.get(key) == handle.get(key) for key in ("service", "kind", "id")
    ):
        metadata = saved.get("metadata")
        stop = metadata.get("stop_receipt") if isinstance(metadata, dict) else None
        if isinstance(stop, dict):
            if stop.get("outcome") == "already_exited" and metadata.get("state") in ("exited", "terminated"):
                return "Process already exited"
            if stop.get("outcome") == "exit_after_stop_request" and metadata.get("state") == "terminated":
                return "Requested stop and confirmed exit"
    return "Checked process stop"


class CellActivity:
    def __init__(self) -> None:
        self._actions: list[str] = []
        self._other_actions = False
        self._ambiguous = False

    def observe(self, admission: ExecutionAdmission, nested_id: str, capability: str,
                args: dict, receipt: CapabilityReceipt) -> None:
        origin = receipt.attribution
        if (self._ambiguous or receipt.deduplicated or not nested_id
                or receipt.capability.get("capability_id") != capability
                or origin.get("surface") != "ipython"
                or origin.get("chat_id") != admission.chat_id
                or origin.get("run_id") != admission.run_id
                or origin.get("outer_tool_call_id") != admission.outer_tool_call_id
                or origin.get("cell_execution_id") != admission.execution_id
                or origin.get("nested_call_id") != nested_id):
            return
        action = _action(capability, args, receipt) if receipt.ok else ""
        if not action:
            self._ambiguous = True
        elif action not in self._actions:
            if len(self._actions) < 2:
                self._actions.append(action)
            else:
                self._other_actions = True

    def summary(self) -> str:
        if self._ambiguous or not self._actions:
            return "Ran Python"
        text = self._actions[0]
        if len(self._actions) == 2:
            text += ", " + self._actions[1].lower()
        return text + (" and more" if self._other_actions else "")
