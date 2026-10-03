from types import SimpleNamespace

import pytest

from kernel_runtime import capsule_worker as module


def capture(namespace, **request):
    worker = module.KernelCapsuleWorker(SimpleNamespace(namespace=namespace, runtime_profile={}),
                                        reinstall_namespace=lambda document: None, document={})
    return worker.capture({"schema": "variant1.kernel-capsule-capture-request.v1", **request})


def test_new_and_reused_values_have_verified_reduced_traversals(monkeypatch):
    original = module._encoded_chunks
    calls = []
    def observed(value, serializer):
        calls.append(serializer)
        yield from original(value, serializer)
    monkeypatch.setattr(module, "_encoded_chunks", observed)
    first = capture({"value": {"items": [1, 2, 3]}})
    assert len(calls) == 2 and first["values"][0]["reused"] is False
    previous = {**first["values"][0], "artifact_ref": "artifact://sha256/" + "a" * 64}
    calls.clear()
    second = capture({"value": {"items": [1, 2, 3]}}, known_values=[previous])
    assert len(calls) == 1 and second["values"][0]["reused"] is True
    assert "data_b64" not in second["values"][0]


@pytest.mark.parametrize("mutated", ["same", "much longer", ""])
def test_second_pass_rejects_size_or_digest_changes(monkeypatch, mutated):
    original = module._encoded_chunks
    calls = 0
    def changing(value, serializer):
        nonlocal calls
        calls += 1
        yield from original("four" if calls == 1 else mutated, serializer)
    monkeypatch.setattr(module, "_encoded_chunks", changing)
    result = capture({"value": "four"})
    assert result["error"]["code"] == "capsule_value_changed_during_capture"


def test_growing_validation_stream_stops_at_captured_bound(monkeypatch):
    calls, consumed = 0, []
    def growing(value, serializer):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield b"four"
        else:
            for index in range(1000):
                consumed.append(index)
                yield b"four"
    monkeypatch.setattr(module, "_encoded_chunks", growing)
    assert capture({"value": "four"})["error"]["code"] == "capsule_value_changed_during_capture"
    assert consumed == [0, 1]


def test_reused_in_place_mutation_is_not_skipped():
    value = [1, 2]
    previous = {**capture({"value": value})["values"][0], "artifact_ref": "retained"}
    value.append(3)
    result = capture({"value": value}, known_values=[previous])
    assert result["values"][0]["reused"] is False
    assert result["values"][0]["sha256"] != previous["sha256"]
