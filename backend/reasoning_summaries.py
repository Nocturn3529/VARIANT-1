"""Provider-exposed summaries stay distinct from private reasoning/replay data."""
from __future__ import annotations
import inspect
import time
import uuid

SUMMARY_TEXT_LIMIT = 16_000
SUMMARY_STEP_LIMIT = 200


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
