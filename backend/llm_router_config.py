"""LLM config load/save and mode helpers for LLMRouter.

Pure-ish helpers that keep atomic persistence and default merge out of the
router class body. Methods on ``LLMRouter`` remain thin wrappers.
"""

from __future__ import annotations

from copy import deepcopy
import json
import os

# Used only when the config file does not exist or has been quarantined.
DEFAULT_LLM_CONFIG: dict = {
    "mode": "local",
    "local": {},
    "inference": {"runtime": "llamacpp", "runtimes": {}},
    "sampling": {},
}

def load_llm_config(path: str) -> dict:
    """Load configuration without silently overwriting malformed state.

    A missing file starts from defaults. A malformed existing file is moved to
    a collision-safe ``.corrupt`` sibling before defaults are returned, keeping
    encrypted credentials and user settings recoverable. If preservation fails,
    startup fails instead of allowing a later settings write to destroy evidence.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError("LLM config must be an object")
        if cfg.get("mode") not in {"local", "cloud"}:
            cfg["mode"] = "local"
        cfg.pop("fallback", None)
        cfg.pop("subagent_enabled", None)
        cfg.pop("vision", None)
        cfg.pop("events", None)
        local = cfg.get("local")
        if isinstance(local, dict):
            local.pop("reasoning", None)
        cloud = cfg.get("cloud")
        if isinstance(cloud, dict):
            cloud.pop("xai_reasoning_effort", None)
            cloud.pop("openai-codex_reasoning_effort", None)
            for provider in ("xai", "openai-codex"):
                block = cloud.get(provider)
                if isinstance(block, dict):
                    block.pop("reasoning_effort", None)
        voice = cfg.get("voice")
        if isinstance(voice, dict):
            voice.setdefault("stt_provider", "xai" if voice.get("stt_route") == "cloud" else "local")
            voice.setdefault("tts_provider", "xai" if voice.get("tts_route") == "cloud" else "kokoro")
            voice.setdefault("auto_tts", bool(voice.get("tts_enabled", True)))
            voice.setdefault("stt", {})
            voice.setdefault("tts", {})
            for key in ("stt_route", "tts_route", "tts_enabled", "muted",
                        "cloud_voice", "cloud_language"):
                voice.pop(key, None)
        return cfg
    except FileNotFoundError:
        return deepcopy(DEFAULT_LLM_CONFIG)
    except Exception as exc:
        if not path or not os.path.exists(path):
            raise RuntimeError(f"LLM config could not be read: {exc}") from exc
        candidate = path + ".corrupt"
        index = 1
        while os.path.exists(candidate):
            candidate = f"{path}.corrupt.{index}"
            index += 1
        try:
            os.replace(path, candidate)
        except Exception as preserve_exc:
            raise RuntimeError(
                f"LLM config is invalid and could not be preserved: {preserve_exc}"
            ) from exc
        print(
            f"[variant1-backend] invalid llm_config preserved at {candidate}; defaults",
            flush=True,
        )
        return deepcopy(DEFAULT_LLM_CONFIG)


def save_config(
    config_path: str | None,
    cfg: dict,
    *,
    strict: bool = False,
) -> bool:
    """Atomically write cfg to ``config_path``.

    Ordinary preference changes remain best-effort for compatibility. Lifecycle
    transactions pass ``strict=True`` so a candidate model is never reported as
    committed when the durable configuration could not be replaced.
    """
    if not config_path:
        if strict:
            raise RuntimeError("LLM config path is not configured")
        return False
    try:
        # Atomic write: config.json holds the encrypted cloud keys, model
        # selections, mode, and sampling -- a crash mid-write must not
        # truncate it. Temp + os.replace mirrors AutomationStore.save.
        d = os.path.dirname(config_path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, config_path)
        return True
    except Exception as e:
        if strict:
            raise
        print(f"[router] save_config failed: {e}", flush=True)
        return False


def apply_mode(cfg: dict, mode: str) -> bool:
    """Set the user-selected main-model mode. Returns True if applied."""
    if mode not in ("local", "cloud"):
        return False
    cfg["mode"] = mode
    return True


def apply_local_model(cfg: dict, model_path: str, mmproj_path: str = "") -> None:
    """Update local model and projector paths."""
    loc = cfg.setdefault("local", {})
    loc["model"] = model_path
    loc["mmproj"] = mmproj_path or ""
