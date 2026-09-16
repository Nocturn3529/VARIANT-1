"""Bounded, ordered evidence for one admitted persistent-Python cell."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import json
import re
from typing import Any, Callable

from .worker_context import NAMESPACE_IDENTIFIER_CHARS, NAMESPACE_INVENTORY_LIMIT


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

OUTPUT_EVENT_SCHEMA = "variant1.kernel-output-events.v1"

_BINARY_MIME_TYPES = frozenset({
    "application/pdf",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
})
_DISPLAY_PREFERENCE = (
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "image/svg+xml",
    "text/markdown",
    "application/json",
    "text/plain",
    "text/html",
)
_MAX_MIME_ALTERNATIVES = 32


@dataclass(frozen=True)
class OutputLimits:
    max_message_bytes: int = 256 * 1024
    max_cell_bytes: int = 512 * 1024
    max_events: int = 256
    max_mime_bytes: int = 256 * 1024
    max_artifact_bytes: int = 8 * 1024 * 1024
    max_cell_artifact_bytes: int = 16 * 1024 * 1024


@dataclass
class CellOutput:
    # ``chunks`` is the small live/model projection. ``events`` is the durable
    # typed evidence used for notebook reconstruction.
    chunks: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    admitted_bytes: int = 0
    dropped_bytes: int = 0
    admitted_events: int = 0
    dropped_events: int = 0
    stale_events: int = 0
    artifact_errors: list[str] = field(default_factory=list)
    evidence_inline_bytes: int = 0
    evidence_artifact_bytes: int = 0
    evidence_dropped_bytes: int = 0
    evidence_dropped_events: int = 0
    evidence_dropped_bodies: int = 0
    error_name: str = ""
    error_value: str = ""
    traceback: list[str] = field(default_factory=list)
    namespace_delta: dict[str, Any] = field(default_factory=dict)
    # Private host cache; deliberately absent from evidence()/public output.
    namespace_inventory: list[str] | None = field(default=None, repr=False)
    namespace_inventory_omitted: int = field(default=0, repr=False)
    terminate_requested: bool = False
    terminal_observation: str = ""

    @property
    def truncated(self) -> bool:
        """Whether the concise model projection omitted any output."""

        return bool(self.dropped_bytes or self.dropped_events)

    def _visible_chunks(self) -> list[dict[str, Any]]:
        """Apply display updates and clear-output semantics to live chunks."""

        visible: list[dict[str, Any]] = []
        clear_on_next = False
        for chunk in self.chunks:
            kind = str(chunk.get("kind") or "")
            if kind == "clear_output":
                if bool(chunk.get("wait")):
                    clear_on_next = True
                else:
                    visible.clear()
                    clear_on_next = False
                continue
            if clear_on_next:
                visible.clear()
                clear_on_next = False
            display_id = str(chunk.get("display_id") or "")
            if bool(chunk.get("update")) and display_id:
                replaced = False
                for index, previous in enumerate(visible):
                    if str(previous.get("display_id") or "") == display_id:
                        visible[index] = chunk
                        replaced = True
                if replaced:
                    continue
            visible.append(chunk)
        return visible

    def visible_artifacts(self) -> list[dict[str, Any]]:
        refs = {
            str(chunk.get("artifact_ref") or "")
            for chunk in self._visible_chunks()
            if str(chunk.get("artifact_ref") or "")
        }
        return [
            dict(item)
            for item in self.artifacts
            if str(item.get("ref") or "") in refs
        ]

    def text(self) -> str:
        parts: list[str] = []
        visible = self._visible_chunks()
        for chunk in visible:
            text = str(chunk.get("text") or "")
            if text:
                parts.append(text)
        if self.error_name or self.error_value:
            heading = ": ".join(
                item for item in (self.error_name, self.error_value) if item
            )
            if heading and not any(heading in part for part in parts):
                parts.append(heading)
        if self.truncated:
            parts.append(
                "[output projection truncated: "
                f"{self.dropped_bytes} bytes / {self.dropped_events} events omitted]"
            )
        visible_refs = {
            str(chunk.get("artifact_ref") or "")
            for chunk in visible
            if str(chunk.get("artifact_ref") or "")
        }
        if visible_refs:
            parts.append("Artifacts: " + ", ".join(sorted(visible_refs)))
        terminal = str(self.terminal_observation or "").strip()
        if terminal and not any(terminal == part.strip() for part in parts):
            parts.append(("\n" if parts else "") + terminal)
        return "".join(parts).strip()

    def namespace_footer(self) -> str:
        """Report exceptional namespace loss without narrating normal state.

        Python variables are already visible in the authored cell and remain in
        the live kernel. Repeating every ordinary updated/retained name in the
        transcript adds no evidence and compounds prompt cost on later calls.
        """

        updated = [
            str(name) for name in self.namespace_delta.get("updated", ())
            if str(name)
        ]
        retained = [
            str(name) for name in self.namespace_delta.get("retained", ())
            if str(name)
        ]
        updated_omitted = max(
            0, int(self.namespace_delta.get("updated_omitted") or 0)
        )
        retained_omitted = max(
            0, int(self.namespace_delta.get("retained_omitted") or 0)
        )
        if not self.truncated and not (updated_omitted or retained_omitted):
            return ""
        footer = []
        if updated:
            footer.append("Updated: " + ", ".join(updated))
        if retained:
            footer.append("Retained: " + ", ".join(retained))
        if updated_omitted:
            footer.append(f"{updated_omitted} updated name(s) omitted")
        if retained_omitted:
            footer.append(f"{retained_omitted} retained name(s) omitted")
        if not footer:
            return ""
        return "[Session state] " + "; ".join(footer)

    def evidence(self) -> dict[str, Any]:
        return {
            "schema": OUTPUT_EVENT_SCHEMA,
            "events": [dict(event) for event in self.events],
            "bounds": {
                "inline_bytes": int(self.evidence_inline_bytes),
                "artifact_bytes": int(self.evidence_artifact_bytes),
                "dropped_bytes": int(self.evidence_dropped_bytes),
                "admitted_events": len(self.events),
                "dropped_events": int(self.evidence_dropped_events),
                "dropped_bodies": int(self.evidence_dropped_bodies),
                "stale_events": int(self.stale_events),
            },
            "namespace_delta": dict(self.namespace_delta),
        }


class CellOutputCollector:
    """Capture provider-neutral events and a bounded model projection."""

    def __init__(
        self,
        *,
        limits: OutputLimits,
        artifact_store: Any = None,
        artifact_scope: str = "",
        on_chunk: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.limits = limits
        self.artifact_store = artifact_store
        self.artifact_scope = str(artifact_scope or "")
        self.on_chunk = on_chunk
        self.result = CellOutput()
        self._sequence = 0

    @staticmethod
    def _bytes(value: Any) -> bytes:
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            return value.encode("utf-8", errors="replace")
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8", errors="replace")

    @staticmethod
    def _json_value(raw: bytes) -> Any:
        try:
            return json.loads(raw.decode("utf-8", errors="strict"))
        except Exception:
            return raw.decode("utf-8", errors="replace")

    def stale(self) -> None:
        self.result.stale_events += 1

    def accept_namespace_delta(self, content: Any) -> None:
        """Record a metadata-only, bounded projection of persistent names."""

        if not isinstance(content, dict) or content.get("schema") != (
            "variant1.kernel-namespace-delta.v1"
        ):
            return

        def names(field: str) -> list[str]:
            values = content.get(field)
            if not isinstance(values, list):
                return []
            out = []
            for value in values[:12]:
                name = str(value or "")[:128]
                if name and name not in out:
                    out.append(name)
            return out

        delta = {
            "schema": "variant1.kernel-namespace-delta.v1",
            "updated": names("updated"),
            "retained": names("retained"),
            "updated_omitted": max(0, int(content.get("updated_omitted") or 0)),
            "retained_omitted": max(0, int(content.get("retained_omitted") or 0)),
        }
        self.result.namespace_delta = delta
        inventory = content.get("inventory")
        if isinstance(inventory, list):
            self.result.namespace_inventory = list(dict.fromkeys(
                name for name in inventory[:NAMESPACE_INVENTORY_LIMIT]
                if isinstance(name, str) and name.isidentifier()
                and not name.startswith("_") and len(name) <= NAMESPACE_IDENTIFIER_CHARS
            ))
            self.result.namespace_inventory_omitted = max(
                0, int(content.get("inventory_omitted") or 0)
            )

    def accept_execution_control(self, content: Any) -> None:
        if not isinstance(content, dict) or content.get("schema") != (
            "variant1.kernel-execution-control.v1"
        ):
            return
        self.result.terminate_requested = bool(content.get("terminate"))
        self.result.terminal_observation = str(
            content.get("observation") or ""
        )[:4_000]

    def _can_record_event(self, content: Any) -> bool:
        if len(self.result.events) < max(0, int(self.limits.max_events)):
            return True
        size = len(self._bytes(content))
        self.result.evidence_dropped_bytes += size
        self.result.evidence_dropped_events += 1
        self.result.dropped_bytes += size
        self.result.dropped_events += 1
        return False

    def _record_event(self, event: dict[str, Any]) -> int:
        self._sequence += 1
        recorded = {"sequence": self._sequence, **event}
        self.result.events.append(recorded)
        return self._sequence

    def _admit(
        self,
        kind: str,
        text: str,
        *,
        media_type: str = "text/plain",
        sequence: int = 0,
        display_id: str = "",
        update: bool = False,
        artifact_ref: str = "",
    ) -> None:
        raw = self._bytes(text)
        message_limit = max(1, int(self.limits.max_message_bytes))
        cell_remaining = max(
            0, int(self.limits.max_cell_bytes) - self.result.admitted_bytes
        )
        event_remaining = max(
            0, int(self.limits.max_events) - self.result.admitted_events
        )
        admitted = min(len(raw), message_limit, cell_remaining) if event_remaining else 0
        dropped = len(raw) - admitted
        if admitted:
            visible = raw[:admitted].decode("utf-8", errors="replace")
            chunk = {
                "kind": kind,
                "media_type": media_type,
                "text": visible,
                "bytes": admitted,
                "sequence": int(sequence),
            }
            if display_id:
                chunk["display_id"] = display_id
            if update:
                chunk["update"] = True
            if artifact_ref:
                chunk["artifact_ref"] = artifact_ref
            self.result.chunks.append(chunk)
            self.result.admitted_bytes += admitted
            self.result.admitted_events += 1
            if self.on_chunk is not None:
                try:
                    self.on_chunk(dict(chunk))
                except Exception:
                    pass
        if dropped or not event_remaining:
            self.result.dropped_bytes += max(
                dropped, len(raw) if not event_remaining else 0
            )
            self.result.dropped_events += 1

    def _control_chunk(self, *, sequence: int, wait: bool) -> None:
        chunk = {
            "kind": "clear_output",
            "sequence": int(sequence),
            "wait": bool(wait),
            "bytes": 0,
        }
        self.result.chunks.append(chunk)

    def _remember_artifact(self, artifact: dict[str, Any]) -> None:
        ref = str(artifact.get("ref") or "")
        if ref and not any(str(item.get("ref") or "") == ref for item in self.result.artifacts):
            self.result.artifacts.append(dict(artifact))

    def _artifact_body(
        self,
        raw: bytes,
        *,
        media_type: str,
        kind: str,
        encoding: str,
    ) -> dict[str, Any]:
        per_body = max(1, int(self.limits.max_artifact_bytes))
        cell_remaining = max(
            0,
            int(self.limits.max_cell_artifact_bytes)
            - self.result.evidence_artifact_bytes,
        )
        if len(raw) > per_body or len(raw) > cell_remaining:
            self.result.evidence_dropped_bytes += len(raw)
            self.result.evidence_dropped_bodies += 1
            return {
                "storage": "omitted",
                "encoding": encoding,
                "bytes": len(raw),
                "reason": "artifact_limit",
            }
        if self.artifact_store is None:
            self.result.evidence_dropped_bytes += len(raw)
            self.result.evidence_dropped_bodies += 1
            return {
                "storage": "omitted",
                "encoding": encoding,
                "bytes": len(raw),
                "reason": "artifact_store_unavailable",
            }
        try:
            ref = self.artifact_store.put_bytes(
                raw,
                media_type=media_type,
                kind=kind,
                scope=self.artifact_scope,
            )
            artifact = ref.to_dict()
            self._remember_artifact(artifact)
            self.result.evidence_artifact_bytes += len(raw)
            return {
                "storage": "artifact",
                "encoding": encoding,
                "bytes": len(raw),
                "artifact": artifact,
            }
        except Exception as exc:
            self.result.artifact_errors.append(type(exc).__name__)
            self.result.evidence_dropped_bytes += len(raw)
            self.result.evidence_dropped_bodies += 1
            # Keep legacy projection-loss counters useful to existing callers.
            self.result.dropped_bytes += len(raw)
            self.result.dropped_events += 1
            return {
                "storage": "omitted",
                "encoding": encoding,
                "bytes": len(raw),
                "reason": "artifact_write_failed",
            }

    def _body(
        self,
        value: Any,
        *,
        media_type: str,
        kind: str,
        binary_base64: bool = False,
    ) -> dict[str, Any]:
        encoding = "utf-8" if isinstance(value, str) else "json"
        if binary_base64:
            encoding = "binary"
            try:
                raw = base64.b64decode(str(value), validate=True)
            except Exception:
                raw = b""
            if not raw and str(value or ""):
                self.result.evidence_dropped_bodies += 1
                return {
                    "storage": "omitted",
                    "encoding": encoding,
                    "bytes": 0,
                    "reason": "invalid_base64",
                }
            return self._artifact_body(
                raw,
                media_type=media_type,
                kind=kind,
                encoding=encoding,
            )

        raw = self._bytes(value)
        per_body_limit = max(1, int(self.limits.max_mime_bytes))
        cell_remaining = max(
            0, int(self.limits.max_cell_bytes) - self.result.evidence_inline_bytes
        )
        if len(raw) <= per_body_limit and len(raw) <= cell_remaining:
            self.result.evidence_inline_bytes += len(raw)
            return {
                "storage": "inline",
                "encoding": encoding,
                "bytes": len(raw),
                "data": value if encoding == "utf-8" else self._json_value(raw),
            }
        return self._artifact_body(
            raw,
            media_type=media_type,
            kind=kind,
            encoding=encoding,
        )

    def _mime_bundle(self, data: dict[str, Any]) -> dict[str, dict[str, Any]]:
        bundle: dict[str, dict[str, Any]] = {}
        if not isinstance(data, dict):
            return bundle
        for index, (raw_media_type, value) in enumerate(data.items()):
            if index >= _MAX_MIME_ALTERNATIVES:
                self.result.evidence_dropped_bodies += 1
                self.result.evidence_dropped_bytes += len(self._bytes(value))
                continue
            media_type = str(raw_media_type or "").strip()[:256]
            if not media_type:
                continue
            bundle[media_type] = self._body(
                value,
                media_type=media_type,
                kind="kernel_display_body",
                binary_base64=media_type.lower() in _BINARY_MIME_TYPES,
            )
        return bundle

    @staticmethod
    def _descriptor_text(descriptor: dict[str, Any]) -> str:
        if str(descriptor.get("storage") or "") != "inline":
            return ""
        value = descriptor.get("data")
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def _descriptor_ref(descriptor: dict[str, Any]) -> str:
        artifact = descriptor.get("artifact")
        if not isinstance(artifact, dict):
            return ""
        return str(artifact.get("ref") or "")

    def _project_display(
        self,
        bundle: dict[str, dict[str, Any]],
        *,
        sequence: int,
        display_id: str,
        update: bool,
    ) -> None:
        media_types = list(bundle)
        selected = next(
            (item for item in _DISPLAY_PREFERENCE if item in bundle),
            media_types[0] if media_types else "",
        )
        if not selected:
            return
        descriptor = bundle[selected]
        ref = self._descriptor_ref(descriptor)
        if ref:
            self._admit(
                "artifact_ref",
                f"[{selected} retained as {ref}]\n",
                sequence=sequence,
                display_id=display_id,
                update=update,
                artifact_ref=ref,
                media_type=selected,
            )
            return
        text = self._descriptor_text(descriptor)
        if text:
            suffix = "" if selected == "text/plain" else "\n"
            self._admit(
                "display",
                text + suffix,
                media_type=selected,
                sequence=sequence,
                display_id=display_id,
                update=update,
            )
            return
        reason = str(descriptor.get("reason") or "output unavailable")
        self._admit(
            "display_error",
            f"[{selected} {reason}]\n",
            sequence=sequence,
            display_id=display_id,
            update=update,
        )


    def accept_event(self, event: dict[str, Any]) -> None:
        """Ingest one normalized ``variant1.repl-protocol.v1`` output event."""

        if not isinstance(event, dict):
            return
        event_type = str(event.get("type") or "")
        if event_type == "namespace_delta":
            self.accept_namespace_delta(event.get("content"))
            return
        if event_type == "execution_control":
            self.accept_execution_control(event.get("content"))
            return
        admitted_types = {
            "stdout",
            "stderr",
            "result",
            "display",
            "update_display",
            "clear_output",
            "error",
        }
        if event_type not in admitted_types or not self._can_record_event(event):
            return

        if event_type in {"stdout", "stderr"}:
            stream_text = str(event.get("text") or "")
            body = self._body(
                stream_text,
                media_type="text/plain; charset=utf-8",
                kind="kernel_stream_body",
            )
            sequence = self._record_event({
                "type": "stream",
                "name": event_type,
                "body": body,
            })
            self._admit(
                event_type,
                stream_text,
                sequence=sequence,
                artifact_ref=self._descriptor_ref(body),
            )
            return

        if event_type in {"result", "display", "update_display"}:
            durable_type = {
                "result": "execute_result",
                "display": "display_data",
                "update_display": "update_display_data",
            }[event_type]
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            bundle = self._mime_bundle(data)
            metadata = (
                event.get("metadata")
                if isinstance(event.get("metadata"), dict)
                else {}
            )
            display_id = str(event.get("display_id") or "")[:512]
            transient = {"display_id": display_id} if display_id else {}
            recorded: dict[str, Any] = {
                "type": durable_type,
                "data": bundle,
                "metadata": self._body(
                    metadata,
                    media_type="application/json",
                    kind="kernel_display_metadata",
                ),
                "transient": self._body(
                    transient,
                    media_type="application/json",
                    kind="kernel_display_transient",
                ),
            }
            if display_id:
                recorded["display_id"] = display_id
            if durable_type == "execute_result":
                recorded["execution_count"] = int(
                    event.get("execution_count") or 0
                )
            sequence = self._record_event(recorded)
            self._project_display(
                bundle,
                sequence=sequence,
                display_id=display_id,
                update=durable_type == "update_display_data",
            )
            return

        if event_type == "clear_output":
            wait = bool(event.get("wait"))
            sequence = self._record_event({"type": "clear_output", "wait": wait})
            self._control_chunk(sequence=sequence, wait=wait)
            return

        self.result.error_name = str(
            event.get("name") or "ExecutionError"
        )
        self.result.error_value = str(event.get("message") or "")
        trace = [
            _ANSI.sub("", str(line))
            for line in (
                event.get("traceback")
                if isinstance(event.get("traceback"), list)
                else ()
            )
        ]
        self.result.traceback = trace[-24:]
        name_body = self._body(
            self.result.error_name,
            media_type="text/plain; charset=utf-8",
            kind="kernel_error_body",
        )
        value_body = self._body(
            self.result.error_value,
            media_type="text/plain; charset=utf-8",
            kind="kernel_error_body",
        )
        traceback_body = self._body(
            self.result.traceback,
            media_type="application/json",
            kind="kernel_error_traceback",
        )
        sequence = self._record_event({
            "type": "error",
            "ename": name_body,
            "evalue": value_body,
            "traceback": traceback_body,
        })
        rendered = "\n".join(self.result.traceback) or (
            f"{self.result.error_name}: {self.result.error_value}"
        )
        self._admit(
            "error",
            rendered + "\n",
            sequence=sequence,
            artifact_ref=self._descriptor_ref(traceback_body),
        )
