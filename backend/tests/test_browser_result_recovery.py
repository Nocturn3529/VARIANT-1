"""Regression workflows from the stopped September desktop evaluation."""

import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from artifacts.blob_service import ArtifactBlobService
from artifacts.blob_handles import blob_handle_envelope
from artifacts.capabilities import _artifact_handle_router
from browser_fabric import AdapterDownload, EmbeddedBrowserAdapter, BrowserScopeMismatch
from browser_fabric.capabilities import _action_result, _observation_result
from kernel_runtime.worker_bridge import _decode_host_result
from tests.test_browser_fabric_phase4 import (
    browser_runtime, capability_context, FakeCapabilityBroker,
)
from work_fabric.scope import WorkScope


def host_for(fabric):
    runtime = SimpleNamespace(
        browser=fabric, broker=FakeCapabilityBroker(),
        artifacts=SimpleNamespace(blobs=ArtifactBlobService(fabric.artifact_store)),
    )
    return SimpleNamespace(require_runtime=lambda: runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["navigate", "click", "keys", "evaluate"])
async def test_download_event_committed_during_action_is_in_its_reply(browser_runtime, action):
    fabric, adapters = browser_runtime
    scope = WorkScope(chat_id="download-reply")
    session = await fabric.open_session(scope=scope)
    adapter = adapters[0]
    original = adapter.perform

    async def perform(target, method, params):
        result = await original(target, method, params)
        assert await fabric._record_adapter_download(
            session.session_id, target, params["_operation_id"],
            AdapterDownload("report.bin", "https://download.test/report", b"original download"),
        )
        return result

    adapter.perform = perform
    params = {"url": "https://download.test"}
    if action == "keys":
        params["keys"] = "Enter"
    result = await fabric.perform(fabric.page_ref(session.session_id), action,
                                 params=params, scope=scope)
    assert result["download_state"] == "completed"
    assert result["download"]["operation_id"] == result["operation_id"]
    assert result["downloads"] == [result["download"]]
    assert fabric.artifact_store.read_bytes(result["download"]["artifact_ref"]) == b"original download"
    assert len(adapter.perform_calls) == 1


