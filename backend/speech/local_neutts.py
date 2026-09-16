"""Shared-owner subprocess driver for one isolated NeuTTS synthesis."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def synthesize(text: str, options: dict, model: str) -> bytes:
    ref_audio = str(options.get("ref_audio") or "").strip()
    ref_text = str(options.get("ref_text") or "").strip()
    if not Path(ref_audio).is_file() or not Path(ref_text).is_file():
        raise RuntimeError("NeuTTS needs ref_audio and ref_text files")
    script = Path(__file__).with_name("neutts_synth.py")
    with tempfile.TemporaryDirectory(prefix="variant1-neutts-") as folder:
        output = Path(folder) / "speech.wav"
        result = subprocess.run(
            [sys.executable, str(script), "--text", text, "--out", str(output),
             "--ref-audio", ref_audio, "--ref-text", ref_text, "--model", model,
             "--device", str(options.get("device") or "cpu")],
            capture_output=True,
            timeout=180,
            creationflags=(0x08000000 if os.name == "nt" else 0),
        )
        if result.returncode or not output.is_file():
            detail = (result.stderr or b"").decode("utf-8", "replace")[-500:]
            raise RuntimeError(f"NeuTTS synthesis failed: {detail}")
        return output.read_bytes()

