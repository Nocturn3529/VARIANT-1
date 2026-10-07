"""A promoted helper calls mounted capabilities exactly as it did in the kernel.

Each call goes through the real kernel proxy and through a real mutation worker
built from canonical catalog bindings; the wire arguments must match.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core_invariants import canonical_json
from kernel_runtime.worker_bridge import CapabilityProxy
from session_catalog.catalog import _binding
from session_catalog.mutation import MutationManager
from session_catalog.mutation_contracts import MutationWorkerError
from session_catalog.mutation_worker_client import MutationWorkerClient
from shell_tool import _RUN_COMMAND_PARAMS
from tools import Tool


async def _noop(_args):
    return None


def _canonical(name: str, params: dict) -> dict:
    return json.loads(canonical_json(_binding(Tool(name, name, _noop, params=params))))


BINDINGS = {
    "run_command": _canonical("run_command", _RUN_COMMAND_PARAMS),
    "read_file": _canonical("read_file", {
        "path": {"type": "string", "required": True},
        "limit": {"type": "integer", "required": False},
    }),
    "apply_edits": _canonical("apply_edits", {
        "edits": {"type": "array", "required": True, "items": {"type": "object"}},
    }),
    "choose": _canonical("choose", {
        "selection": {"type": "object", "required": False},
        "tool_name": {"type": "string", "required": False},
        "arguments": {"type": "object", "required": False},
    }),
}

# (Python call in a cell, args, kwargs) for each proxy.
CALLS = [
    ("run_command", "tools.run_command(['app.exe', '--flag'])", (["app.exe", "--flag"],), {}),
    ("run_command", "tools.run_command('dir', timeout=5)", ("dir",), {"timeout": 5}),
    ("read_file", "tools.read_file({'path': 'notes.txt'})", ({"path": "notes.txt"},), {}),
    ("read_file", "tools.read_file('notes.txt', limit=None)", ("notes.txt",), {"limit": None}),
    ("read_file", "tools.read_file('notes.txt', 20)", ("notes.txt", 20), {}),
    ("apply_edits", "tools.apply_edits({'file': 'a.txt'})", ({"file": "a.txt"},), {}),
    ("choose", "tools.choose([{'id': 'x'}])", ([{"id": "x"}],), {}),
    ("choose", "tools.choose('search_issues')", ("search_issues",), {}),
]


def _worker_contracts() -> dict:
    bindings = list(BINDINGS.values())
    loaded = SimpleNamespace(release_id="release", document={"categories": [{
        "category_id": "build",
        "slots": [
            {"position": index + 1, "projection": "seeds", "bindings": [binding]}
            for index, binding in enumerate(bindings)
        ],
    }]})

    def no_runtime(_chat_id):
        raise LookupError("no runtime in this test")

    fake = SimpleNamespace(
        enabled_resolver=lambda: {binding["tool_name"] for binding in bindings},
        runtime_registry=SimpleNamespace(ensure_runtime=no_runtime),
        registry=SimpleNamespace(get=lambda _name: None),
    )
    return MutationManager._worker_proxy_contracts(
        MutationManager._proxy_contracts(fake, loaded, "chat")
    )


async def _worker_arguments(tmp_path, source: str) -> list[dict]:
    seen: list[dict] = []

    async def proxy_call(_name, arguments, request_id):
        seen.append(arguments)
        return {"ok": True, "result": None, "receipt_id": "r-" + request_id}

    await MutationWorkerClient(str(tmp_path / "workers")).run(
        {
            "mode": "execute",
            "source": source,
            "arguments": {},
            "proxy_contracts": _worker_contracts(),
        },
        proxy_call=proxy_call,
    )
    return seen


@pytest.mark.asyncio
async def test_kernel_and_worker_send_identical_wire_arguments(tmp_path):
    body = "".join(f"    {expression}\n" for _alias, expression, _a, _k in CALLS)
    worker = await _worker_arguments(tmp_path, "def run(arguments):\n" + body + "    return None\n")
    kernel = [
        CapabilityProxy(BINDINGS[alias], None)._arguments(args, kwargs)
        for alias, _expression, args, kwargs in CALLS
    ]
    assert worker == kernel
    # The conveniences survive the shared rules.
    assert kernel[0] == {"argv": ["app.exe", "--flag"]}
    assert kernel[2] == {"path": "notes.txt"}
    assert kernel[3] == {"path": "notes.txt"}
    assert kernel[5] == {"edits": [{"file": "a.txt"}]}
    assert kernel[6] == {"selection": {"id": "x"}}
    assert kernel[7] == {"tool_name": "search_issues"}


@pytest.mark.asyncio
async def test_worker_reports_invalid_calls_like_the_kernel(tmp_path):
    with pytest.raises(TypeError, match=r"Invalid call to tools\.read_file"):
        CapabilityProxy(BINDINGS["read_file"], None)._arguments(("a", 1, 2), {})
    with pytest.raises(MutationWorkerError) as caught:
        await _worker_arguments(
            tmp_path,
            "def run(arguments):\n    return tools.read_file('a', 1, 2)\n",
        )
    assert "Invalid call to tools.read_file" in str(caught.value)
