import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import traceback
from types import SimpleNamespace

import pytest

from core_invariants import request_fingerprint
from extensions.runtime_v2 import create_extension_v2_runtime
from extensions.worker_host import (
    PluginIdempotencyConflict, PluginInvocationFailed, PluginWorkerHost, PluginWorkerLost,
    UnknownPluginEffect,
)
from messaging_gateway.base import MessageEnvelope
from messaging_gateway.extension_adapter import ExtensionMessagingAdapter


_PACKAGE = "com.example.messaging-fixture"
_CONTRIBUTION = "fixture-messaging"


def _runtime(tmp_path: Path):
    root = tmp_path / "package"
    (root / "python").mkdir(parents=True)
    (root / "arguments.json").write_text(json.dumps({
        "type": "object", "required": ["operation", "token", "credentials"],
        "properties": {"token": {"type": "string"}, "credentials": {"type": "object"}},
    }), encoding="utf-8")
    (root / "python" / "fixture.py").write_text(
        '''
def dispatch(arguments, context):
    token = arguments["token"]
    access = arguments["credentials"]["access"]
    assert token.startswith("fixture-token-") and access.startswith("fixture-access-")
    print(f"provider diagnostic: {token} {access}")
    if arguments.get("text") == "reject":
        raise RuntimeError(f"fixture rejected {token} {access}")
    return {"cursor": 1, "authorized": True, "echo": [token, access]}
'''.lstrip(),
        encoding="utf-8",
    )
    (root / "variant1.plugin.json").write_text(json.dumps({
        "schema_version": 2, "id": _PACKAGE, "name": "Fixture adapter", "version": "1.0.0",
        "entrypoints": {"python": {"module": "fixture", "environment": "isolated"}},
        "contributes": {"messaging_adapters": [{
            "id": _CONTRIBUTION, "adapter_name": _CONTRIBUTION,
            "handler": "fixture:dispatch", "effect_class": "external_effect",
            "input_schema": "arguments.json",
        }]},
    }), encoding="utf-8")
    runtime = create_extension_v2_runtime(
        str(tmp_path / "data"), environment_builder=lambda *_: {"mode": "test-source"},
    )
    runtime.packages.install(str(root))
    return runtime


def _database_dump(path):
    with closing(sqlite3.connect(path)) as connection:
        return "\n".join(connection.iterdump())


@pytest.mark.asyncio
async def test_gateway_credentials_are_available_only_to_the_dispatch(tmp_path):
    runtime = _runtime(tmp_path)
    contribution = runtime.packages.resolved_contributions(kind="messaging_adapters")[0]
    polled = asyncio.Event()
    revision = 1

    async def token(_name):
        return f"fixture-token-{revision}"

    async def credentials(_name):
        return {"access": f"fixture-access-{revision}"}

    adapter = ExtensionMessagingAdapter(SimpleNamespace(
        ingress=SimpleNamespace(
            adapter_cursor=lambda _name: 0,
            set_adapter_cursor=lambda _name, cursor: polled.set() if cursor == 1 else None,
        ),
        adapter_config=lambda _name: {"enabled": True}, token=token, credentials=credentials,
    ), runtime, contribution)
    envelope = MessageEnvelope(
        adapter=adapter.name, message_id="test-message", conversation_id="test-thread",
        user_id="test-user", text="message",
    )
    try:
        await adapter.start()
        await asyncio.wait_for(polled.wait(), timeout=15)
        await adapter.stop()
        await adapter.send_text(envelope, "reply")
        revision = 2
        await adapter.send_text(envelope, "reply")
        await adapter.send_typing(envelope)
        with pytest.raises(PluginInvocationFailed, match="fixture rejected") as caught:
            await adapter.send_text(envelope, "reject")

        rows = runtime.workers.operations()
        assert sorted(row["arguments"]["operation"] for row in rows) == [
            "poll", "send_text", "send_text", "send_typing",
        ]
        assert all(row["attempt_count"] == 1 for row in rows)
        assert all("token" not in row["arguments"] and "credentials" not in row["arguments"] for row in rows)
        for row in rows:
            if row["status"] == "succeeded":
                assert row["result"]["authorized"]
                assert row["result"]["echo"] == ["[redacted]", "[redacted]"]
        evidence = _database_dump(runtime.workers.path) + json.dumps(runtime.workers.state()) + str(caught.value)
        for saved_revision in (1, 2):
            assert f"fixture-token-{saved_revision}" not in evidence
            assert f"fixture-access-{saved_revision}" not in evidence
    finally:
        await adapter.stop()
        await runtime.workers.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("verified", [True, False])
