"""Inference runtime catalog and persisted endpoint configuration.

VARIANT-1 bundles and manages llama.cpp.  Higher-throughput runtimes are exposed
through their OpenAI-compatible HTTP servers so the desktop/backend package
does not need to absorb their mutually incompatible Python/CUDA environments.
The catalog is the single backend authority for what Settings may configure.
"""

from __future__ import annotations

from copy import deepcopy
import os
import platform
import sys
from urllib.parse import urlparse


RUNTIME_MANIFESTS = {
    "llamacpp": {
        "display_name": "llama.cpp",
        "description": "Bundled GGUF runtime for the zero-setup local path.",
        "bundled": True,
        "managed": True,
        "model_format": "GGUF",
        "default_endpoint": "",
        "default_context_size": 8192,
        "setup": {
            "kind": "bundled",
            "summary": "Installed with VARIANT-1 and started automatically.",
            "docs_url": "https://github.com/ggml-org/llama.cpp",
            "steps": [],
        },
    },
    "vllm": {
        "display_name": "vLLM",
        "description": "High-throughput CUDA inference through a vLLM OpenAI server.",
        "bundled": False,
        "managed": False,
        "model_format": "Hugging Face",
        "default_endpoint": "http://127.0.0.1:8000",
        "default_context_size": 32768,
        "setup": {
            "kind": "guided",
            "summary": "Run vLLM in Linux/WSL or on another machine, then connect its endpoint.",
            "docs_url": "https://docs.vllm.ai/en/latest/getting_started/installation/",
            "steps": [
                "Install vLLM in a dedicated Linux Python environment (WSL works on Windows).",
                "Start: vllm serve <model> --host 0.0.0.0 --port 8000",
                "Enter the endpoint and served model below, then check the connection.",
            ],
        },
    },
    "sglang": {
        "display_name": "SGLang",
        "description": "High-throughput CUDA inference through an SGLang OpenAI server.",
        "bundled": False,
        "managed": False,
        "model_format": "Hugging Face",
        "default_endpoint": "http://127.0.0.1:30000",
        "default_context_size": 32768,
        "setup": {
            "kind": "guided",
            "summary": "Run SGLang in Linux/WSL or on another machine, then connect its endpoint.",
            "docs_url": "https://docs.sglang.ai/start/install.html",
            "steps": [
                "Install SGLang in a dedicated Linux Python environment (WSL works on Windows).",
                "Start: python -m sglang.launch_server --model-path <model> --port 30000",
                "Enter the endpoint and served model below, then check the connection.",
            ],
        },
    },
    "mlx": {
        "display_name": "MLX",
        "description": "Apple Silicon inference through an MLX OpenAI-compatible server.",
        "bundled": False,
        "managed": False,
        "model_format": "MLX / Hugging Face",
        "default_endpoint": "http://127.0.0.1:8080",
        "default_context_size": 32768,
        "setup": {
            "kind": "guided",
            "summary": "Run MLX on Apple Silicon or connect to an MLX server on another machine.",
            "docs_url": "https://github.com/ml-explore/mlx-lm",
            "steps": [
                "On Apple Silicon, install mlx-lm in a dedicated Python environment.",
                "Start: mlx_lm.server --model <model> --port 8080",
                "Enter the endpoint and served model below, then check the connection.",
            ],
        },
    },
    "openai_compatible": {
        "display_name": "OpenAI-compatible",
        "description": "Connect any user-managed local or LAN OpenAI-compatible server.",
        "bundled": False,
        "managed": False,
        "model_format": "Runtime-defined",
        "default_endpoint": "",
        "default_context_size": 8192,
        "setup": {
            "kind": "configure",
            "summary": "Start the server yourself, then provide its base URL and model ID.",
            "docs_url": "",
            "steps": [
                "Expose GET /v1/models and POST /v1/chat/completions.",
                "Enter the base endpoint without /v1 and the exact served model ID.",
                "Optionally name an environment variable containing a bearer token.",
            ],
        },
    },
}


def selected_runtime_id(cfg: dict) -> str:
    inference = cfg.get("inference") if isinstance(cfg, dict) else None
    runtime_id = str((inference or {}).get("runtime") or "llamacpp").strip().lower()
    return runtime_id if runtime_id in RUNTIME_MANIFESTS else "llamacpp"


def runtime_config(cfg: dict, runtime_id: str) -> dict:
    runtime_id = normalize_runtime_id(runtime_id)
    inference = cfg.get("inference") if isinstance(cfg, dict) else None
    runtimes = (inference or {}).get("runtimes")
    stored = (runtimes or {}).get(runtime_id)
    stored = stored if isinstance(stored, dict) else {}
    manifest = RUNTIME_MANIFESTS[runtime_id]
    return {
        "endpoint": str(stored.get("endpoint") or manifest["default_endpoint"]).strip(),
        "model": str(stored.get("model") or "").strip(),
        "context_size": _context_size(
            stored.get("context_size"), manifest["default_context_size"]),
        "api_key_env": str(stored.get("api_key_env") or "").strip(),
    }


