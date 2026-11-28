"""Displayable provider thoughts and summaries, separate from replay data."""
from __future__ import annotations
import asyncio
import inspect
import time
import uuid
from dataclasses import dataclass, field

SUMMARY_TEXT_LIMIT = 16_000
SUMMARY_STEP_LIMIT = 200
DISPLAY_THOUGHT_SOURCES = ("provider_summary", "provider_reasoning")
_READABLE_RESPONSES_DELTAS = frozenset({
    "response.reasoning.delta",
    "response.reasoning_text.delta",
})


def thoughts_enabled(router, profile, model, payload, *, provider: str) -> bool:
    """Use the effective request and the matching route's display policy."""
    from model_runtime.request_policy import effective_reasoning_rule

    reasoning = payload.get("reasoning")
    options = reasoning if isinstance(reasoning, dict) else {}
    effort = str(options.get("effort") or payload.get("reasoning_effort") or "").lower()
    thinking = payload.get("thinking")
    thinking_type = thinking.get("type") if isinstance(thinking, dict) else None
    template = payload.get("chat_template_kwargs")
    template = template if isinstance(template, dict) else {}
    generation = payload.get("generationConfig")
    generation = generation if isinstance(generation, dict) else {}
    gemini = generation.get("thinkingConfig") or generation.get("thinking_config")
    gemini = gemini if isinstance(gemini, dict) else {}
    if (
        reasoning is False or options.get("enabled") is False or options.get("exclude") is True
        or effort in {"off", "none"} or thinking_type == "disabled"
        or payload.get("reasoning_budget") == 0 or template.get("enable_thinking") is False
        or gemini.get("includeThoughts") is False or gemini.get("include_thoughts") is False
        or gemini.get("thinkingBudget") == 0 or gemini.get("thinking_budget") == 0
    ):
        return False
    getter = getattr(router, "bound_model_route", None)
    route = getter() if callable(getter) else None
    if isinstance(route, dict) and route.get("provider") == provider and route.get("model") == model:
        if route.get("reasoning_enabled") is False or route.get("reasoning_effort") in ("off", "none"):
            return False
        if route.get("reasoning_enabled") is True:
            return True
    if (options.get("enabled") is True or effort or thinking_type in ("enabled", "adaptive")
            or gemini.get("includeThoughts") is True or gemini.get("include_thoughts") is True):
        return True
    return effective_reasoning_rule(router, profile, model).get("default_enabled") is not False


def readable_responses_reasoning_delta(event: dict) -> str:
    """Read the plaintext field of an explicit reasoning event."""
    kind = event.get("type")
    delta = event.get("delta")
    return delta if isinstance(kind, str) and kind in _READABLE_RESPONSES_DELTAS and isinstance(delta, str) else ""


def extract_readable_thought_parts(delta: dict, plaintext):
    """Read only plaintext aliases and typed text or summary fields."""
    if isinstance(plaintext, str) and plaintext:
        # A provider can send the same token in both carriers.
        yield plaintext, "provider_reasoning"
        return
    details = delta.get("reasoning_details")
    for part in details if isinstance(details, list) else ():
        if not isinstance(part, dict):
            continue
        if part.get("type") == "reasoning.text" and isinstance(part.get("text"), str):
            yield part["text"], "provider_reasoning"
        elif part.get("type") == "reasoning.summary" and isinstance(part.get("summary"), str):
            yield part["summary"], "provider_summary"


@dataclass
class _ReturnedText:
    identity: str = field(default_factory=lambda: "summary_" + uuid.uuid4().hex)
    prefix: str = ""
    next_update: float = 0.0
    delivery: asyncio.Task | None = None


def summary_text(text):
    value = str(text or "").strip()
    return value if len(value) <= SUMMARY_TEXT_LIMIT else value[:SUMMARY_TEXT_LIMIT - 21] + "\n[Summary truncated]"


