"""Optional, conservative per-request budget guard for an isolated canary backend.

Only metadata is persisted. Reservations use uncached high-context API rates;
missing response usage retains the reservation rather than assuming free usage.
This is qualification infrastructure, not a new application quota/default.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import threading

import httpx


class QualificationBudget:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def reserve(self, payload: dict, wire_bytes: int) -> tuple[str, float]:
        with self.lock:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if not math.isfinite(state["max_cost_usd"]) or state["max_cost_usd"] <= 0:
                raise RuntimeError("The specified qualification budget must be finite and positive")
            if payload.get("model") != state["model"]:
                raise RuntimeError("Qualification refuses an unexpected model route")
            effort = (payload.get("reasoning") or {}).get("effort") or payload.get("reasoning_effort")
            if effort != state["reasoning_effort"]:
                raise RuntimeError("Qualification refuses an unexpected reasoning effort")
            maximum = payload.get("max_output_tokens", payload.get("max_tokens"))
            if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum <= 0:
                raise RuntimeError("A bounded output request is required for this specified budget")
            # A text token cannot consume fewer than one UTF-8 byte; special
            # token overhead is reserved separately. Vision is bounded by the
            # model's entire context, rather than guessed image token counts.
            def has_media(value):
                if isinstance(value, dict):
                    return value.get("type") in {"image_url", "input_image", "input_audio", "input_video"} or "image_url" in value or any(has_media(item) for item in value.values())
                return isinstance(value, list) and any(has_media(item) for item in value)
            input_bound = state["context_tokens"] if has_media(payload) else min(state["context_tokens"], wire_bytes + 4096)
            reserved = (input_bound * state["input_per_million"] + maximum * state["output_per_million"]) / 1_000_000
            if state["upper_spend_usd"] + reserved > state["max_cost_usd"]:
                raise RuntimeError("Qualification budget exhausted before provider dispatch")
            request_id = str(len(state["requests"]) + 1)
            state["upper_spend_usd"] += reserved
            state["requests"].append({"id": request_id, "reserved_usd": reserved,
                                      "cost_usd": None, "cost_kind": "unobserved", "usage": None})
            self.path.write_text(json.dumps(state, indent=2), encoding="utf-8")
            return request_id, reserved

    def settle(self, request_id: str, usage: dict | None) -> None:
        if not usage:
            return
        with self.lock:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            row = next(row for row in state["requests"] if row["id"] == request_id)
            if row["cost_kind"] != "unobserved":
                return
            ticks = usage.get("cost_in_usd_ticks")
            if isinstance(ticks, (int, float)) and not isinstance(ticks, bool) and ticks >= 0:
                cost, kind = ticks / 10_000_000_000, "provider_reported"
            else:
                prompt = usage.get("input_tokens", usage.get("prompt_tokens"))
                output = usage.get("output_tokens", usage.get("completion_tokens"))
                if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and x >= 0 for x in (prompt, output)):
                    return
                cost = (prompt * state["input_per_million"] + output * state["output_per_million"]) / 1_000_000
                kind = "conservative_api_equivalent"
            state["upper_spend_usd"] += cost - row["reserved_usd"]
            row.update(cost_usd=cost, cost_kind=kind, usage={key: usage[key] for key in
                ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "cost_in_usd_ticks") if key in usage})
            self.path.write_text(json.dumps(state, indent=2), encoding="utf-8")


class MeteredStream(httpx.AsyncByteStream):
    def __init__(self, stream, budget, request_id):
        self.stream, self.budget, self.request_id = stream, budget, request_id
        self.buffer = b""
        self.usage = None

    async def __aiter__(self):
        async for chunk in self.stream:
            self.buffer += chunk
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                if line.startswith(b"data:"):
                    try:
                        frame = json.loads(line[5:].strip())
                        usage = frame.get("usage") or (frame.get("response") or {}).get("usage")
                        if isinstance(usage, dict): self.usage = usage
                    except (ValueError, TypeError, AttributeError):
                        pass
            yield chunk

    async def aclose(self):
        try:
            await self.stream.aclose()
        finally:
            self.budget.settle(self.request_id, self.usage)


def install_budget(path: Path) -> None:
    budget = QualificationBudget(path)
    original = httpx.AsyncClient.send

    async def send(client, request, *args, **kwargs):
        try:
            payload = json.loads(request.content)
        except (ValueError, TypeError, httpx.RequestNotRead):
            payload = {}
        inference = request.method == "POST" and isinstance(payload, dict) and "model" in payload and ("input" in payload or "messages" in payload)
        if not inference:
            return await original(client, request, *args, **kwargs)
        request_id, _ = budget.reserve(payload, len(request.content))
        response = await original(client, request, *args, **kwargs)
        if kwargs.get("stream"):
            response.stream = MeteredStream(response.stream, budget, request_id)
        else:
            try: budget.settle(request_id, response.json().get("usage"))
            except (ValueError, AttributeError): pass
        return response
    httpx.AsyncClient.send = send