def configure_runtime(cfg: dict, runtime_id: str, updates: dict) -> dict:
    runtime_id = normalize_runtime_id(runtime_id)
    if runtime_id == "llamacpp":
        raise ValueError("llama.cpp is configured by VARIANT-1's local model controls")
    updates = updates if isinstance(updates, dict) else {}
    current = runtime_config(cfg, runtime_id)
    if "endpoint" in updates:
        current["endpoint"] = validate_endpoint(updates.get("endpoint"))
    if "model" in updates:
        current["model"] = str(updates.get("model") or "").strip()[:300]
    if "context_size" in updates:
        current["context_size"] = _context_size(
            updates.get("context_size"), current["context_size"])
    if "api_key_env" in updates:
        env_name = str(updates.get("api_key_env") or "").strip()
        if env_name and not env_name.replace("_", "A").isalnum():
            raise ValueError("API key environment variable must contain only letters, numbers, and underscores")
        current["api_key_env"] = env_name[:120]
    inference = cfg.setdefault("inference", {})
    if not isinstance(inference, dict):
        inference = {}
        cfg["inference"] = inference
    runtimes = inference.setdefault("runtimes", {})
    if not isinstance(runtimes, dict):
        runtimes = {}
        inference["runtimes"] = runtimes
    runtimes[runtime_id] = current
    return deepcopy(current)


def set_selected_runtime(cfg: dict, runtime_id: str) -> None:
    runtime_id = normalize_runtime_id(runtime_id)
    inference = cfg.setdefault("inference", {})
    if not isinstance(inference, dict):
        inference = {}
        cfg["inference"] = inference
    inference["runtime"] = runtime_id


def runtime_catalog(cfg: dict, engine=None) -> dict:
    selected = selected_runtime_id(cfg)
    current_id = str(getattr(engine, "runtime_id", selected) or selected)
    items = []
    for runtime_id, source in RUNTIME_MANIFESTS.items():
        item = deepcopy(source)
        configured = runtime_config(cfg, runtime_id)
        native = _native_install_support(runtime_id)
        active = runtime_id == current_id
        configuration_applied = bool(
            active and (
                runtime_id == "llamacpp"
                or (
                    str(getattr(engine, "endpoint", "") or "").rstrip("/")
                    == configured["endpoint"].rstrip("/")
                    and str(getattr(engine, "model", "") or "") == configured["model"]
                )
            )
        )
        item.update({
            "id": runtime_id,
            "selected": runtime_id == selected,
            "active": active,
            "configuration_applied": configuration_applied,
            "configured": runtime_id == "llamacpp" or bool(
                configured["endpoint"] and configured["model"]),
            "ready": bool(
                active and getattr(engine, "ready", False)),
            "endpoint": configured["endpoint"],
            "model": configured["model"],
            "context_size": configured["context_size"],
            "api_key_env": configured["api_key_env"],
            "platform": platform_id(),
            "native_install_supported": native["supported"],
            "native_install_detail": native["detail"],
            "credential_available": bool(
                configured["api_key_env"] and os.getenv(configured["api_key_env"])),
        })
        items.append(item)
    return {
        "type": "inference:runtimes",
        "selected": selected,
        "items": items,
    }


def normalize_runtime_id(runtime_id: str) -> str:
    value = str(runtime_id or "").strip().lower()
    if value not in RUNTIME_MANIFESTS:
        raise ValueError(f"unknown inference runtime: {value or '(empty)'}")
    return value


def validate_endpoint(value) -> str:
    endpoint = str(value or "").strip().rstrip("/")
    if not endpoint:
        return ""
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("endpoint must be an http:// or https:// URL")
    if endpoint.endswith("/v1"):
        endpoint = endpoint[:-3].rstrip("/")
    return endpoint


def _context_size(value, default: int) -> int:
    try:
        return min(1_048_576, max(2048, int(value)))
    except (TypeError, ValueError):
        return int(default)


def platform_id() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _native_install_support(runtime_id: str) -> dict:
    machine = platform.machine().lower()
    if runtime_id == "llamacpp":
        return {"supported": True, "detail": "Bundled with VARIANT-1."}
    if runtime_id in {"vllm", "sglang"}:
        if sys.platform.startswith("linux"):
            return {"supported": True, "detail": "Native Linux Python/CUDA environment."}
        if sys.platform.startswith("win"):
            return {"supported": False, "detail": "Use WSL2/Linux or a remote server; native Windows is not bundled."}
        return {"supported": False, "detail": "Use a Linux/NVIDIA server."}
    if runtime_id == "mlx":
        supported = sys.platform == "darwin" and machine in {"arm64", "aarch64"}
        return {
            "supported": supported,
            "detail": "Native on Apple Silicon." if supported else "Use an Apple Silicon server.",
        }
    return {"supported": True, "detail": "Any reachable OpenAI-compatible endpoint."}


__all__ = [
    "RUNTIME_MANIFESTS",
    "configure_runtime",
    "platform_id",
    "runtime_catalog",
    "runtime_config",
    "selected_runtime_id",
    "set_selected_runtime",
    "validate_endpoint",
]
