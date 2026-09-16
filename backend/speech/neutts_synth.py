"""Isolated NeuTTS helper; the heavyweight local model exits after one call."""

from __future__ import annotations

import argparse
import wave
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--ref-audio", required=True)
    parser.add_argument("--ref-text", required=True)
    parser.add_argument("--model", default="neuphonic/neutts-air-q4-gguf")
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args()
    import numpy as np
    from neutts import NeuTTS
    ref_text = Path(args.ref_text).read_text(encoding="utf-8").strip()
    backbone_device = "gpu" if args.device == "cuda" else args.device
    engine = NeuTTS(backbone_repo=args.model, backbone_device=backbone_device,
                    codec_repo="neuphonic/neucodec", codec_device=args.device)
    codes = engine.encode_reference(str(Path(args.ref_audio)))
    samples = np.asarray(engine.infer(args.text, codes, ref_text), dtype=np.float32).reshape(-1)
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(pcm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