@pytest.mark.asyncio
async def test_delayed_native_progress_completion_and_replay_keep_original_operation(browser_runtime, tmp_path):
    fabric, adapters = browser_runtime
    scope = WorkScope(chat_id="late-download")
    session = await fabric.open_session(kind="embedded", headless=False, scope=scope)
    page = fabric.page_ref(session.session_id)
    original = await fabric.navigate(page, "https://download.test/file", scope=scope, idempotency_key="once")
    assert original["download_state"] == "not_observed_yet"
    later = await fabric.evaluate(fabric.page_ref(session.session_id), "document.title", scope=scope)
    target = fabric.store.get_target(page.target_id)
    raw = {"download_id": "native-late", "operation_id": original["operation_id"],
           "status": "in_progress", "bytes": 3, "total_bytes": 19,
           "suggested_filename": "fixture.bin", "url": "https://download.test/file"}
    acknowledged = False

    async def request(command):
        nonlocal acknowledged
        if command["action"] == "downloads":
            return {"downloads": [dict(raw)] if not acknowledged else []}
        if command["action"] == "drain_downloads":
            return {"downloads": [dict(raw)] if raw["status"] == "completed" and not acknowledged else []}
        if command["action"] == "ack_downloads":
            acknowledged = True
            return {"ok": True}
        raise AssertionError("history/replay must not repeat a browser action")

    embedded = EmbeddedBrowserAdapter(request)
    embedded.set_download_sink(lambda target_id, operation_id, download:
        fabric._record_adapter_download(session.session_id, target_id, operation_id, download))
    fabric._adapters[session.session_id] = embedded
    await fabric.refresh_downloads(session.session_id)
    rows = fabric.download_history(session.session_id, operation_id=original["operation_id"], scope=scope)
    assert rows[0]["state"] == "in_progress" and rows[0]["artifact_ref"] is None
    assert not fabric.download_history(session.session_id, operation_id=later["operation_id"], scope=scope)
    # Another chat cannot acquire this owned session's download history.
    with pytest.raises(BrowserScopeMismatch):
        fabric.download_history(session.session_id, scope=WorkScope(chat_id="other-chat"))
    raw.update(status="error", error="Could not stage the download.")
    await fabric.refresh_downloads(session.session_id)
    failed = fabric._operation_downloads(fabric.store.get_operation(original["operation_id"]))
    assert failed["download_state"] == "failed"
    assert failed["downloads"][0]["error"] == "Could not stage the download."
    payload = b"original downloaded file"
    from pathlib import Path
    staging = Path(fabric.download_staging_root)
    staging.mkdir(parents=True, exist_ok=True)
    path = staging / "late.bin"
    path.write_bytes(payload)
    # A native completion can precede a failed backend handoff, as in PB05.
    # Expose the actual failure rather than leaving it perpetually finalizing.
    outside = tmp_path / "outside-staging.bin"
    outside.write_bytes(payload)
    raw.update(status="completed", path=str(outside), bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    await fabric.refresh_downloads(session.session_id)
    failed_rows = fabric.download_history(session.session_id, operation_id=original["operation_id"], scope=scope)
    assert failed_rows[0]["state"] == failed_rows[0]["status"] == "failed"
    assert failed_rows[0]["native_status"] == "completed"
    assert "outside native download staging" in failed_rows[0]["error"]
    assert "do not repeat the download" in failed_rows[0]["recovery"]
    assert failed_rows[0]["artifact_ref"] is None and not acknowledged
    assert fabric._operation_downloads(fabric.store.get_operation(original["operation_id"]))["download_state"] == "failed"
    with pytest.raises(BrowserScopeMismatch):
        fabric.download_history(session.session_id, scope=WorkScope(chat_id="other-chat"))
    await fabric.refresh_downloads(session.session_id)
    repeated = fabric.download_history(session.session_id, operation_id=original["operation_id"], scope=scope)[0]
    events = [event for event in fabric.events(session.session_id) if event.kind == "download.record_failed"]
    assert len(events) == 1
    assert repeated["registration_attempts"] == 2
    assert repeated["diagnostic_event_id"] == failed_rows[0]["diagnostic_event_id"] == events[0].event_id
    assert events[0].payload["native_download_id"] == "native-late"
    assert events[0].payload["operation_id"] == original["operation_id"]
    raw.update(status="completed", path=str(path), bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    await fabric.refresh_downloads(session.session_id)
    replay = await fabric.navigate(page, "https://download.test/file", scope=scope, idempotency_key="once")
    assert replay["download_state"] == "completed"
    assert len(replay["downloads"]) == 1
    assert replay["download"]["operation_id"] == original["operation_id"]
    assert replay["download"]["artifact_ref"]
    assert acknowledged and len(adapters[0].perform_calls) == 2
    assert len(fabric.download_history(session.session_id, scope=scope)) == 1
    assert not fabric.download_history(session.session_id, scope=scope)[0].get("error")


@pytest.mark.asyncio
async def test_browser_images_export_original_bytes_through_retained_handles(browser_runtime, tmp_path):
    fabric, _ = browser_runtime
    scope = WorkScope(chat_id="image-owner")
    context = capability_context(scope)
    host = host_for(fabric)
    session = await fabric.open_session(kind="embedded", headless=False, scope=scope)
    page = fabric.page_ref(session.session_id)
    observation = await fabric.observe(page, include_screenshot=True, scope=scope)
    result = _observation_result(host, context, fabric, observation)
    assert result["surface"] == "VARIANT-1 in-app browser"
    calls = []

    class Bridge:
        async def invoke_async(self, descriptor, payload, **_kwargs):
            calls.append(payload)
            return await _artifact_handle_router(host, context, payload["handle"], payload["method"], payload["arguments"])

    bridge = Bridge()
    decoded = _decode_host_result(result, bridge)
    assert decoded.operation_id is decoded["operation_id"] is decoded.get("operation_id") is None
    assert decoded.observation_id == observation.observation_id
    assert "operation_id=" not in repr(decoded)
    image = decoded.image
    assert image.ref == image["ref"] == image.get("ref")
    assert image.size == len(b"fake-png") and image.bytes == image.size
    # It has no dependency on a currently injected artifacts root or Explore mount.
    context = replace(context, mount_revision=context.mount_revision + 1)
    assert await image.read_bytes.async_() == b"fake-png"
    destination = tmp_path / "original.png"
    receipt = await image.save.async_(str(destination))
    assert receipt["verified"] and destination.read_bytes() == b"fake-png"
    assert all(call["handle"]["service"] == "artifacts" for call in calls)
    context = capability_context(WorkScope(chat_id="different-owner"))
    with pytest.raises(PermissionError):
        await image.call_async("read_bytes")
    with pytest.raises(PermissionError):
        blob_handle_envelope(host, context, image.ref)


@pytest.mark.asyncio
async def test_browser_action_exposes_page_value_download_and_export_without_nested_guessing(browser_runtime, tmp_path):
    fabric, _ = browser_runtime
    scope = WorkScope(chat_id="action-view")
    context = capability_context(scope)
    host = host_for(fabric)
    session = await fabric.open_session(scope=scope)
    page = fabric.page_ref(session.session_id)
    result = await fabric.perform(page, "keys", params={"keys": "Enter", "expect_download": True}, scope=scope)
    projected = _action_result(host, context, fabric, session.session_id, page.target_id, result)
    class Bridge:
        pass
    bridge = Bridge()
    decoded = _decode_host_result(projected, bridge)
    assert decoded.value == decoded.get("value") == decoded["value"]
    assert decoded.page.url == decoded.url
    assert decoded.session_id == decoded["session_id"] == decoded.get("session_id") == session.session_id
    assert decoded.target_id == decoded["target_id"] == decoded.get("target_id") == page.target_id
    assert decoded.page.session_id == decoded.session_id
    assert decoded.page.id == decoded.target_id
    assert decoded.download["artifact"].size == len(b"download")
    assert decoded.download["artifact"].ref == decoded.download["artifact_ref"]
