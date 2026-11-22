from dataclasses import replace

import pytest

from kernel_runtime.contracts import ExecutionAdmission
from tool_core import CapabilityReceipt


def admission():
    return ExecutionAdmission("cell", "chat", "run", "outer", 1, "release", 1, "build", 1, "env", ())


def receipt(capability, nested="nested", **options):
    return CapabilityReceipt("receipt", options.pop("status", "ok"), {"capability_id": capability}, "digest",
                             attribution={"chat_id": "chat", "run_id": "run", "outer_tool_call_id": "outer",
                                          "cell_execution_id": "cell", "nested_call_id": nested, "surface": "ipython"},
                             **options)


@pytest.mark.parametrize("key", ["chat_id", "run_id", "outer_tool_call_id", "cell_execution_id", "nested_call_id", "surface"])
def test_only_this_cells_receipts_supply_labels(key):
    cell = admission()
    wrong = receipt("read_file")
    wrong.attribution[key] = "elsewhere"
    cell.activity.observe(cell, "nested", "read_file", {}, wrong)
    cell.activity.observe(cell, "nested", "read_file", {}, receipt("grep"))
    cell.activity.observe(cell, "nested", "read_file", {}, receipt("read_file", deduplicated=True))
    assert cell.activity.summary() == "Ran Python"


def test_repetitive_polling_and_mixed_calls_stay_bounded():
    cell = admission()
    process = {"service": "execution", "kind": "process", "id": "proc"}
    for n in range(64):
        key = str(n)
        cell.activity.observe(cell, key, "remote_handle_dispatch", {"handle": process, "method": "inspect"},
                              receipt("remote_handle_dispatch", key))
    assert cell.activity.summary() == "Checked process state"
    cell.activity.observe(cell, "logs", "remote_handle_dispatch", {"handle": process, "method": "read"},
                          receipt("remote_handle_dispatch", "logs"))
    assert cell.activity.summary() == "Checked process state, read process logs"
    cell.activity.observe(cell, "list", "run_command", {"mode": "processes"}, receipt("run_command", "list"))
    assert cell.activity.summary() == "Checked process state, read process logs and more"
    cell.activity.observe(cell, "nested", "read_file", {}, receipt("read_file", status="error"))
    assert cell.activity.summary() == "Ran Python"
    assert admission().activity.summary() == "Ran Python"


def test_handle_stop_labels_require_matching_result_evidence():
    handle = {"service": "execution", "kind": "process", "id": "proc"}
    metadata = {"state": "terminated", "stop_receipt": {"outcome": "exit_after_stop_request"}}
    base = receipt("remote_handle_dispatch")
    def label(returned):
        cell = admission()
        cell.activity.observe(cell, "nested", "remote_handle_dispatch",
                              {"handle": {**handle, "metadata": metadata}, "method": "stop"},
                              replace(base, result_value=returned))
        return cell.activity.summary()
    assert label({"text": "Stopped process"}) == "Checked process stop"
    assert label({"$variant1_handle": {**handle, "id": "another", "metadata": metadata}}) == "Checked process stop"
    assert label({"$variant1_handle": {**handle, "metadata": metadata}}) == "Requested stop and confirmed exit"
    assert label({"$variant1_handle": {**handle, "metadata": {
        "state": "exited", "stop_receipt": {"outcome": "already_exited"},
    }}}) == "Process already exited"


def test_reported_nonzero_command_and_unsupported_handles():
    cell = admission()
    cell.activity.observe(cell, "nested", "run_command", {}, receipt("run_command", result_value={
        "schema": "variant1.command-result.v1", "ok": False, "exit_code": 75,
    }))
    assert cell.activity.summary() == "Command returned a nonzero exit"
    cell = admission()
    cell.activity.observe(cell, "nested", "remote_handle_dispatch", {
        "handle": {"service": "other", "kind": "process", "id": "proc"}, "method": "read",
    }, receipt("remote_handle_dispatch", result_value="Read process logs"))
    assert cell.activity.summary() == "Ran Python"


def test_host_browser_and_peer_lookups_remain_recognizable():
    cell = admission()
    for capability, operation in (("browser", "navigate"), ("peers", "list")):
        cell.activity.observe(cell, operation, capability, {"operation": operation}, receipt(capability, operation))
    assert cell.activity.summary() == "Opened browser page, checked peers"
