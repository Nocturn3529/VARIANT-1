VARIANT-1 user-supplied speech models

STT: drop a complete Windows whisper.cpp server distribution into whisper/.
It must include whisper-server.exe, its adjacent DLLs, and a compatible ggml *.bin model.
VARIANT-1 uses the configured model filename when present, otherwise the first *.bin.

TTS: drop a compatible Kokoro pair into kokoro/ using these exact names:
  kokoro-v1.0.onnx
  voices-v1.0.bin

Use Settings > General > Voice to refresh availability.
