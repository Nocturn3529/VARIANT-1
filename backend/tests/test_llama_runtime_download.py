from __future__ import annotations

import hashlib
import io
import json
import tarfile
import zipfile

import pytest

from model_runtime import llama_runtime


def _runtime_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        bundle.writestr("build/bin/llama-server.exe", b"fake runtime")
        bundle.writestr("build/bin/ggml.dll", b"fake library")
    return output.getvalue()


def test_windows_asset_resolution_matches_upstream_release_shape(monkeypatch):
    monkeypatch.setattr(llama_runtime.sys, "platform", "win32")
    backend, assets = llama_runtime.resolve_assets(
        "b10679", "cuda", arch="x64",
    )
    assert backend == "cuda"
    assert assets == [
        "llama-b10679-bin-win-cuda-13.3-x64.zip",
        "cudart-llama-bin-win-cuda-13.3-x64.zip",
    ]
    assert llama_runtime.resolve_assets(
        "b10679", "cpu", arch="arm64",
    )[1] == ["llama-b10679-bin-win-cpu-arm64.zip"]


def test_runtime_install_is_verified_manifested_and_idempotent(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(llama_runtime.sys, "platform", "win32")
    monkeypatch.setattr(
        llama_runtime,
        "resolve_assets",
        lambda tag, backend: ("cpu", [f"llama-{tag}-test.zip"]),
    )
    downloads = []
    archive = _runtime_zip()

    def download(_url, destination, **_kwargs):
        downloads.append(str(destination))
        destination.write_bytes(archive)

    monkeypatch.setattr(llama_runtime, "_download", download)
    monkeypatch.setattr(
        llama_runtime, "verify_install",
        lambda _folder, tag: f"version {tag.lstrip('b')}",
    )

    pins = {"llama-b10679-test.zip": (hashlib.sha256(archive).hexdigest(), len(archive))}
    installed = llama_runtime.install_runtime(
        str(tmp_path), tag="b10679", backend="cpu", pins=pins,
    )
    again = llama_runtime.install_runtime(
        str(tmp_path), tag="b10679", backend="cpu", pins=pins,
    )

    assert installed["binary"].endswith("llama-server.exe")
    assert installed["verified_version"] == "version 10679"
    assert again["binary"] == installed["binary"]
    assert len(downloads) == 1
    manifest = json.loads(
        (tmp_path / "runtime" / "llamacpp" / "b10679" / "cpu" / "manifest.json")
        .read_text(encoding="utf-8")
    )
    assert manifest["tag"] == "b10679"
    assert len(next(iter(manifest["assets"].values()))) == 64


def test_download_that_misses_its_pin_is_discarded_before_extraction(tmp_path, monkeypatch):
    monkeypatch.setattr(
        llama_runtime, "resolve_assets",
        lambda tag, backend: ("cpu", [f"llama-{tag}-test.zip"]),
    )
    archive = _runtime_zip()
    monkeypatch.setattr(
        llama_runtime, "_download",
        lambda _url, destination, **_kwargs: destination.write_bytes(archive),
    )
    with pytest.raises(llama_runtime.LlamaRuntimeError, match="does not match its pinned SHA-256"):
        llama_runtime.install_runtime(
            str(tmp_path), tag="b10679", backend="cpu",
            pins={"llama-b10679-test.zip": ("0" * 64, len(archive))},
        )
    downloads = tmp_path / "runtime" / "llamacpp" / "downloads"
    assert not (downloads / "llama-b10679-test.zip").exists()
    with pytest.raises(llama_runtime.LlamaRuntimeError, match="not a pinned llama.cpp asset"):
        llama_runtime.install_runtime(str(tmp_path), tag="b10679", backend="cpu", pins={})


def test_every_platform_resolves_to_pinned_release_assets():
    from model_runtime.llama_runtime_pins import LLAMA_ASSET_PINS, LLAMA_TAG

    hosts = [("win32", "x64"), ("win32", "arm64"), ("linux", "x64"),
             ("linux", "arm64"), ("darwin", "arm64"), ("darwin", "x64")]
    for system, arch in hosts:
        backends = llama_runtime.available_backends(system, arch)
        assert backends
        for backend in backends:
            _selected, assets = llama_runtime.resolve_assets(
                LLAMA_TAG, backend, arch=arch, system=system,
            )
            assert all(asset in LLAMA_ASSET_PINS for asset in assets), (system, arch, backend)
    assert llama_runtime.available_backends("darwin", "arm64") == ["metal"]
    assert llama_runtime.available_backends("linux", "x64") == ["vulkan", "cpu"]
    with pytest.raises(llama_runtime.LlamaRuntimeError, match="no cuda build for linux"):
        llama_runtime.resolve_assets(LLAMA_TAG, "cuda", arch="x64", system="linux")


def test_tar_runtime_keeps_the_server_executable_and_refuses_escaping_links(tmp_path):
    good = tmp_path / "good.tar.gz"
    with tarfile.open(good, "w:gz") as bundle:
        data = b'#!/bin/sh' + bytes([10]) + b'echo version: 10679' + bytes([10])
        info = tarfile.TarInfo("build/bin/llama-server")
        info.size, info.mode = len(data), 0o755
        bundle.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("build/bin/libllama.so")
        link.type, link.linkname = tarfile.SYMTYPE, "libllama.so.0"
        bundle.addfile(link)
    llama_runtime._safe_extract(good, tmp_path / "ok", progress=None, cancelled=None, label="")
    server = tmp_path / "ok" / "build" / "bin" / "llama-server"
    assert server.read_bytes().startswith(b"#!")
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as bundle:
        link = tarfile.TarInfo("build/bin/evil")
        link.type, link.linkname = tarfile.SYMTYPE, "../../../../outside"
        bundle.addfile(link)
    with pytest.raises(llama_runtime.LlamaRuntimeError, match="unsafe"):
        llama_runtime._safe_extract(bad, tmp_path / "bad", progress=None, cancelled=None, label="")


def test_runtime_archive_cannot_escape_staging_root(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../escape.exe", b"no")
    with pytest.raises(llama_runtime.LlamaRuntimeError, match="unsafe path"):
        llama_runtime._safe_extract(
            archive,
            tmp_path / "target",
            progress=None,
            cancelled=None,
            label="",
        )


def test_active_packaged_runtime_is_reported_as_usable_not_missing(tmp_path):
    binary = tmp_path / "app" / "bin" / "llama-server.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"packaged runtime")

    result = llama_runtime.status(
        str(tmp_path / "data"), configured_binary=str(binary),
        bundled_binary=str(binary),
    )

    assert result["installed"] is True
    assert result["managed_installed"] is False
    assert result["bundled_active"] is True
    assert result["install_source"] == "bundled"
    assert result["active_binary"] == str(binary)