async def emit_summary_event(sink, event):
    """Only explicitly typed public channels receive live summary snapshots."""
    callback = getattr(sink, "summary_event", None)
    if not callable(callback):
        return
    try:
        result = callback(dict(event))
        if inspect.isawaitable(result):
            await result
    except Exception:
        pass  # UI delivery cannot turn provider success into an inference error.


def emit_summary(sink, text):
    if sink is None or not text:
        return
    callback = getattr(sink, "summary", None)
    try:
        (callback if callable(callback) else sink)(text)
    except Exception:
        pass  # Display callbacks cannot change the provider call's outcome.


class ReasoningBuffer:
    """Preserve the channel while buffering a retryable provider attempt."""
    def __init__(self, summary_sink=None):
        self.parts = []
        self.summary_sink = summary_sink
        self.events = {}
        self._returned: dict[str, _ReturnedText] = {}

    async def returned_thought(self, delta, source="provider_reasoning"):
        """Publish the plaintext display channel of one eligible provider attempt."""
        if not isinstance(delta, str) or not delta or not callable(getattr(self.summary_sink, "summary_event", None)):
            return
        if source not in DISPLAY_THOUGHT_SOURCES:
            return
        stream = self._returned.get(source)
        if stream is None:
            stream = self._returned[source] = _ReturnedText()
        remaining = SUMMARY_TEXT_LIMIT + 1 - len(stream.prefix)
        if remaining <= 0:
            return
        stream.prefix += delta[:remaining]
        text = stream.prefix.strip()
        if len(stream.prefix) > SUMMARY_TEXT_LIMIT:
            marker = "\n[Summary truncated]" if source == "provider_summary" else "\n[Thought truncated]"
            text = stream.prefix[:SUMMARY_TEXT_LIMIT - len(marker)].rstrip() + marker
        if not text:
            return
        prior = self.events.get(stream.identity)
        if prior and (prior["text"] == text or prior["status"] != "running"):
            return
        event = {"summary_id": stream.identity, "source": source,
                 "summary_revision": prior["summary_revision"] + 1 if prior else 1,
                 "text": text, "status": "running", "ts": prior["ts"] if prior else time.time() * 1000}
        now = time.monotonic()
        if now >= stream.next_update and stream.delivery is None:
            stream.next_update = now + 0.075
            await self.summary_event(event)
        else:
            self.events[stream.identity] = event
            if stream.delivery is None:
                stream.delivery = asyncio.create_task(self._deliver_latest(stream))

    async def _deliver_latest(self, stream):
        # Deliver the tail of a burst even if the provider pauses before its
        # next token. Only one pending delivery exists for each display channel.
        try:
            while True:
                await asyncio.sleep(max(0, stream.next_update - time.monotonic()))
                event = self.events[stream.identity]
                if event["status"] != "running":
                    return
                stream.next_update = time.monotonic() + 0.075
                await emit_summary_event(self.summary_sink, event)
                if self.events[stream.identity] is event:
                    return
        finally:
            stream.delivery = None

    async def summary_event(self, event):
        identity = str(event.get("summary_id") or "")
        revision = event.get("summary_revision")
        if (not identity or type(revision) is not int
                or event.get("status") not in {"running", "done", "discarded", "cancelled"}):
            return
        prior = self.events.get(identity)
        if prior and revision <= prior["summary_revision"]:
            return
        if prior and prior["status"] != "running" and event["status"] == "running":
            return
        if prior and prior["status"] in {"discarded", "cancelled"} and event["status"] != prior["status"]:
            return
        self.events[identity] = dict(event)
        await emit_summary_event(self.summary_sink, event)

    async def finish(self, status):
        pending = [stream.delivery for stream in self._returned.values() if stream.delivery is not None]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for event in list(self.events.values()):
            if event["status"] != "running" and not (status in {"discarded", "cancelled"} and event["status"] == "done"):
                continue
            await self.summary_event({**event, "status": status,
                "summary_revision": event["summary_revision"] + 1})

    def __call__(self, text):
        if isinstance(text, str) and text:
            self.parts.append(("private", text))

    def summary(self, text):
        if isinstance(text, str) and text:
            self.parts.append(("summary", text))

    def text(self):
        return "".join(text for _, text in self.parts)

    def public_text(self):
        return "\n\n".join(text for kind, text in self.parts if kind == "summary")

    def replay(self, sink):
        for kind, text in self.parts:
            if kind == "summary":
                emit_summary(sink, text)
            else:
                try:
                    sink(text)
                except Exception:
                    pass


