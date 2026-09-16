"""Optional observability exporters for VARIANT-1's trace envelope.

The local JSONL trace is canonical operational evidence.  Exporters are
best-effort mirrors and are installed only through explicit configuration.
"""

from __future__ import annotations

from collections import OrderedDict
import threading
from typing import Any


_MAX_OPEN_SPANS = 2_048
_SENSITIVE_ATTRIBUTE_NAMES = frozenset({
    "api_key", "authorization", "body", "code", "cookie", "headers",
    "input", "output", "password", "payload", "response", "secret", "source",
    "args",
    "args_preview",
    "content",
    "data",
    "data_b64",
    "goal",
    "image_b64",
    "messages",
    "path",
    "prompt",
    "query",
    "reason",
    "reply",
    "result",
    "text",
    "title",
    "token",
    "url",
})


def _attribute_value(value: Any) -> str | bool | int | float:
    if value is None:
        return ""
    if isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, dict):
        return f"<dict items={len(value)}>"
    if isinstance(value, (list, tuple, set, frozenset)):
        return f"<{type(value).__name__} items={len(value)}>"
    return f"<{type(value).__name__}>"


def _is_sensitive_attribute(name: str) -> bool:
    normalized = str(name or "").strip().lower().replace("-", "_").replace(".", "_")
    return normalized in _SENSITIVE_ATTRIBUTE_NAMES or normalized.endswith(
        ("_content", "_image_b64", "_preview", "_reply", "_text", "_token",
         "_api_key", "_authorization", "_password", "_secret", "_cookie", "_headers")
    )


def flatten_attributes(
    value: dict[str, Any],
    *,
    prefix: str = "variant1",
) -> dict[str, str | bool | int | float]:
    """Flatten one trace envelope into OpenTelemetry-safe attributes."""
    out: dict[str, str | bool | int | float] = {}
    for key, item in dict(value or {}).items():
        if key == "attributes" and isinstance(item, dict):
            for child_key, child in item.items():
                child_name = str(child_key)[:120]
                if _is_sensitive_attribute(child_name):
                    out[f"{prefix}.attributes.{child_name}.redacted"] = True
                    if isinstance(child, str):
                        out[f"{prefix}.attributes.{child_name}.chars"] = len(child)
                    continue
                out[f"{prefix}.attributes.{child_name}"] = _attribute_value(child)
            continue
        name = str(key)[:120]
        if _is_sensitive_attribute(name):
            out[f"{prefix}.{name}.redacted"] = True
            if isinstance(item, str):
                out[f"{prefix}.{name}.chars"] = len(item)
            continue
        out[f"{prefix}.{name}"] = _attribute_value(item)
    return out


class OpenTelemetryTraceExporter:
    """Map paired VARIANT-1 envelope phases onto OpenTelemetry spans.

    This class configures no network exporter itself.  The application owner
    may install any OpenTelemetry SDK processor/exporter. Without one, the API
    remains a harmless local no-op.
    """

    def __init__(self, *, tracer_name: str = "variant1.agent") -> None:
        from opentelemetry import trace

        self._trace = trace
        self._tracer = trace.get_tracer(tracer_name)
        self._lock = threading.RLock()
        self._open: OrderedDict[tuple[str, str], Any] = OrderedDict()

    @staticmethod
    def _time_ns(envelope: dict[str, Any]) -> int:
        try:
            return int(float(envelope.get("timestamp") or 0.0) * 1_000_000_000)
        except Exception:
            return 0

    def _parent_context(self, envelope: dict[str, Any]):
        trace_id = int(str(envelope.get("trace_id") or "0"), 16)
        parent_text = str(envelope.get("parent_span_id") or "")
        if not parent_text:
            # A stable synthetic root groups independently exported event spans
            # under the same trace without storing a long-lived root object.
            parent_text = str(envelope.get("span_id") or "1")
        span_id = int(parent_text, 16)
        if not trace_id or not span_id:
            return None
        span_context = self._trace.SpanContext(
            trace_id=trace_id,
            span_id=span_id,
            is_remote=True,
            trace_flags=self._trace.TraceFlags(self._trace.TraceFlags.SAMPLED),
            trace_state=self._trace.TraceState(),
        )
        return self._trace.set_span_in_context(
            self._trace.NonRecordingSpan(span_context)
        )

    def _start(self, envelope: dict[str, Any]):
        kwargs: dict[str, Any] = {
            "name": str(envelope.get("event") or "variant1.event")[:240],
            "attributes": flatten_attributes(envelope),
        }
        parent = self._parent_context(envelope)
        if parent is not None:
            kwargs["context"] = parent
        start_time = self._time_ns(envelope)
        if start_time:
            kwargs["start_time"] = start_time
        return self._tracer.start_span(**kwargs)

    def __call__(self, envelope: dict[str, Any]) -> None:
        phase = str(envelope.get("phase") or "event")
        key = (
            str(envelope.get("trace_id") or ""),
            str(envelope.get("span_id") or ""),
        )
        if phase == "start":
            span = self._start(envelope)
            with self._lock:
                previous = self._open.pop(key, None)
                if previous is not None:
                    previous.end()
                self._open[key] = span
                while len(self._open) > _MAX_OPEN_SPANS:
                    _, stale = self._open.popitem(last=False)
                    stale.end()
            return
        if phase == "end":
            with self._lock:
                span = self._open.pop(key, None)
            if span is None:
                span = self._start(envelope)
            else:
                for attr_key, attr_value in flatten_attributes(envelope).items():
                    span.set_attribute(attr_key, attr_value)
            status = str(envelope.get("status") or "").lower()
            if status in {"error", "failed", "cancelled", "unavailable"}:
                try:
                    from opentelemetry.trace import Status, StatusCode

                    span.set_status(Status(StatusCode.ERROR, status))
                except Exception:
                    pass
            end_time = self._time_ns(envelope)
            span.end(end_time=end_time or None)
            return
        span = self._start(envelope)
        span.end(end_time=self._time_ns(envelope) or None)

    def close(self) -> None:
        with self._lock:
            pending = list(self._open.values())
            self._open.clear()
        for span in pending:
            try:
                span.end()
            except Exception:
                pass


def configured_exporter(name: str):
    selected = str(name or "").strip().lower()
    if selected in {"", "none", "off", "disabled"}:
        return None
    if selected in {"otel", "opentelemetry"}:
        return OpenTelemetryTraceExporter()
    raise ValueError(f"unknown VARIANT-1 trace exporter: {name}")
