from __future__ import annotations

import io
import json
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

    installed = llama_runtime.install_runtime(
        str(tmp_path), tag="b10679", backend="cpu",
    )
    again = llama_runtime.install_runtime(
        str(tmp_path), tag="b10679", backend="cpu",
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
