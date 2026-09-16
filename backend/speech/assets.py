"""Stable user-supplied speech asset paths with development compatibility."""

from __future__ import annotations

import os
from pathlib import Path

from paths import APP_ROOT

KOKORO_MODEL_NAME = "kokoro-v1.0.onnx"
KOKORO_VOICES_NAME = "voices-v1.0.bin"
WHISPER_RUNTIME_FILES = (
    "whisper-server.exe",
    "whisper.dll",
    "ggml.dll",
    "ggml-base.dll",
    "ggml-cpu.dll",
)


def data_root(value: str | None = None) -> Path:
    raw = str(value or os.environ.get("VARIANT1_DATA_DIR") or APP_ROOT).strip()
    return Path(raw).expanduser().resolve()


def whisper_drop_dir(value: str | None = None) -> Path:
    return data_root(value) / "models" / "speech" / "whisper"


def kokoro_drop_dir(value: str | None = None) -> Path:
    return data_root(value) / "models" / "speech" / "kokoro"


def resolve_whisper_binary(
    configured: object,
    *,
    app_root: str = APP_ROOT,
    data_dir: str | None = None,
) -> Path:
    """Resolve a user-supplied whisper-server, then the legacy dev runtime."""

    raw = str(configured or "").strip()
    root = data_root(data_dir)
    candidates: list[Path] = []
    if raw:
        path = Path(raw).expanduser()
        if path.is_absolute():
            candidates.append(path)
        else:
            candidates.extend((root / path, Path(app_root).resolve() / path))
    candidates.extend((
        whisper_drop_dir(str(root)) / "whisper-server.exe",
        Path(app_root).resolve() / "bin" / "whisper" / "whisper-server.exe",
    ))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return candidates[0].resolve()


def whisper_runtime_complete(binary: str | Path) -> bool:
    folder = Path(binary).expanduser().resolve().parent
    return all((folder / name).is_file() for name in WHISPER_RUNTIME_FILES)


def resolve_whisper_model(
    configured: object,
    *,
    app_root: str = APP_ROOT,
    data_dir: str | None = None,
) -> Path:
    """Resolve an explicit path, any dropped ggml bin, then the legacy dev asset."""

    raw = str(configured or "").strip()
    root = data_root(data_dir)
    candidates: list[Path] = []
    if raw:
        path = Path(raw).expanduser()
        if path.is_absolute():
            candidates.append(path)
        else:
            candidates.extend((root / path, Path(app_root).resolve() / path))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()

    drop = whisper_drop_dir(str(root))
    if drop.is_dir():
        models = sorted(
            path.resolve() for path in drop.glob("*.bin")
            if path.is_file() and not path.name.endswith(".part")
        )
        if models:
            return models[0]

    legacy = Path(app_root).resolve() / "models" / "base" / "whisper-small.bin"
    if legacy.is_file():
        return legacy.resolve()
    if candidates:
        return candidates[0].resolve()
    return (drop / "whisper.bin").resolve()


def resolve_kokoro_assets(
    *,
    app_root: str = APP_ROOT,
    data_dir: str | None = None,
) -> tuple[Path, Path]:
    """Prefer the user drop folder; retain old repository assets for development."""

    drop = kokoro_drop_dir(data_dir)
    user = (drop / KOKORO_MODEL_NAME, drop / KOKORO_VOICES_NAME)
    if all(path.is_file() for path in user):
        return tuple(path.resolve() for path in user)

    legacy = Path(app_root).resolve() / "models" / "base" / "kokoro"
    old = (legacy / KOKORO_MODEL_NAME, legacy / KOKORO_VOICES_NAME)
    if all(path.is_file() for path in old):
        return tuple(path.resolve() for path in old)
    return tuple(path.resolve() for path in user)


__all__ = [
    "KOKORO_MODEL_NAME",
    "KOKORO_VOICES_NAME",
    "WHISPER_RUNTIME_FILES",
    "data_root",
    "kokoro_drop_dir",
    "resolve_kokoro_assets",
    "resolve_whisper_binary",
    "resolve_whisper_model",
    "whisper_drop_dir",
    "whisper_runtime_complete",
]
