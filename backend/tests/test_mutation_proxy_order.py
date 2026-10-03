"""Positional proxy calls bind in declared order in the kernel and in mutations.

Catalog documents are canonical JSON, so parameter keys arrive sorted. These
tests use a real catalog binding whose declared order is not alphabetical.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core_invariants import canonical_json
from kernel_runtime.worker_bridge import CapabilityProxy
from session_catalog.catalog import _binding
from session_catalog.mutation import MutationManager
from session_catalog.mutation_worker_client import MutationWorkerClient
from tools import Tool


async def _noop(_args):
    return None


def _canonical_binding() -> dict:
    tool = Tool(
        "read_file", "Read part of a file.", _noop,
        params={
            "path": {"type": "string", "required": True},
            "limit": {"type": "integer", "required": False},
        },
        effect_class="read",
    )
    binding = json.loads(canonical_json(_binding(tool)))
    assert list(binding["params"]) == ["limit", "path"]  # sorted, not declared
    return binding


def _contracts(binding: dict) -> dict:
    loaded = SimpleNamespace(
        release_id="release",
        document={"categories": [{
            "category_id": "build",
            "slots": [{"position": 1, "projection": "seeds", "bindings": [binding]}],
        }]},
    )

    def no_runtime(_chat_id):
        raise LookupError("no runtime in this test")

    fake = SimpleNamespace(
        enabled_resolver=lambda: {"read_file"},
        runtime_registry=SimpleNamespace(ensure_runtime=no_runtime),
        registry=SimpleNamespace(get=lambda _name: None),
    )
    return MutationManager._proxy_contracts(fake, loaded, "chat")


def test_kernel_proxy_binds_positionals_in_declared_order():
    binding = _canonical_binding()
    proxy = CapabilityProxy(binding, None)
    assert proxy._arguments(("notes.txt", 5), {}) == {"path": "notes.txt", "limit": 5}


def test_mutation_contracts_carry_declared_parameter_order():
    contracts = _contracts(_canonical_binding())
    assert contracts["tools.read_file"]["parameters"] == ["path", "limit"]


@pytest.mark.asyncio
async def test_mutation_worker_binds_positionals_like_the_kernel(tmp_path):
    contracts = _contracts(_canonical_binding())
    calls = []

    async def proxy_call(name, arguments, request_id):
        calls.append((name, arguments))
        return {"ok": True, "result": arguments, "receipt_id": "r-" + request_id}

    report = await MutationWorkerClient(str(tmp_path / "workers")).run(
        {
            "mode": "execute",
            "source": "def run(arguments):\n    return tools.read_file('notes.txt', 5)\n",
            "arguments": {},
            "proxy_contracts": MutationManager._worker_proxy_contracts(contracts),
        },
        proxy_call=proxy_call,
    )
    assert calls == [("tools.read_file", {"path": "notes.txt", "limit": 5})]
    assert report["result"] == {"path": "notes.txt", "limit": 5}