async def test_upgrade_scrubs_messaging_receipts_without_replaying_effects(tmp_path, verified):
    runtime = _runtime(tmp_path)
    contribution = runtime.packages.resolved_contributions(kind="messaging_adapters")[0]
    digest = contribution["package_digest"]
    context = {"surface": "messaging", "adapter": _CONTRIBUTION, "package_digest": digest}
    arguments = {"operation": "send_text", "text": "reply"}
    old = {**arguments, "token": "fixture-token-old", "credentials": {"access": "fixture-access-old"}}
    old_fingerprint = request_fingerprint("plugin.invoke", {
        "package_digest": digest, "contribution_kind": "messaging_adapters",
        "contribution_id": _CONTRIBUTION, "handler": "fixture:dispatch",
        "effect_class": "external_effect",
        "arguments": old if verified else {**arguments, "text": "different"},
        "context": context,
    })
    path = runtime.workers.path
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DELETE FROM extension_worker_migration")
        if not verified:
            conn.execute("INSERT INTO extension_worker_migration VALUES ('messaging-credentials-v1')")
        conn.execute(
            "INSERT INTO extension_worker_operation_v2 "
            "(operation_id,dedupe_key,idempotency_key,request_fingerprint,package_id,package_digest,"
            "contribution_id,handler,effect_class,status,arguments_json,context_json,result_json,error_json,"
            "prepared_at,attempt_count) VALUES (?,?,?,?,?,?,?,?,?,'succeeded',?,?,?,?,0,1)",
            ("existing", "send-once", "send-once", old_fingerprint, _PACKAGE, digest,
             _CONTRIBUTION, "fixture:dispatch", "external_effect",
             json.dumps(old), json.dumps(context), json.dumps({"sent": True, "echo": old["token"]}),
             json.dumps({"message": old["credentials"]["access"]}) if not verified else ""),
        )

    def unexpected_dispatch(*_args):
        pytest.fail("a completed send must reuse its original receipt")

    for _ in range(2):
        host = PluginWorkerHost(runtime.packages, path, command_builder=unexpected_dispatch)
        try:
            new = {**arguments, "token": "fixture-token-new", "credentials": {"access": "fixture-access-new"}}
            options = {"context": context, "idempotency_key": "send-once", "contribution_kind": "messaging_adapters"}
            operation = host.operation("existing")
            assert operation["status"] == "succeeded"
            assert operation["attempt_count"] == 1
            assert operation["arguments"] == arguments
            assert operation["result"] == {"sent": True, "echo": "[redacted]"}
            if verified:
                receipt = await host.invoke(_PACKAGE, _CONTRIBUTION, new, **options)
                assert receipt["operation_id"] == "existing" and receipt["replayed"]
            else:
                assert operation["error"] == {"message": "[redacted]"}
                with pytest.raises(PluginIdempotencyConflict):
                    await host.invoke(_PACKAGE, _CONTRIBUTION, new, **options)
            with pytest.raises(PluginIdempotencyConflict):
                await host.invoke(_PACKAGE, _CONTRIBUTION, {**new, "text": "different"}, **options)
        finally:
            await host.shutdown()
    assert "fixture-token-old" not in _database_dump(path)
    assert "fixture-access-old" not in _database_dump(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("persist_terminal", [True, False])
async def test_lost_messaging_worker_surfaces_only_redacted_diagnostics(tmp_path, monkeypatch, persist_terminal):
    runtime = _runtime(tmp_path)
    host = runtime.workers
    secret = "fixture-token-transport"

    async def lose_worker(*_args, **_kwargs):
        raise PluginWorkerLost(f"lost worker for {secret}")

    async def get_worker(_package):
        return SimpleNamespace(generation=1, request=lose_worker, retire_requested=False)

    async def drop_worker(_digest, _worker):
        pass

    monkeypatch.setattr(host, "_get_worker", get_worker)
    monkeypatch.setattr(host, "_drop_worker", drop_worker)
    transition = host._transition

    def settle(operation_id, expected, status, **kwargs):
        if status == "unknown_effect" and not persist_terminal:
            return host._operation(operation_id)
        return transition(operation_id, expected, status, **kwargs)

    monkeypatch.setattr(host, "_transition", settle)
    try:
        error_type = UnknownPluginEffect if persist_terminal else PluginInvocationFailed
        with pytest.raises(error_type, match="lost worker for") as caught:
            await host.invoke(
                _PACKAGE, _CONTRIBUTION, {"operation": "poll", "token": secret, "credentials": {}},
                contribution_kind="messaging_adapters", context={"surface": "messaging"},
            )
        assert "[redacted]" in str(caught.value)
        assert secret not in "".join(traceback.format_exception(caught.value))
        assert secret not in _database_dump(host.path)
    finally:
        await host.shutdown()
