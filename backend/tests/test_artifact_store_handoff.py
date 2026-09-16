"""Portable artifact metadata, grants, manifests, and export tests."""

from __future__ import annotations

import hashlib
import base64
from pathlib import Path
import sqlite3

import pytest

from artifacts import (
    ArtifactExport,
    ArtifactIntegrityError,
    ArtifactManifest,
    ArtifactMetadata,
    ContentAddressedArtifactStore,
)
from artifacts.store import ArtifactExportCancellation, ArtifactExportCancelled
from artifacts.blob_service import ArtifactBlobService
from kernel_runtime.output import CellOutputCollector, OutputLimits


def test_stat_returns_typed_object_and_scoped_metadata(tmp_path):
    store = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    saved = store.put_bytes(
        b"portable payload",
        media_type="application/x-variant1-test",
        kind="handoff_fixture",
        scope="chat:source",
    )

    scoped = store.stat(saved.ref, scope="chat:source", verify=True)
    host = store.stat(saved.ref, verify=True)

    assert isinstance(scoped, ArtifactMetadata)
    assert scoped.ref == saved.ref
    assert scoped.sha256 == hashlib.sha256(b"portable payload").hexdigest()
    assert scoped.bytes == len(b"portable payload")
    assert scoped.media_type == "application/x-variant1-test"
    assert scoped.kind == "handoff_fixture"
    assert scoped.scope == "chat:source"
    assert host.scope == ""
    assert host.to_dict()["bytes"] == len(b"portable payload")
    with pytest.raises(PermissionError):
        store.stat(saved.ref, scope="chat:other")


def test_grant_reuses_cas_bytes_and_inherits_the_explicit_source_grant(tmp_path):
    store = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    payload = b"one immutable object"
    source = store.put_bytes(
        payload,
        media_type="application/x-source",
        kind="conversation_attachment",
        scope="chat:source",
    )
    blob = Path(store._path(source.sha256))
    before = (blob.stat().st_size, blob.stat().st_mtime_ns)

    granted = store.grant(
        source.ref,
        "work:workspace-7:revision-3",
        source_scope="chat:source",
    )

    assert granted.ref == source.ref
    assert granted.scope == "work:workspace-7:revision-3"
    assert (blob.stat().st_size, blob.stat().st_mtime_ns) == before
    assert store.read_bytes_scoped(granted.ref, granted.scope) == payload
    target_metadata = store.stat(granted.ref, scope=granted.scope)
    assert target_metadata.media_type == "application/x-source"
    assert target_metadata.kind == "conversation_attachment"
    with pytest.raises(PermissionError):
        store.grant(
            source.ref,
            "work:target",
            source_scope="chat:not-the-owner",
        )
    with pytest.raises(PermissionError):
        store.grant(source.ref, "")


def test_manifest_is_deterministic_bounded_and_cursor_pageable(tmp_path):
    store = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    scope = "work:handoff"
    refs = {
        store.put_text(value, kind="note", scope=scope).ref
        for value in ("charlie", "alpha", "bravo", "delta")
    }

    first = store.manifest(scope, limit=2)
    repeated = store.manifest(scope, limit=2)

    assert isinstance(first, ArtifactManifest)
    assert first == repeated
    assert first.to_dict() == repeated.to_dict()
    assert [item.ref for item in first.items] == sorted(refs)[:2]
    assert first.has_more is True
    assert first.next_after_ref == first.items[-1].ref
    assert len(first.digest) == 64
    assert first.total_bytes == sum(item.bytes for item in first.items)

    second = store.manifest(
        scope,
        limit=5000,
        after_ref=first.next_after_ref,
        verify=True,
    )
    assert [item.ref for item in second.items] == sorted(refs)[2:]
    assert second.has_more is False
    assert second.next_after_ref == ""
    assert not ({item.ref for item in first.items} & {item.ref for item in second.items})
    with pytest.raises(ValueError):
        store.manifest(scope, after_ref="not-an-artifact-ref")


