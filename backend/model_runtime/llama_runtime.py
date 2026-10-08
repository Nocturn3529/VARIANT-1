"""Managed acquisition of the llama.cpp server runtime.

VARIANT-1 does not download model weights.  This module only installs the
platform runtime into writable application data, verifies the extracted
``llama-server`` executable, and records an immutable manifest.  The release
tag is pinned by the VARIANT-1 build; updates remain an explicit user action.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import uuid
import zipfile
from typing import Callable


from model_runtime.llama_runtime_pins import LLAMA_ASSET_PINS, LLAMA_TAG

DEFAULT_LLAMA_TAG = LLAMA_TAG
RELEASE_URL = "https://github.com/ggml-org/llama.cpp/releases/download/{tag}/{asset}"
WINDOWS_CUDA_VERSION = "13.3"
WINDOWS_ARM64_CUDA_VERSION = "13.4"
_NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0

Progress = Callable[[str, int, int, str], None]
Cancelled = Callable[[], bool]


class LlamaRuntimeError(RuntimeError):
    pass


def runtime_root(data_dir: str) -> Path:
    return Path(data_dir).resolve() / "runtime" / "llamacpp"


def _host_arch() -> str:
    machine = platform.machine().lower()
    ident = os.environ.get("PROCESSOR_IDENTIFIER", "").lower()
    return "arm64" if machine in {"arm64", "aarch64"} or "armv8" in ident else "x64"


def _host_platform(system: str | None = None) -> str:
    current = system or sys.platform
    if current.startswith("win"):
        return "windows"
    if current.startswith("linux"):
        return "linux"
    if current == "darwin":
        return "macos"
    return ""


def available_backends(system: str | None = None, arch: str | None = None) -> list[str]:
    """Compute backends VARIANT-1 can install on this platform, best first."""

    host = _host_platform(system)
    architecture = arch or _host_arch()
    if host == "windows":
        return ["cuda", "vulkan", "cpu"] if architecture == "x64" else ["cuda", "cpu"]
    if host == "linux":
        return ["vulkan", "cpu"]
    if host == "macos":
        return ["metal"] if architecture == "arm64" else ["cpu"]
    return []


def _linux_has_vulkan() -> bool:
    import ctypes.util

    if ctypes.util.find_library("vulkan"):
        return True
    return any(os.path.isfile(path) for path in (
        "/usr/lib/x86_64-linux-gnu/libvulkan.so.1",
        "/usr/lib/aarch64-linux-gnu/libvulkan.so.1",
        "/usr/lib64/libvulkan.so.1",
        "/usr/lib/libvulkan.so.1",
    ))


def recommended_backend(system: str | None = None, arch: str | None = None) -> str:
    host = _host_platform(system)
    architecture = arch or _host_arch()
    if host == "macos":
        return "metal" if architecture == "arm64" else "cpu"
    if host == "linux":
        # Vulkan covers NVIDIA, AMD and Intel GPUs with the vendor driver.
        return "vulkan" if _linux_has_vulkan() else "cpu"
    if host != "windows":
        return "cpu"
    if shutil.which("nvidia-smi.exe") or shutil.which("nvidia-smi"):
        return "cuda"
    # A present Vulkan loader is a useful non-invasive signal for Intel/AMD
    # graphics.  If it is absent, the CPU package is the reliable baseline.
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    if architecture == "x64" and os.path.isfile(os.path.join(system_root, "System32", "vulkan-1.dll")):
        return "vulkan"
    return "cpu"


def resolve_assets(
    tag: str = DEFAULT_LLAMA_TAG,
    backend: str = "auto",
    *,
    arch: str | None = None,
    system: str | None = None,
) -> tuple[str, list[str]]:
    host = _host_platform(system)
    if not host:
        raise LlamaRuntimeError("managed llama.cpp download is not available on this platform")
    architecture = arch or _host_arch()
    if architecture not in {"x64", "arm64"}:
        raise LlamaRuntimeError(f"unsupported architecture: {architecture}")
    selected = str(backend or "auto").strip().lower()
    if selected == "auto":
        selected = recommended_backend(system, architecture)
    if selected not in available_backends(system, architecture):
        raise LlamaRuntimeError(
            f"llama.cpp has no {selected} build for {host} {architecture}; "
            f"choose one of: {', '.join(available_backends(system, architecture))}"
        )
    if host == "windows":
        if selected == "cuda":
            cuda = WINDOWS_ARM64_CUDA_VERSION if architecture == "arm64" else WINDOWS_CUDA_VERSION
            return selected, [
                f"llama-{tag}-bin-win-cuda-{cuda}-{architecture}.zip",
                f"cudart-llama-bin-win-cuda-{cuda}-{architecture}.zip",
            ]
        if selected == "vulkan":
            return selected, [f"llama-{tag}-bin-win-vulkan-x64.zip"]
        return selected, [f"llama-{tag}-bin-win-cpu-{architecture}.zip"]
    if host == "linux":
        if selected == "vulkan":
            return selected, [f"llama-{tag}-bin-ubuntu-vulkan-{architecture}.tar.gz"]
        return selected, [f"llama-{tag}-bin-ubuntu-{architecture}.tar.gz"]
    # macOS: the arm64 build includes Metal; the Intel build runs on the CPU.
    return selected, [f"llama-{tag}-bin-macos-{architecture}.tar.gz"]


def server_binary(folder: Path) -> Path:
    for name in ("llama-server.exe", "llama-server"):
        direct = folder / name
        if direct.is_file():
            return direct
    for name in ("llama-server.exe", "llama-server"):
        matches = sorted(folder.rglob(name)) if folder.is_dir() else []
        matches = [item for item in matches if item.is_file()]
        if len(matches) > 1:
            raise LlamaRuntimeError("runtime archive contains multiple llama-server executables")
        if matches:
            return matches[0]
    raise LlamaRuntimeError(f"llama-server was not found under {folder}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cancelled(cancelled: Cancelled | None) -> None:
    if cancelled is not None and cancelled():
        raise LlamaRuntimeError("llama.cpp runtime download was cancelled")


def _download(
    url: str,
    destination: Path,
    *,
    progress: Progress | None,
    cancelled: Cancelled | None,
    label: str,
) -> None:
    part = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(url, headers={
        "User-Agent": "VARIANT-1 llama.cpp runtime installer",
        "Accept": "application/octet-stream",
    })
    try:
        with urllib.request.urlopen(request, timeout=120) as response, part.open("wb") as output:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                _cancelled(cancelled)
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                done += len(chunk)
                if progress is not None:
                    progress("download", done, total, label)
        os.replace(part, destination)
    finally:
        try:
            part.unlink()
        except FileNotFoundError:
            pass


def _verify_pin(archive: Path, asset: str, pins: dict[str, tuple[str, int]]) -> str:
    """Check a downloaded archive against its pinned digest before extraction."""

    pin = pins.get(asset)
    if pin is None:
        raise LlamaRuntimeError(f"{asset} is not a pinned llama.cpp asset; refusing it")
    digest = _sha256(archive)
    if digest != pin[0]:
        try:
            archive.unlink()
        except OSError:
            pass
        raise LlamaRuntimeError(f"{asset} does not match its pinned SHA-256; the download was discarded")
    return digest


def _safe_extract(
    archive: Path,
    destination: Path,
    *,
    progress: Progress | None,
    cancelled: Cancelled | None,
    label: str,
) -> None:
    root = destination.resolve()
    if archive.name.endswith((".tar.gz", ".tgz")):
        _safe_extract_tar(archive, root, progress=progress, cancelled=cancelled, label=label)
        return
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        total = sum(max(0, int(item.file_size)) for item in members)
        done = 0
        for member in members:
            _cancelled(cancelled)
            target = (root / member.filename).resolve()
            if os.path.commonpath([str(root), str(target)]) != str(root):
                raise LlamaRuntimeError(f"unsafe path in runtime archive: {member.filename}")
            bundle.extract(member, root)
            done += max(0, int(member.file_size))
            if progress is not None:
                progress("extract", done, total, label)


def _safe_extract_tar(
    archive: Path,
    root: Path,
    *,
    progress: Progress | None,
    cancelled: Cancelled | None,
    label: str,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        total = sum(max(0, int(item.size)) for item in members)
        done = 0
        for member in members:
            _cancelled(cancelled)
            target = (root / member.name).resolve()
            if os.path.commonpath([str(root), str(target)]) != str(root):
                raise LlamaRuntimeError(f"unsafe path in runtime archive: {member.name}")
            # The "data" filter refuses absolute paths, links that leave the
            # destination, and device files; it keeps the executable bit.
            try:
                bundle.extract(member, root, filter="data")
            except tarfile.FilterError as exc:
                raise LlamaRuntimeError(f"unsafe entry in runtime archive: {member.name}") from exc
            done += max(0, int(member.size))
            if progress is not None:
                progress("extract", done, total, label)


def verify_install(folder: Path, tag: str) -> str:
    executable = server_binary(folder)
    result = subprocess.run(
        [str(executable), "--version"],
        cwd=str(executable.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        creationflags=_NO_WINDOW,
        check=False,
    )
    output = str(result.stdout or "").strip()
    if result.returncode != 0:
        raise LlamaRuntimeError(f"llama-server --version exited with {result.returncode}")
    if re.search(r"\bversion\s*:?\s*b?" + re.escape(tag.removeprefix("b")) + r"\b", output, re.IGNORECASE) is None:
        raise LlamaRuntimeError(
            f"runtime version mismatch: expected {tag}, received {output[:160] or 'no version'}"
        )
    return output.splitlines()[0] if output else tag


def installed_builds(data_dir: str) -> list[dict]:
    root = runtime_root(data_dir)
    rows = []
    if not root.is_dir():
        return rows
    for manifest_path in root.glob("*/*/manifest.json"):
        if manifest_path.parent.parent.name.startswith("."):
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            executable = server_binary(manifest_path.parent)
            if not manifest.get("verified_version"):
                continue
            rows.append({
                **manifest,
                "binary": str(executable),
                "folder": str(manifest_path.parent),
            })
        except Exception:
            continue
    def number(item: dict) -> int:
        digits = "".join(ch for ch in str(item.get("tag") or "") if ch.isdigit())
        return int(digits or 0)
    return sorted(rows, key=number, reverse=True)


def status(
    data_dir: str,
    *,
    configured_binary: str = "",
    bundled_binary: str = "",
    running_binary: str = "",
    tag: str = DEFAULT_LLAMA_TAG,
) -> dict:
    builds = installed_builds(data_dir)
    selected = next((row for row in builds if row.get("tag") == tag), None)
    configured = os.path.realpath(configured_binary) if configured_binary else ""
    active = os.path.realpath(running_binary) if running_binary else configured
    managed_root = os.path.realpath(str(runtime_root(data_dir)))
    try:
        managed_active = bool(
            active and os.path.commonpath([managed_root, active]) == managed_root
        )
    except ValueError:
        managed_active = False
    bundled_active = bool(
        active and bundled_binary and os.path.isfile(active)
        and os.path.normcase(active) == os.path.normcase(os.path.realpath(bundled_binary))
    )
    custom_active = bool(active and os.path.isfile(active) and not managed_active and not bundled_active)
    managed_installed = selected is not None
    backends = available_backends()
    return {
        "supported": bool(backends),
        "tag": tag,
        "recommended_backend": recommended_backend() if backends else "",
        "available_backends": backends,
        "installed": managed_installed or bundled_active or custom_active,
        "managed_installed": managed_installed,
        "bundled_active": bundled_active,
        "custom_active": custom_active,
        "install_source": (
            "managed" if managed_active
            else "bundled" if bundled_active
            else "custom" if custom_active
            else "managed" if managed_installed
            else "none"
        ),
        "backend": str((selected or {}).get("backend") or ""),
        "version": str((selected or {}).get("verified_version") or ""),
        "binary": str((selected or {}).get("binary") or ""),
        "active_binary": active,
        "configured_binary": configured,
        "running_binary": running_binary,
        "pending_restart": bool(running_binary and os.path.normcase(active) != os.path.normcase(configured)),
        "managed_active": managed_active,
        "update_available": bool(builds and selected is None),
        "installed_builds": builds,
        "runtime_root": managed_root,
    }


def install_runtime(
    data_dir: str,
    *,
    tag: str = DEFAULT_LLAMA_TAG,
    backend: str = "auto",
    progress: Progress | None = None,
    cancelled: Cancelled | None = None,
    pins: dict[str, tuple[str, int]] | None = None,
) -> dict:
    selected, assets = resolve_assets(tag, backend)
    pinned = LLAMA_ASSET_PINS if pins is None else pins
    root = runtime_root(data_dir)
    final = root / tag / selected
    manifest_path = final / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            executable = server_binary(final)
            if manifest.get("verified_version"):
                return {**manifest, "binary": str(executable), "folder": str(final)}
        except Exception:
            pass

    downloads = root / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    staging_parent = root / ".staging"
    staging_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{tag}-{selected}-", dir=staging_parent))
    recorded = {}
    try:
        for index, asset in enumerate(assets, 1):
            _cancelled(cancelled)
            label = f"{index}/{len(assets)}" if len(assets) > 1 else ""
            archive = downloads / asset
            if not archive.is_file():
                _download(
                    RELEASE_URL.format(tag=tag, asset=asset),
                    archive,
                    progress=progress,
                    cancelled=cancelled,
                    label=label,
                )
            if progress is not None:
                progress("verify", 0, 0, label)
            recorded[asset] = _verify_pin(archive, asset, pinned)
            _safe_extract(
                archive, staging,
                progress=progress,
                cancelled=cancelled,
                label=label,
            )
        _cancelled(cancelled)
        if progress is not None:
            progress("verify", 0, 0, "")
        version = verify_install(staging, tag)
        manifest = {
            "tag": tag,
            "backend": selected,
            "assets": recorded,
            "verified_version": version,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            shutil.rmtree(final)
        os.replace(staging, final)
        staging = Path()
        executable = server_binary(final)
        return {**manifest, "binary": str(executable), "folder": str(final)}
    finally:
        if str(staging) not in {"", "."} and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


__all__ = [
    "DEFAULT_LLAMA_TAG",
    "LlamaRuntimeError",
    "available_backends",
    "install_runtime",
    "installed_builds",
    "recommended_backend",
    "resolve_assets",
    "runtime_root",
    "server_binary",
    "status",
    "verify_install",
]
