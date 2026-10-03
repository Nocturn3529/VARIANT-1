"""Bounded-memory exports of a frozen context view to an explicit user path."""
from __future__ import annotations

from contextlib import suppress
import hashlib
import json
import os
import tempfile


class ContextExportCancelled(RuntimeError):
    pass


def export_view(service, chat_id, view_id, *, path, format="jsonl", overwrite=False, cancelled=lambda: False):
    if format not in {"jsonl", "markdown"}:
        raise ValueError("Choose JSONL or Markdown export.")
    if not isinstance(path, str) or not path.strip() or "\0" in path or not os.path.isabs(path):
        raise ValueError("Choose an absolute destination path.")
    if not isinstance(overwrite, bool):
        raise ValueError("Overwrite must be an explicit boolean.")
    target = os.path.abspath(path)
    if os.path.exists(target) and not overwrite:
        raise FileExistsError("Destination already exists. Choose another file.")
    status = service.status(chat_id, view_id)
    descriptor = {"schema": "variant1.context-export.v1", "view_id": view_id,
                  "chat_id": chat_id, "status": status, "format": format,
                  "coverage": "Retained text and visible snapshot projections; referenced binary artifacts are not embedded. Omissions are explicit."}
    digest, byte_count, source_count, part_count, omission_count = hashlib.sha256(), 0, 0, 0, 0
    omissions = []
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=os.path.dirname(target), prefix=".variant1-context-", delete=False) as handle:
            temporary = handle.name
            def check():
                if cancelled():
                    raise ContextExportCancelled("Context export was cancelled.")
            def write(text):
                nonlocal byte_count
                check()
                raw = text.encode("utf-8")
                handle.write(raw)
                digest.update(raw)
                byte_count += len(raw)
            def row(value):
                write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
            if format == "jsonl":
                row({"type": "view", **descriptor})
            else:
                write("# External session context\n\n")
                write("    " + json.dumps(descriptor, ensure_ascii=False) + "\n\n")
            for source in service.iter_records(chat_id, view_id, page_size=100):
                check()
                source_count += 1
                source_id = source["source_id"]
                if format == "jsonl":
                    row({"type": "source", "source": source})
                else:
                    write(f"## Source {source_count}\n\n    " + json.dumps(source, ensure_ascii=False) + "\n\n")
                for part in ("source", "result", "output") if source["kind"] == "cell" else ("result",):
                    at_line_start = True
                    if format == "markdown":
                        write("### " + part + "\n\n")
                    try:
                        for chunk in service.iter_expansion(chat_id, view_id, source_id, part=part, chunk_chars=16384):
                            check()
                            if format == "jsonl":
                                row({"type": "content", "source_id": source_id, "part": part, **chunk})
                            else:
                                text = str(chunk.get("text") or "")
                                # Indented code preserves content without needing a fence
                                # longer than a delimiter embedded in historical content.
                                for line in text.splitlines(keepends=True):
                                    write(("    " if at_line_start else "") + line)
                                    at_line_start = line.endswith("\n")
                        part_count += 1
                        if format == "markdown":
                            write("\n\n")
                    except Exception as exc:
                        if getattr(exc, "code", "") != "context_source_unavailable":
                            raise
                        omission_count += 1
                        omission = {"source_id": source_id, "part": part, "reason": "retained source part unavailable"}
                        if len(omissions) < 100:
                            omissions.append(omission)
                        if format == "jsonl":
                            row({"type": "omission", **omission})
                        else:
                            write("\n\nRecorded omission: " + omission["reason"] + ".\n\n")
            summary = {"source_count": source_count, "part_count": part_count, "omission_count": omission_count}
            if format == "jsonl":
                row({"type": "complete", **summary})
            else:
                write("Export summary: " + json.dumps(summary) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            check()
        # Recheck revocation after every source has been read and verified.
        service._view(chat_id, view_id)
        if cancelled():
            raise ContextExportCancelled("Context export was cancelled.")
        if overwrite:
            os.replace(temporary, target)
        else:
            os.link(temporary, target)  # Exclusive publication cannot replace a concurrent file.
            os.unlink(temporary)
        temporary = ""
        return {"path": target, "format": format, "view_id": view_id, "bytes": byte_count,
                "sha256": digest.hexdigest(), **summary, "omissions": omissions,
                "omissions_truncated": omission_count > len(omissions)}
    finally:
        if temporary:
            with suppress(OSError):
                os.unlink(temporary)
