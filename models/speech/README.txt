VARIANT-1 user-supplied speech models

STT: drop a complete Windows whisper.cpp server distribution into whisper/.
It must include whisper-server.exe, its adjacent DLLs, and a compatible ggml *.bin model.
VARIANT-1 uses the configured model filename when present, otherwise the first *.bin.

TTS: install and start your own Kokoro-compatible speech server.
Set its API base URL in Voice > Kokoro (including /v1), model and voice, then Preview.
The packaged application does not install the engine or weights.
Dropping ONNX/voice files is only supported by the optional source-development engine.

Use Settings > General > Voice to refresh availability.