def test_stream_and_verified_atomic_export_support_handoff(tmp_path):
    store = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    payload = bytes(range(251)) * 19
    saved = store.put_bytes(payload, kind="binary_handoff", scope="chat:stream")

    chunks = list(store.iter_bytes(
        saved.ref,
        scope="chat:stream",
        chunk_size=97,
    ))
    assert b"".join(chunks) == payload
    assert max(map(len, chunks)) <= 97
    with store.open_reader(saved.ref, scope="chat:stream") as reader:
        assert reader.read(13) == payload[:13]

    destination = tmp_path / "handoff" / "payload.bin"
    receipt = store.export_to(
        saved.ref,
        destination,
        scope="chat:stream",
        chunk_size=113,
    )
    assert isinstance(receipt, ArtifactExport)
    assert receipt.destination == str(destination.resolve())
    assert receipt.bytes == len(payload)
    assert receipt.sha256 == saved.sha256
    assert receipt.verified is True
    assert destination.read_bytes() == payload
    with pytest.raises(FileExistsError):
        store.export_to(saved.ref, destination, scope="chat:stream")

    replacement = store.export_to(
        saved.ref,
        destination,
        scope="chat:stream",
        overwrite=True,
    )
    assert replacement.to_dict()["verified"] is True
    assert destination.read_bytes() == payload


def test_no_overwrite_export_cannot_replace_concurrent_winner(
    tmp_path, monkeypatch,
):
    store = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    saved = store.put_bytes(b"artifact-bytes", kind="race")
    destination = tmp_path / "race" / "winner.bin"
    original_iter = store.iter_bytes

    def racing_iter(*args, **kwargs):
        yield from original_iter(*args, **kwargs)
        destination.write_bytes(b"concurrent-winner")

    monkeypatch.setattr(store, "iter_bytes", racing_iter)
    with pytest.raises(FileExistsError):
        store.export_to(saved.ref, destination, overwrite=False)
    assert destination.read_bytes() == b"concurrent-winner"
    assert not list(destination.parent.glob(".variant1-artifact-*"))


def test_export_cancellation_prevents_late_publication(tmp_path, monkeypatch):
    store = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    saved = store.put_bytes(b"x" * 200_000, kind="cancelled-export")
    destination = tmp_path / "cancelled" / "late.bin"
    cancellation = ArtifactExportCancellation()
    original_iter = store.iter_bytes

    def cancelling_iter(*args, **kwargs):
        for index, chunk in enumerate(original_iter(*args, **kwargs)):
            yield chunk
            if index == 0:
                cancellation.cancel()

    monkeypatch.setattr(store, "iter_bytes", cancelling_iter)
    with pytest.raises(ArtifactExportCancelled):
        store.export_to(
            saved.ref, destination, cancellation=cancellation,
        )
    assert not destination.exists()
    assert not list(destination.parent.glob(".variant1-artifact-*"))


def test_verification_detects_corruption_and_export_cleans_partial_file(tmp_path):
    store = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    saved = store.put_bytes(b"expected", kind="integrity", scope="chat:source")
    Path(store._path(saved.sha256)).write_bytes(b"tampered")

    with pytest.raises(ArtifactIntegrityError):
        store.stat(saved.ref, scope="chat:source", verify=True)
    with pytest.raises(ArtifactIntegrityError):
        b"".join(store.iter_bytes(saved.ref, scope="chat:source", chunk_size=3))

    destination = tmp_path / "exports" / "must-not-exist.bin"
    with pytest.raises(ArtifactIntegrityError):
        store.export_to(saved.ref, destination, scope="chat:source")
    assert not destination.exists()
    assert not list(destination.parent.glob(".variant1-artifact-*"))


def test_stream_chunk_size_is_explicitly_bounded(tmp_path):
    store = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    saved = store.put_bytes(b"x", kind="bounds")

    with pytest.raises(ValueError):
        store.iter_bytes(saved.ref, chunk_size=0)
    with pytest.raises(ValueError):
        store.iter_bytes(saved.ref, chunk_size=4 * 1024 * 1024 + 1)


