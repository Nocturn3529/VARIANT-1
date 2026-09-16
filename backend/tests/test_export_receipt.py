import hashlib
import json
from types import SimpleNamespace

import pytest

from artifacts import ContentAddressedArtifactStore
from artifacts.blob_service import ArtifactBlobService
from artifacts.capabilities import _export_receipt_view, _artifact_handle_router
from kernel_runtime.worker_bridge import _decode_host_result
from tests.test_browser_fabric_phase4 import capability_context, FakeCapabilityBroker
from work_fabric.scope import WorkScope


@pytest.mark.asyncio
async def test_export_receipt_is_exact_scoped_and_deliverable_without_retyping(tmp_path):
    store = ContentAddressedArtifactStore(str(tmp_path/'cas'))
    scope = WorkScope(chat_id='receipt-owner')
    payload = b'Release checklist: build, test, review.\n'
    ref = store.put_bytes(payload, kind='download', scope=scope.chat_id).ref
    blobs = ArtifactBlobService(store)
    output = tmp_path/'release.txt'
    result = blobs.save(ref, str(output), scope=scope)
    receipt = json.loads(blobs.read_bytes(result['receipt_ref'], scope=scope))
    assert receipt['sha256'] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert receipt['bytes'] == len(payload) and receipt['destination'] == str(output.resolve())
    assert receipt['verified'] is True
    with pytest.raises(PermissionError):
        blobs.read_bytes(result['receipt_ref'], scope=WorkScope(chat_id='other'))
    host = SimpleNamespace(require_runtime=lambda: SimpleNamespace(
        artifacts=SimpleNamespace(blobs=blobs), broker=FakeCapabilityBroker()))
    context = capability_context(scope)
    class Bridge:
        async def invoke_async(self, descriptor, value, **kwargs):
            return await _artifact_handle_router(host, context, value['handle'], value['method'], value['arguments'])
    bridge=Bridge()
    decoded=_decode_host_result(_export_receipt_view(host, context, result), bridge)
    assert decoded.sha256 == result['sha256']
    destination=tmp_path/'receipt.json'
    await decoded.receipt.save.async_(str(destination))
    assert json.loads(destination.read_bytes()) == receipt
    # A receipt describes the bytes at export time, not future mutable content.
    output.write_bytes(b'later edit')
    assert json.loads(destination.read_bytes())['sha256'] == hashlib.sha256(payload).hexdigest()


def test_receipt_failure_does_not_misreport_successful_primary_write(tmp_path, monkeypatch):
    store=ContentAddressedArtifactStore(str(tmp_path/'cas')); blobs=ArtifactBlobService(store)
    ref=store.put_bytes(b'ok', kind='fixture', scope='owner').ref
    def unavailable(*args,**kwargs): raise OSError('receipt store unavailable')
    monkeypatch.setattr(store,'put_json',unavailable)
    target=tmp_path/'file'
    result=blobs.save(ref,str(target),scope='owner')
    assert result['verified'] and target.read_bytes()==b'ok'
    assert 'receipt_error' in result and 'receipt_ref' not in result