class ResponsesSummary:
    """Normalize summary deltas and terminal snapshots without duplicating text.

    Never reads reasoning_text, content, signatures or encrypted_content.
    observe returns newly retained byte growth for the existing response budget.
    """
    def __init__(self):
        self.parts = {}
        self.identities = {}
        self.sequences = {}
        self.summary_id = "summary_" + uuid.uuid4().hex
        self.revision = 0
        self.ts = None
        self.last_public_text = ""
        self._dirty = False

    async def progress(self, sink):
        if not self._dirty:
            return
        self._dirty = False
        text = summary_text("\n\n".join(value for value in self.parts.values() if value))
        if text == self.last_public_text:
            return
        self.last_public_text = text
        self.revision += 1
        if self.ts is None:
            self.ts = time.time() * 1000
        await emit_summary_event(sink, {"summary_id": self.summary_id,
            "summary_revision": self.revision, "text": text, "status": "running", "ts": self.ts})

    def _key(self, event, item=None):
        identity = (item or {}).get("id") or event.get("item_id")
        identity = identity if isinstance(identity, str) else ""
        if identity and identity in self.identities:
            return self.identities[identity]
        index = event.get("output_index")
        key = ("output", index) if type(index) is int and index >= 0 else ("item", identity) if identity else ("output", 0)
        if identity:
            self.identities[identity] = key
        return key

    def _set(self, key, index, text, *, delta=False):
        if not isinstance(text, str):
            return 0
        key = (key, index if type(index) is int and index >= 0 else 0)
        old = self.parts.get(key, "")
        new = old + text if delta else text
        self.parts[key] = new
        self._dirty = self._dirty or new != old
        return max(0, len(new.encode("utf-8")) - len(old.encode("utf-8")))

    def observe(self, event):
        kind = event.get("type", "")
        if kind in {"response.reasoning_summary_text.delta", "response.reasoning_summary_text.done"}:
            key = self._key(event)
            index = event.get("summary_index", 0)
            if type(index) is not int or index < 0:
                index = 0
            if kind.endswith(".delta"):
                if not isinstance(event.get("delta"), str) or not event["delta"]:
                    return 0
                sequence = event.get("sequence_number")
                if type(sequence) is int:
                    marker = (key, index)
                    if sequence <= self.sequences.get(marker, -1):
                        return 0
                    self.sequences[marker] = sequence
                return self._set(key, index, event.get("delta"), delta=True)
            return self._set(key, index, event.get("text"))
        if kind in {"response.reasoning_summary_part.added", "response.reasoning_summary_part.done"}:
            part = event.get("part") or {}
            if isinstance(part, dict) and part.get("type") == "summary_text" and part.get("text"):
                return self._set(self._key(event), event.get("summary_index", 0), part["text"])
        items = []
        if kind in {"response.output_item.added", "response.output_item.done"}:
            items.append((event, event.get("item") or {}))
        elif kind in {"response.completed", "response.incomplete"}:
            response = event.get("response") or {}
            if isinstance(response, dict):
                items.extend(({"output_index": index}, item) for index, item in enumerate(response.get("output") or []))
        growth = 0
        for metadata, item in items:
            if not isinstance(item, dict) or item.get("type") != "reasoning":
                continue
            key = self._key(metadata, item)
            for index, part in enumerate(item.get("summary") or []):
                if isinstance(part, dict) and part.get("type") == "summary_text":
                    growth += self._set(key, index, part.get("text"))
        return growth

    def publish(self, sink):
        text = "\n\n".join(value for value in self.parts.values() if value)
        emit_summary(sink, text)