def test_existing_grant_database_is_backfilled_without_rewriting_blob(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    payload = b"created by the pre-metadata store"
    digest = hashlib.sha256(payload).hexdigest()
    ref = f"artifact://sha256/{digest}"
    blob = root / digest[:2] / digest[2:4] / digest
    blob.parent.mkdir(parents=True)
    blob.write_bytes(payload)
    before = blob.stat().st_mtime_ns
    with sqlite3.connect(root / "artifact-grants.sqlite3") as conn:
        conn.execute(
            """
            CREATE TABLE artifact_grant (
                scope TEXT NOT NULL,
                ref TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                bytes INTEGER NOT NULL,
                media_type TEXT NOT NULL,
                kind TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY(scope, ref)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO artifact_grant(
                scope, ref, sha256, bytes, media_type, kind, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "chat:legacy",
                ref,
                digest,
                len(payload),
                "application/x-legacy",
                "legacy_attachment",
                123.0,
            ),
        )

    store = ContentAddressedArtifactStore(str(root))
    metadata = store.stat(ref, verify=True)

    assert metadata.media_type == "application/x-legacy"
    assert metadata.kind == "legacy_attachment"
    assert metadata.created_at == 123.0
    assert blob.stat().st_mtime_ns == before


def test_identical_bytes_keep_each_scoped_semantic_kind(tmp_path):
    store = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    first = store.put_json(
        {"value": 1}, kind="generic_json", scope="chat:semantic"
    )
    second = store.put_json(
        {"value": 1}, kind="kernel_capsule_pointer", scope="chat:semantic"
    )
    assert first.ref == second.ref
    assert [row["ref"] for row in store.list_scope(
        "chat:semantic", kind="generic_json"
    )] == [first.ref]
    assert [row["ref"] for row in store.list_scope(
        "chat:semantic", kind="kernel_capsule_pointer"
    )] == [second.ref]
    assert store.stat(second, scope="chat:semantic").kind == "kernel_capsule_pointer"


def test_put_repairs_existing_corrupt_content_addressed_file(tmp_path):
    store = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    saved = store.put_bytes(b"correct payload", kind="repair", scope="chat:repair")
    target = Path(store._path(saved.sha256))
    target.write_bytes(b"corrupt")
    repaired = store.put_bytes(
        b"correct payload", kind="repair", scope="chat:repair"
    )
    assert repaired.ref == saved.ref
    assert target.read_bytes() == b"correct payload"
    assert store.stat(saved.ref, scope="chat:repair", verify=True).bytes == 15


def test_blob_service_uses_workspace_goal_and_artifact_scope_encoding(tmp_path):
    store = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    blobs = ArtifactBlobService(store)
    workspace = store.put_text(
        "workspace body", kind="text", scope="workspace:workspace-a"
    )
    goal = store.put_text("goal body", kind="text", scope="goal:goal-a")
    artifact = store.put_text(
        "artifact body", kind="text", scope="artifact:artifact-a"
    )
    assert blobs.read_text(
        workspace.ref, scope={"workspace_id": "workspace-a"}
    )["text"] == "workspace body"
    assert blobs.read_text(
        goal.ref, scope={"goal_id": "goal-a"}
    )["text"] == "goal body"
    assert blobs.read_text(
        artifact.ref, scope={}, artifact_id="artifact-a"
    )["text"] == "artifact body"


def test_retained_kernel_image_chunk_keeps_image_media_type(tmp_path):
    store = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    collector = CellOutputCollector(
        limits=OutputLimits(max_mime_bytes=1),
        artifact_store=store,
        artifact_scope="chat:image",
    )
    collector.accept_event({
        "type": "display",
        "data": {
            "image/png": base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode("ascii")
        },
    })
    chunk = collector.result.chunks[0]
    assert chunk["kind"] == "artifact_ref"
    assert chunk["media_type"] == "image/png"
    assert chunk["artifact_ref"].startswith("artifact://sha256/")
