from pathlib import Path

from speech.assets import (
    resolve_whisper_binary,
    KOKORO_MODEL_NAME,
    KOKORO_VOICES_NAME,
    kokoro_drop_dir,
    resolve_kokoro_assets,
    resolve_whisper_model,
    whisper_drop_dir,
    WHISPER_RUNTIME_FILES,
)
from speech.local_stt import WhisperServer


def test_user_speech_drop_directories_are_under_data_root(tmp_path):
    assert whisper_drop_dir(str(tmp_path)) == tmp_path / "models" / "speech" / "whisper"
    assert kokoro_drop_dir(str(tmp_path)) == tmp_path / "models" / "speech" / "kokoro"


def test_whisper_discovers_any_user_supplied_bin(tmp_path):
    drop = whisper_drop_dir(str(tmp_path))
    drop.mkdir(parents=True)
    selected = drop / "my-private-whisper.bin"
    selected.write_bytes(b"ggml")

    resolved = resolve_whisper_model(
        "models/speech/whisper/whisper.bin",
        app_root=str(tmp_path / "app"),
        data_dir=str(tmp_path),
    )

    assert resolved == selected.resolve()


def test_whisper_server_refreshes_after_a_model_is_dropped(tmp_path):
    app_root = tmp_path / "app"
    binary = app_root / "bin" / "whisper-server.exe"
    binary.parent.mkdir(parents=True)
    for name in WHISPER_RUNTIME_FILES:
        (binary.parent / name).write_bytes(b"runtime")
    server = WhisperServer(
        {"binary": str(binary), "model": "models/speech/whisper/whisper.bin"},
        str(app_root),
        data_root=str(tmp_path),
    )
    assert server.installed() is False

    drop = Path(server.model_drop_dir)
    drop.mkdir(parents=True)
    model = drop / "user-model.bin"
    model.write_bytes(b"ggml")

    assert server.installed() is True
    assert Path(server.model) == model.resolve()


def test_kokoro_requires_the_compatible_user_pair(tmp_path):
    drop = kokoro_drop_dir(str(tmp_path))
    drop.mkdir(parents=True)
    model = drop / KOKORO_MODEL_NAME
    voices = drop / KOKORO_VOICES_NAME
    model.write_bytes(b"onnx")

    selected_model, selected_voices = resolve_kokoro_assets(
        app_root=str(tmp_path / "app"), data_dir=str(tmp_path),
    )
    assert selected_model == model.resolve()
    assert selected_voices == voices.resolve()
    assert selected_voices.is_file() is False

    voices.write_bytes(b"voices")
    selected_model, selected_voices = resolve_kokoro_assets(
        app_root=str(tmp_path / "app"), data_dir=str(tmp_path),
    )
    assert selected_model == model.resolve()
    assert selected_voices == voices.resolve()


def test_whisper_binary_only_adapts_bundled_defaults(tmp_path, monkeypatch):
    app = tmp_path / "app"
    data = tmp_path / "data"
    bundled = app / "bin" / "whisper" / "whisper-server.exe"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"runtime")
    custom_dir = app / "runtimes" / "cpu"
    custom_dir.mkdir(parents=True)
    custom = custom_dir / "whisper-server"
    custom.write_bytes(b"custom")

    monkeypatch.setattr("speech.assets.os.name", "nt")
    # Bundled default pin gets .exe adaptation on Windows.
    resolved_default = resolve_whisper_binary(
        "bin/whisper/whisper-server",
        app_root=str(app),
        data_dir=str(data),
    )
    assert resolved_default == bundled.resolve()

    # Custom same-basename relative must keep parent and configured name.
    resolved_custom = resolve_whisper_binary(
        "runtimes/cpu/whisper-server",
        app_root=str(app),
        data_dir=str(data),
    )
    assert resolved_custom == custom.resolve()


def test_whisper_binary_preserves_absolute_custom(tmp_path):
    abs_bin = tmp_path / "opt" / "whisper-server"
    abs_bin.parent.mkdir(parents=True)
    abs_bin.write_bytes(b"abs")
    bundled = tmp_path / "data" / "models" / "speech" / "whisper" / "whisper-server"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"bundled")
    resolved = resolve_whisper_binary(
        str(abs_bin),
        app_root=str(tmp_path / "app"),
        data_dir=str(tmp_path / "data"),
    )
    assert resolved == abs_bin.resolve()


def test_whisper_binary_preserves_parent_relative_when_bundled_exists(tmp_path):
    app = tmp_path / "workspace" / "app"
    data = tmp_path / "workspace" / "data"
    bundled = data / "models" / "speech" / "whisper" / "whisper-server"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"bundled")
    sibling = tmp_path / "workspace" / "models" / "speech" / "whisper" / "whisper-server"
    sibling.parent.mkdir(parents=True)
    sibling.write_bytes(b"sibling")

    resolved = resolve_whisper_binary(
        "../models/speech/whisper/whisper-server",
        app_root=str(app),
        data_dir=str(data),
    )
    assert resolved == sibling.resolve()


def test_whisper_binary_preserves_repeated_parent_segments(tmp_path):
    app = tmp_path / "outer" / "workspace" / "app"
    data = tmp_path / "outer" / "workspace" / "data"
    bundled = data / "models" / "speech" / "whisper" / "whisper-server"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"bundled")
    uncle = tmp_path / "outer" / "models" / "speech" / "whisper" / "whisper-server"
    uncle.parent.mkdir(parents=True)
    uncle.write_bytes(b"uncle")

    resolved = resolve_whisper_binary(
        "../../models/speech/whisper/whisper-server",
        app_root=str(app),
        data_dir=str(data),
    )
    assert resolved == uncle.resolve()


def test_whisper_binary_dot_slash_still_adapts_bundled_default(tmp_path, monkeypatch):
    app = tmp_path / "app"
    data = tmp_path / "data"
    bundled = app / "bin" / "whisper" / "whisper-server.exe"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"runtime")
    monkeypatch.setattr("speech.assets.os.name", "nt")
    resolved = resolve_whisper_binary(
        "./bin/whisper/whisper-server",
        app_root=str(app),
        data_dir=str(data),
    )
    assert resolved == bundled.resolve()
