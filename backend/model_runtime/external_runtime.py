"""Adapter for user-managed OpenAI-compatible local inference servers."""

from __future__ import annotations

import os
import time

import httpx

from model_runtime.llama_server import LocalEngineError
from model_runtime.runtime_catalog import RUNTIME_MANIFESTS, runtime_config


class ExternalOpenAIRuntime:
    """Engine-compatible facade over a server VARIANT-1 does not own.

    ``start`` means probe and attach; ``stop`` only detaches.  The process is
    never launched or terminated by VARIANT-1, which keeps WSL/Python/CUDA/MLX
    lifecycle under explicit user control.
    """

    managed = False
    supports_llama_extensions = False
    supports_reasoning = False
    autostart = False
    proc = None
    mmproj = ""
    reasoning_budget = 0

    def __init__(self, runtime_id: str, cfg: dict):
        if runtime_id not in RUNTIME_MANIFESTS or runtime_id == "llamacpp":
            raise ValueError(f"unsupported external runtime: {runtime_id}")
        source = runtime_config(cfg, runtime_id)
        manifest = RUNTIME_MANIFESTS[runtime_id]
        self.runtime_id = runtime_id
        self.display_name = manifest["display_name"]
        self.endpoint = source["endpoint"].rstrip("/")
        self.model = source["model"]
        self.api_model = self.model
        self.ctx_size = source["context_size"]
        self.api_key_env = source["api_key_env"]
        self.ready = False
        self.last_error = ""
        self.last_probe_at = 0.0
        self.last_latency_ms = 0.0
        self.discovered_models: list[str] = []

    @property
    def base_url(self) -> str:
        return self.endpoint

    @property
    def request_adapter(self) -> str:
        return f"{self.runtime_id}.openai_chat_completions"

    def poll_process(self) -> bool:
        return self.ready

    def _headers(self) -> dict:
        if not self.api_key_env:
            return {}
        value = str(os.getenv(self.api_key_env) or "").strip()
        return {"Authorization": f"Bearer {value}"} if value else {}

    @property
    def request_headers(self) -> dict:
        return self._headers()

    async def probe(self) -> dict:
        started = time.perf_counter()
        self.last_probe_at = time.time()
        if not self.endpoint:
            self.ready = False
            self.last_error = "Endpoint is not configured."
            return self.probe_status()
        try:
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
                response = await client.get(
                    f"{self.base_url}/v1/models", headers=self._headers())
            response.raise_for_status()
            body = response.json()
            rows = body.get("data") if isinstance(body, dict) else []
            models = []
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, str):
                    models.append(row)
                elif isinstance(row, dict) and row.get("id"):
                    models.append(str(row["id"]))
            self.discovered_models = sorted(set(models))
            if self.model and self.discovered_models and self.model not in self.discovered_models:
                self.ready = False
                self.last_error = (
                    f"Configured model '{self.model}' is not advertised by this endpoint.")
            else:
                self.ready = True
                self.last_error = ""
        except Exception as exc:
            self.ready = False
            self.last_error = str(exc)[:240]
        self.last_latency_ms = max(0.0, (time.perf_counter() - started) * 1000.0)
        return self.probe_status()

    def probe_status(self) -> dict:
        return {
            "runtime_id": self.runtime_id,
            "display_name": self.display_name,
            "ready": self.ready,
            "endpoint": self.endpoint,
            "model": self.model,
            "models": list(self.discovered_models),
            "latency_ms": round(self.last_latency_ms, 2),
            "checked_at": round(self.last_probe_at, 3),
            "error": self.last_error,
        }

    async def start(self) -> None:
        if not self.model:
            raise LocalEngineError(
                f"{self.display_name} needs a served model ID in Settings")
        status = await self.probe()
        if not status["ready"]:
            raise LocalEngineError(
                f"{self.display_name} endpoint is not ready: {status['error']}")

    async def stop(self) -> None:
        self.ready = False

    async def restart(self, model=None, mmproj=None) -> None:
        if model:
            self.model = str(model)
            self.api_model = self.model
        await self.start()

    def _endpoint_url(self, path: str) -> str:
        base = self.base_url.rstrip("/")
        suffix = "/" + str(path or "").lstrip("/")
        if base.endswith("/v1") and suffix.startswith("/v1/"):
            suffix = suffix[3:]
        return base + suffix

    async def count_tokens(self, text: str):
        if not self.ready or not text:
            return None
        attempts = (
            {"content": str(text)},
            {"model": self.api_model, "prompt": str(text)},
        )
        try:
            async with httpx.AsyncClient(timeout=8.0, trust_env=False) as client:
                for payload in attempts:
                    response = await client.post(
                        self._endpoint_url("/tokenize"),
                        json=payload,
                        headers=self._headers(),
                    )
                    if response.status_code != 200:
                        continue
                    body = response.json()
                    tokens = body.get("tokens") if isinstance(body, dict) else None
                    if isinstance(tokens, list):
                        return len(tokens)
                    count = body.get("count") if isinstance(body, dict) else None
                    if isinstance(count, int) and not isinstance(count, bool):
                        return max(0, count)
        except Exception:
            pass
        return None

    async def count_prompt_tokens(self, template_payload: dict):
        if not self.ready or not isinstance(template_payload, dict):
            return None
        try:
            async with httpx.AsyncClient(timeout=8.0, trust_env=False) as client:
                native = await client.post(
                    self._endpoint_url("/v1/chat/completions/input_tokens"),
                    json=template_payload,
                    headers=self._headers(),
                )
                if native.status_code == 200:
                    body = native.json()
                    count = (
                        body.get("input_tokens")
                        if isinstance(body, dict) else None
                    )
                    if isinstance(count, int) and not isinstance(count, bool):
                        return max(0, count)

                rendered = await client.post(
                    self._endpoint_url("/apply-template"),
                    json=template_payload,
                    headers=self._headers(),
                )
                if rendered.status_code != 200:
                    return None
                body = rendered.json()
                prompt = body.get("prompt") if isinstance(body, dict) else None
            return await self.count_tokens(prompt) if isinstance(prompt, str) else None
        except Exception:
            return None

    def runtime_status(self) -> dict:
        return {
            "runtime_id": self.runtime_id,
            "display_name": self.display_name,
            "managed": False,
            "endpoint": self.endpoint,
            "model": self.model,
            "effective_context": self.ctx_size,
            "ready": self.ready,
            "pid": None,
            "last_probe_at": round(self.last_probe_at, 3),
            "latency_ms": round(self.last_latency_ms, 2),
            "discovered_models": list(self.discovered_models),
            "last_error": self.last_error,
        }


__all__ = ["ExternalOpenAIRuntime"]
