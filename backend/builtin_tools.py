"""
VARIANT-1 built-in tools — Phase 1, Step 8c (Layer 5, spec 5.x)

Local file and shell-adjacent capabilities that benefit from running
in-process instead of through an MCP server. Registered handlers are ordinary
module functions; the skill library is exposed only through its mounted
``skills`` object.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import io
import json
import os
import shutil
import tempfile
import threading
import uuid
from pathlib import PurePosixPath
from typing import Any

import tools
from core_invariants import cancellation_is_requested
from tool_core import ProgrammaticArtifactPayload, ToolProjectionResult
from file_paths import effective_path as _canonical_effective_path
from file_paths import is_reparse_or_link as _is_reparse_or_link
from file_paths import normalize_path as _canonical_normalize_path

# Pi-style reads paginate by human/model-meaningful line numbers and always
# return a deterministic continuation cursor.  VARIANT-1's tool runner projects at
# most 10K characters to the model, so keep the read payload below that outer
# ceiling; otherwise the runner could clip off the continuation instruction.
DEFAULT_READ_LINES = 2_000
MAX_READ_LINES = 2_000
MAX_READ_BYTES = 8_000
MAX_COMPLETE_PROGRAMMATIC_BYTES = 1_000_000
MAX_COMPLETE_ARTIFACT_BYTES = 16_000_000
MAX_LIST = 500
MAX_FIND = 200
MAX_SEARCH_FILE_BYTES = 2_000_000


_PATCH_TRANSACTION_LOCK = threading.RLock()


def _resolve(p) -> str:
    return _canonical_normalize_path(p)


def _effective_path(path) -> str:
    return _canonical_effective_path(path)


def _check(path) -> str:
    """Normalize a path for file operations."""
    return _resolve(_effective_path(path))


async def _thread(fn):
    return await asyncio.get_event_loop().run_in_executor(None, fn)


# -- files -------------------------------------------------------------------
async def read_file(args):
    path = (args.get("path") or "").strip()
    if not path:
        raise tools.ToolError("read_file needs a 'path'")
    try:
        explicit_window = args.get("offset") is not None or args.get("limit") is not None
        offset = int(args.get("offset") or 1)
        limit = int(args.get("limit") or DEFAULT_READ_LINES)
    except (TypeError, ValueError):
        raise tools.ToolError("read_file offset and limit must be integers")
    if offset < 1:
        raise tools.ToolError("read_file offset must be a 1-indexed line number")
    limit = max(1, min(limit, MAX_READ_LINES))
    rp = _check(path)

    def _r():
        if not os.path.isfile(rp):
            raise tools.ToolError(
                f"not a file at this exact path: {path}. "
                "read_file does not search subdirectories; use glob to find a filename."
            )
        selected: list[str] = []
        selected_bytes = 0
        total_lines = 0
        byte_limited = False
        oversized_first_line = False
        source_hasher = hashlib.sha256()
        decoded_hasher = hashlib.sha256()
        full_text: str | None = None

        def identity(stat):
            return (
                int(getattr(stat, "st_dev", 0)), int(getattr(stat, "st_ino", 0)),
                int(stat.st_size), int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9))),
            )

        with open(rp, "rb") as binary_handle:
            before = identity(os.fstat(binary_handle.fileno()))
            raw_size = before[2]
            if raw_size <= MAX_COMPLETE_ARTIFACT_BYTES:
                raw = binary_handle.read()
                source_hasher.update(raw)
                full_text = raw.decode("utf-8", errors="replace")
                line_source = io.StringIO(full_text, newline="")
            else:
                if not explicit_window:
                    raise tools.ToolError(
                        f"complete read exceeds the {MAX_COMPLETE_ARTIFACT_BYTES}-byte artifact limit; use explicit offset/limit windows"
                    )
                for chunk in iter(lambda: binary_handle.read(1024 * 1024), b""):
                    source_hasher.update(chunk)
                binary_handle.seek(0)
                line_source = io.TextIOWrapper(
                    binary_handle, encoding="utf-8", errors="replace", newline=""
                )
            for line_number, line in enumerate(line_source, start=1):
                total_lines = line_number
                encoded = line.encode("utf-8", errors="replace")
                decoded_hasher.update(encoded)
                if line_number < offset or len(selected) >= limit or byte_limited:
                    continue
                line_bytes = len(encoded)
                if selected_bytes + line_bytes > MAX_READ_BYTES:
                    if not selected:
                        # Exception for pathological minified/generated files:
                        # a single physical line cannot fit under the outer tool
                        # result ceiling. Return a clearly marked prefix so the
                        # model can identify it, then advance to avoid a permanent
                        # same-line loop.
                        prefix = line.encode("utf-8", errors="replace")[:MAX_READ_BYTES]
                        line = prefix.decode("utf-8", errors="ignore")
                        selected.append(line)
                        selected_bytes = len(line.encode("utf-8"))
                        oversized_first_line = True
                    byte_limited = True
                    continue
                selected.append(line)
                selected_bytes += line_bytes
            if isinstance(line_source, io.TextIOWrapper):
                line_source.detach()
            after = identity(os.fstat(binary_handle.fileno()))
            if before != after:
                raise tools.ToolError(
                    "file changed during read; retry from a fresh coherent snapshot",
                    code="file_changed_during_read",
                )

        if total_lines == 0:
            if offset != 1:
                raise tools.ToolError(
                    f"read_file offset {offset} is past the end of the empty file"
                )
            empty_value: Any = (
                {
                    "schema": "variant1.file-read-result.v2",
                    "text": "",
                    "path": rp,
                    "offset": 1,
                    "end": 0,
                    "total_lines": 0,
                    "complete_file": True,
                    "next_offset": None,
                    "selected_sha256": hashlib.sha256(b"").hexdigest(),
                    "full_sha256": hashlib.sha256(b"").hexdigest(),
                    "source_sha256": hashlib.sha256(b"").hexdigest(),
                }
                if explicit_window else ""
            )
            return ToolProjectionResult(
                f"{rp} (empty file; 0 lines)",
                programmatic_value=empty_value,
                receipt_metadata={
                    "projection": "file-content-v2",
                    "path": rp,
                    "offset": 1,
                    "end": 0,
                    "total_lines": 0,
                    "selected_bytes": 0,
                    "content_sha256": hashlib.sha256(b"").hexdigest(),
                    "full_sha256": hashlib.sha256(b"").hexdigest(),
                    "source_sha256": hashlib.sha256(b"").hexdigest(),
                    "display_bytes": 0,
                    "display_sha256": hashlib.sha256(b"").hexdigest(),
                    "display_truncated": False,
                    "programmatic_bytes": 0,
                    "programmatic_sha256": hashlib.sha256(b"").hexdigest(),
                    "programmatic_complete": True,
                    "programmatic_scope": "explicit_window" if explicit_window else "complete_file",
                    "programmatic_file_complete": True,
                    "truncated": False,
                    "ends_with_newline": False,
                },
            )
        if offset > total_lines:
            raise tools.ToolError(
                f"read_file offset {offset} is past the end of the file "
                f"({total_lines} lines)"
            )

        end = offset + len(selected) - 1
        text = "".join(selected)
        assert full_text is not None or explicit_window
        if (
            not explicit_window
            and len(full_text.encode("utf-8", errors="replace")) > MAX_COMPLETE_ARTIFACT_BYTES
        ):
            raise tools.ToolError(
                "decoded complete text exceeds the artifact limit after UTF-8 replacement; use explicit offset/limit windows",
                code="decoded_complete_read_exceeds_artifact_limit",
            )
        header = f"{rp} (lines {offset}-{end} of {total_lines})"
        notices = []
        if oversized_first_line:
            notices.append(
                f"[Line {offset} exceeded the {MAX_READ_BYTES}-byte output ceiling; "
                "the displayed line prefix is truncated. Use grep to locate focused "
                "text in this generated/minified line.]"
            )
        if end < total_lines:
            notices.append(
                f"[Showing lines {offset}-{end} of {total_lines}. "
                f"Use offset={end + 1} to continue.]"
            )
        body = f"{header}:\n{text}"
        if notices:
            body = body.rstrip("\r\n") + "\n\n" + "\n".join(notices)
        complete_file = bool(offset == 1 and end == total_lines and not byte_limited)
        if explicit_window:
            programmatic_value: Any = {
                "schema": "variant1.file-read-result.v2",
                "text": text,
                "path": rp,
                "offset": offset,
                "end": end,
                "total_lines": total_lines,
                "complete_file": complete_file,
                "next_offset": None if end >= total_lines else end + 1,
                "selected_sha256": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(),
                "full_sha256": decoded_hasher.hexdigest(),
                "source_sha256": source_hasher.hexdigest(),
            }
        elif len(full_text.encode("utf-8", errors="replace")) <= MAX_COMPLETE_PROGRAMMATIC_BYTES:
            programmatic_value = full_text
        else:
            full_programmatic = full_text.encode("utf-8", errors="replace")
            programmatic_value = ProgrammaticArtifactPayload(
                data=full_programmatic,
                media_type="text/plain; charset=utf-8",
                kind="complete_file_read",
                result={
                    "schema": "variant1.file-read-result.v2",
                    "text": None,
                    "path": rp,
                    "offset": 1,
                    "end": total_lines,
                    "total_lines": total_lines,
                    "complete_file": True,
                    "next_offset": None,
                    "selected_sha256": decoded_hasher.hexdigest(),
                    "full_sha256": decoded_hasher.hexdigest(),
                    "source_sha256": source_hasher.hexdigest(),
                },
            )
        programmatic_bytes = (
            len(text.encode("utf-8", errors="replace"))
            if explicit_window else len(full_text.encode("utf-8", errors="replace"))
        )
        programmatic_sha256 = (
            hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
            if explicit_window else decoded_hasher.hexdigest()
        )
        display_truncated = bool(end < total_lines or byte_limited)
        return ToolProjectionResult(
            body,
            programmatic_value=programmatic_value,
            receipt_metadata={
                "projection": "file-content-v2",
                "path": rp,
                "offset": offset,
                "end": end,
                "total_lines": total_lines,
                "selected_bytes": selected_bytes,
                "content_sha256": hashlib.sha256(
                    text.encode("utf-8", errors="replace")
                ).hexdigest(),
                "full_sha256": decoded_hasher.hexdigest(),
                "source_sha256": source_hasher.hexdigest(),
                "display_bytes": selected_bytes,
                "display_sha256": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(),
                "display_truncated": display_truncated,
                "programmatic_bytes": programmatic_bytes,
                "programmatic_sha256": programmatic_sha256,
                "programmatic_complete": not explicit_window or not byte_limited,
                "programmatic_scope": "explicit_window" if explicit_window else "complete_file",
                "programmatic_file_complete": not explicit_window or complete_file,
                "truncated": display_truncated,
                "ends_with_newline": text.endswith(("\n", "\r")),
            },
        )
    return await _thread(_r)


def _glob_match(relative_path: str, pattern: str) -> bool:
    """Match a slash-normalized relative path with ordinary glob semantics."""
    rel = relative_path.replace("\\", "/").strip("/")
    pat = pattern.replace("\\", "/").strip("/")
    if os.name == "nt":
        rel, pat = rel.casefold(), pat.casefold()
    patterns = [pat]
    collapsed = pat
    while "/**/" in collapsed:
        collapsed = collapsed.replace("/**/", "/", 1)
        patterns.append(collapsed)
    if pat.startswith("**/"):
        patterns.append(pat[3:])
    if "/" not in pat:
        return fnmatch.fnmatchcase(os.path.basename(rel), pat)
    path = PurePosixPath(rel)
    return any(path.match(item) for item in patterns)


def _internal_file_runtime_path(path: str) -> bool:
    """Hide the exact rollback-WAL tree from model-visible file discovery."""

    try:
        target = os.path.normcase(os.path.realpath(path))
        journal = os.path.normcase(os.path.realpath(_patch_transaction_root()))
        return os.path.commonpath((target, journal)) == journal
    except (OSError, ValueError):
        return False


async def glob_files(args):
    """List a folder, or recursively find files with real glob matching."""
    path = (args.get("path") or ".").strip()
    pattern = (args.get("pattern") or args.get("query") or "").strip()
    # An omitted pattern, ``*``, or ``.`` is a one-level directory listing.
    # Recursive patterns always use the recursive file matcher below.
    if not pattern or pattern in ("*", "."):
        rp = _check(path)

        def _l():
            from file_ignore import IgnoreMatcher

            if not os.path.isdir(rp):
                raise tools.ToolError(f"not a folder: {path}")
            matcher = IgnoreMatcher(rp)
            all_names = sorted(
                name for name in os.listdir(rp)
                if not os.path.islink(os.path.join(rp, name))
                and not _internal_file_runtime_path(os.path.join(rp, name))
                and not matcher.ignored(
                    name, os.path.isdir(os.path.join(rp, name))
                )
            )
            total = len(all_names)
            names = all_names[:MAX_LIST]
            if not names:
                return f"(empty) {rp}"
            rows = [("[dir] " + n) if os.path.isdir(os.path.join(rp, n)) else n
                    for n in names]
            if total > len(names):
                header = (
                    f"{len(names)} of {total} items in {rp} "
                    "(truncated; narrow the path):"
                )
            else:
                header = f"{total} items in {rp}:"
            return header + "\n" + "\n".join(rows)
        return await _thread(_l)

    rp = _check(path)

    def _s():
        from file_ignore import IgnoreMatcher

        if not os.path.isdir(rp):
            raise tools.ToolError(f"not a folder: {path}")
        matcher = IgnoreMatcher(rp)
        hits = []
        for root, dirs, files in os.walk(rp, followlinks=False):
            rel_folder = os.path.relpath(root, rp).replace("\\", "/")
            if rel_folder == ".":
                rel_folder = ""
            dirs[:] = sorted(
                name for name in dirs
                if not _is_reparse_or_link(os.path.join(root, name))
                and not _internal_file_runtime_path(os.path.join(root, name))
                and not matcher.ignored(
                    f"{rel_folder}/{name}".strip("/"), True
                ))
            for name in sorted(files):
                full = os.path.join(root, name)
                rel = f"{rel_folder}/{name}".strip("/")
                if (
                    not _is_reparse_or_link(full)
                    and not _internal_file_runtime_path(full)
                    and not matcher.ignored(rel, False)
                    and _glob_match(rel, pattern)
                ):
                    hits.append(full)
                    if len(hits) >= MAX_FIND:
                        return hits, True
        return hits, False
    hits, capped = await _thread(_s)
    if not hits:
        return f'No files matching "{pattern}" under {path}.'
    suffix = f" (first {MAX_FIND}; refine the pattern)" if capped else ""
    return f"{len(hits)} match(es){suffix}:\n" + "\n".join(hits)


async def grep_files(args):
    """Search text contents with ignore-aware traversal and glob filtering."""
    import re as _re
    path = (args.get("path") or ".").strip()
    pattern = (args.get("pattern") or args.get("query") or "").strip()
    if not pattern:
        raise tools.ToolError("grep needs a non-empty 'pattern'")
    path_glob = (args.get("glob") or args.get("path_glob") or "").strip()
    try:
        limit = int(args.get("limit") or 80)
    except (TypeError, ValueError):
        limit = 80
    limit = max(1, min(limit, MAX_FIND))
    rp = _check(path)

    try:
        rx = _re.compile(pattern, _re.IGNORECASE)
    except _re.error:
        rx = _re.compile(_re.escape(pattern), _re.IGNORECASE)

    def _search_file(full: str, out: list[str]) -> bool:
        if os.path.getsize(full) > MAX_SEARCH_FILE_BYTES:
            return False
        with open(full, "rb") as probe:
            if b"\0" in probe.read(4096):
                return False
        with open(full, "r", encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, 1):
                if rx.search(line):
                    out.append(f"{full}:{number}: {line.rstrip()[:500]}")
                    if len(out) >= limit:
                        return True
        return False

    def _plain():
        from file_ignore import IgnoreMatcher

        out: list[str] = []
        if os.path.isfile(rp):
            try:
                capped = _search_file(rp, out)
            except (OSError, UnicodeError):
                capped = False
            return out, capped
        if not os.path.isdir(rp):
            raise tools.ToolError(f"not a file or folder: {path}")
        matcher = IgnoreMatcher(rp)
        for root, dirs, files in os.walk(rp, followlinks=False):
            rel_folder = os.path.relpath(root, rp).replace("\\", "/")
            if rel_folder == ".":
                rel_folder = ""
            dirs[:] = sorted(
                name for name in dirs
                if not _is_reparse_or_link(os.path.join(root, name))
                and not matcher.ignored(f"{rel_folder}/{name}".strip("/"), True))
            for name in sorted(files):
                full = os.path.join(root, name)
                rel = f"{rel_folder}/{name}".strip("/")
                if (matcher.ignored(rel, False) or _is_reparse_or_link(full)
                        or (path_glob and not _glob_match(rel, path_glob))):
                    continue
                try:
                    if _search_file(full, out):
                        return out, True
                except (OSError, UnicodeError):
                    continue
        return out, False

    hits, capped = await _thread(_plain)
    if not hits:
        return f'No content matches for "{pattern}" under {path}.'
    suffix = (
        " (result limit reached; more matches may exist — narrow the path/glob "
        "or raise limit)"
        if capped else ""
    )
    return f"{len(hits)} match(es){suffix}:\n" + "\n".join(hits)


def _file_signature(path: str) -> tuple:
    try:
        stat = os.stat(path)
        return True, stat.st_size, stat.st_mtime_ns, getattr(stat, "st_ino", 0)
    except FileNotFoundError:
        return False, 0, 0, 0


_PATCH_OPERATION_FIELDS = frozenset({
    "path", "action", "content", "find", "replace", "count",
})


def _normalize_patch_args(args: dict) -> dict:
    """Normalize every public spelling into the one batch transaction form."""

    raw = dict(args or {})
    supplied_forms = int("changes" in raw) + int("change" in raw) + int(
        any(name in raw for name in _PATCH_OPERATION_FIELDS)
    )
    if supplied_forms != 1:
        raise tools.ToolError(
            "apply_patch needs exactly one of changes=[...], change={...}, "
            "or a direct path/action/content/find/replace operation"
        )
    if "changes" in raw:
        changes = raw.get("changes")
        if isinstance(changes, dict):
            changes = [changes]
    elif "change" in raw:
        changes = [raw.get("change")]
    else:
        changes = [{
            name: raw[name]
            for name in _PATCH_OPERATION_FIELDS
            if name in raw
        }]
    return {"changes": changes}


def _prepare_patch(args: dict) -> list[dict]:
    changes = args.get("changes")
    if not isinstance(changes, list) or not 1 <= len(changes) <= 50:
        raise tools.ToolError("apply_patch needs 1-50 items in 'changes'")
    prepared: list[dict] = []
    virtual: dict[str, dict] = {}
    for index, raw in enumerate(changes, 1):
        if not isinstance(raw, dict):
            raise tools.ToolError(f"apply_patch change {index} must be an object")
        action = str(raw.get("action") or "").strip().lower()
        if action == "create":
            action = "write"
        if not action:
            if isinstance(raw.get("content"), str) and not (
                "find" in raw or "replace" in raw
            ):
                action = "write"
            elif "find" in raw or "replace" in raw:
                action = "replace"
        path = str(raw.get("path") or "").strip()
        if action not in {"write", "replace", "delete"}:
            raise tools.ToolError(
                f"apply_patch change {index} action must be create/write, replace, or delete")
        if not path:
            raise tools.ToolError(f"apply_patch change {index} needs a path")
        rp = _check(path)
        if os.path.isdir(rp):
            raise tools.ToolError(f"apply_patch only changes files, not folders: {path}")
        state = virtual.get(rp)
        if state is None:
            signature = _file_signature(rp)
            exists = bool(signature[0])
            original = ""
            loaded = False
            if exists:
                try:
                    with open(rp, "r", encoding="utf-8", errors="replace") as handle:
                        original = handle.read()
                    loaded = True
                except OSError:
                    original = ""
            state = {
                "signature": signature,
                "exists": exists,
                "content": original if loaded else None,
                "loaded": loaded,
                "original": original if loaded or not exists else "",
            }
            virtual[rp] = state
        signature = state["signature"]
        row = {
            "action": action,
            "path": rp,
            "signature": signature,
            "original": state.get("original") or "",
        }
        if action == "write":
            content = raw.get("content")
            if not isinstance(content, str):
                raise tools.ToolError(
                    f"apply_patch write change {index} needs string content")
            row.update(kind="update" if state["exists"] else "create", content=content)
            state.update(exists=True, content=content, loaded=True)
        elif action == "replace":
            if not state["exists"]:
                raise tools.ToolError(f"apply_patch replace target is not a file: {path}")
            find = raw.get("find")
            replacement = raw.get("replace")
            if not isinstance(find, str) or not find:
                raise tools.ToolError(
                    f"apply_patch replace change {index} needs non-empty string find")
            if not isinstance(replacement, str):
                raise tools.ToolError(
                    f"apply_patch replace change {index} needs string replace")
            try:
                count = int(raw.get("count") or 0)
            except (TypeError, ValueError):
                raise tools.ToolError(
                    f"apply_patch replace change {index} count must be an integer")
            if count < 0:
                raise tools.ToolError(
                    f"apply_patch replace change {index} count cannot be negative")
            if not state["loaded"]:
                with open(rp, "r", encoding="utf-8", errors="replace") as handle:
                    state["content"] = handle.read()
                state["loaded"] = True
                if not state.get("original"):
                    state["original"] = state["content"]
            old = state["content"]
            occurrences = old.count(find)
            if not occurrences:
                raise tools.ToolError(f"find text was not found in {path}")
            used = occurrences if count == 0 else min(occurrences, count)
            content = old.replace(find, replacement, count) if count else old.replace(find, replacement)
            row.update(kind="update", content=content, replacements=used)
            state["content"] = content
        else:
            if not state["exists"]:
                raise tools.ToolError(f"apply_patch delete target is not a file: {path}")
            row["kind"] = "delete"
            state.update(exists=False, content=None, loaded=True)
        row["original"] = state.get("original") or ""
        prepared.append(row)
    return prepared


_PATCH_JOURNAL_SCHEMA = "variant1.file-patch-rollback-wal.v2"
_LEGACY_PATCH_JOURNAL_SCHEMA = "variant1.file-patch-transaction.v1"


class _PatchCancellationRequested(RuntimeError):
    pass


def _patch_transaction_root() -> str:
    configured = str(os.environ.get("VARIANT1_PATCH_JOURNAL_DIR") or "").strip()
    if configured:
        return os.path.abspath(os.path.expandvars(os.path.expanduser(configured)))
    data_root = str(os.environ.get("VARIANT1_DATA_DIR") or "").strip()
    if data_root:
        return os.path.join(os.path.abspath(data_root), "data", "file-patches")
    base = str(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
    return os.path.join(os.path.abspath(base), "VARIANT-1", "patch-transactions")


def _fsync_directory(path: str) -> None:
    try:
        flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0))
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_patch_journal(path: str, document: dict) -> None:
    root = os.path.dirname(path)
    os.makedirs(root, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".journal-", suffix=".tmp", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(root)
    finally:
        if os.path.exists(temporary):
            try:
                os.remove(temporary)
            except OSError:
                pass


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cleanup_patch_journal(document: dict, journal_path: str, *, rollback: bool) -> None:
    cleanup_errors: list[str] = []
    for item in document.get("operations") or ():
        for field in ("candidate", "backup"):
            path = str(item.get(field) or "")
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError as exc:
                    cleanup_errors.append(f"{path}: {exc}")
    if rollback:
        folders = sorted(
            {str(item) for item in document.get("made_dirs") or () if str(item)},
            key=lambda value: (value.count(os.sep), len(value)),
            reverse=True,
        )
        for folder in folders:
            try:
                os.rmdir(folder)
            except OSError:
                pass
    if cleanup_errors:
        raise OSError(
            "patch transaction cleanup is incomplete: "
            + "; ".join(cleanup_errors[:20])
        )
    if os.path.exists(journal_path):
        os.remove(journal_path)
        _fsync_directory(os.path.dirname(journal_path))


def _restore_patch_journal(document: dict, journal_path: str) -> None:
    operations = list(document.get("operations") or ())
    for item in reversed(operations):
        path = str(item.get("path") or "")
        if not path:
            raise tools.ToolError(f"patch transaction has an invalid target: {journal_path}")
        parent = os.path.dirname(path) or "."
        originally_existed = bool(item.get("original_exists"))
        original_sha = str(item.get("original_sha256") or "")
        backup = str(item.get("backup") or "")
        if originally_existed:
            backup_valid = bool(
                backup and os.path.isfile(backup)
                and _file_sha256(backup) == original_sha
            )
            target_valid = bool(
                os.path.isfile(path) and _file_sha256(path) == original_sha
            )
            if not backup_valid and not target_valid:
                raise tools.ToolError(
                    f"patch rollback evidence is incomplete for {path}; "
                    f"journal retained at {journal_path}"
                )
            if backup_valid and not target_valid:
                os.makedirs(parent, exist_ok=True)
                fd, restore = tempfile.mkstemp(
                    prefix=".variant1-patch-restore-", suffix=".tmp", dir=parent
                )
                os.close(fd)
                try:
                    shutil.copy2(backup, restore)
                    with open(restore, "r+b") as handle:
                        os.fsync(handle.fileno())
                    os.replace(restore, path)
                    _fsync_directory(parent)
                finally:
                    if os.path.exists(restore):
                        os.remove(restore)
        elif os.path.lexists(path):
            if os.path.isdir(path):
                raise tools.ToolError(
                    f"patch rollback target became a directory: {path}"
                )
            os.remove(path)
            _fsync_directory(parent)
    _cleanup_patch_journal(document, journal_path, rollback=True)


def _recover_patch_journal(journal_path: str) -> None:
    with open(journal_path, "r", encoding="utf-8") as handle:
        document = json.load(handle)
    if (
        not isinstance(document, dict)
        or document.get("schema") not in {
            _PATCH_JOURNAL_SCHEMA, _LEGACY_PATCH_JOURNAL_SCHEMA,
        }
    ):
        raise tools.ToolError(f"unknown patch transaction journal: {journal_path}")
    if str(document.get("state") or "") == "committed":
        _cleanup_patch_journal(document, journal_path, rollback=False)
        return
    _restore_patch_journal(document, journal_path)


def _recover_patch_transactions() -> int:
    root = _patch_transaction_root()
    if not os.path.isdir(root):
        return 0
    recovered = 0
    with _PATCH_TRANSACTION_LOCK:
        for name in sorted(os.listdir(root)):
            if not name.endswith(".json"):
                continue
            _recover_patch_journal(os.path.join(root, name))
            recovered += 1
    return recovered


def _apply_direct_patch(
    operations: list[dict],
    *,
    cancel_requested: threading.Event | None = None,
    broker_operation: dict[str, Any] | None = None,
) -> str:
    """Commit through an internal rollback WAL owned by one broker operation."""

    final_by_path: dict[str, dict] = {}
    for op in operations:
        final_by_path[op["path"]] = op
    final_operations = list(final_by_path.values())
    operation = dict(broker_operation or {})
    operation_id = str(operation.get("operation_id") or "")
    request_fingerprint = str(operation.get("request_fingerprint") or "")
    transaction_id = (
        "patch_" + hashlib.sha256(
            f"{operation_id}\0{request_fingerprint}".encode("utf-8")
        ).hexdigest()
        if operation_id and request_fingerprint
        else "patch_" + uuid.uuid4().hex
    )
    root = _patch_transaction_root()
    journal_path = os.path.join(root, transaction_id + ".json")
    with _PATCH_TRANSACTION_LOCK:
        _recover_patch_transactions()
        for op in final_operations:
            if _file_signature(op["path"]) != op["signature"]:
                raise tools.ToolError(
                    f"file changed while apply_patch was preparing: {op['path']}"
                )
        made_dirs: set[str] = set()
        journal_operations: list[dict] = []
        for index, op in enumerate(final_operations):
            path = op["path"]
            parent = os.path.dirname(path) or "."
            cursor = parent
            while cursor and not os.path.exists(cursor):
                made_dirs.add(cursor)
                next_cursor = os.path.dirname(cursor)
                if next_cursor == cursor:
                    break
                cursor = next_cursor
            original_exists = bool(op["signature"][0])
            journal_operations.append({
                "path": path,
                "kind": op["kind"],
                "candidate": (
                    os.path.join(parent, f".variant1-patch-{transaction_id}-{index}.candidate")
                    if op["kind"] in {"create", "update"} else ""
                ),
                "backup": (
                    os.path.join(parent, f".variant1-patch-{transaction_id}-{index}.backup")
                    if original_exists else ""
                ),
                "original_exists": original_exists,
                "original_sha256": _file_sha256(path) if original_exists else "",
            })
        document = {
            "schema": _PATCH_JOURNAL_SCHEMA,
            "role": "internal_rollback_wal",
            "transaction_id": transaction_id,
            "broker_operation": operation,
            "state": "preparing",
            "applied_count": 0,
            "made_dirs": sorted(made_dirs),
            "operations": journal_operations,
        }
        _write_patch_journal(journal_path, document)
        try:
            by_path = {item["path"]: item for item in journal_operations}
            for op in final_operations:
                path = op["path"]
                item = by_path[path]
                parent = os.path.dirname(path) or "."
                os.makedirs(parent, exist_ok=True)
                candidate = item["candidate"]
                if candidate:
                    with open(candidate, "w", encoding="utf-8", newline="") as handle:
                        handle.write(op["content"])
                        handle.flush()
                        os.fsync(handle.fileno())
                backup = item["backup"]
                if backup:
                    shutil.copy2(path, backup)
                    with open(backup, "r+b") as handle:
                        os.fsync(handle.fileno())
            for op in final_operations:
                if _file_signature(op["path"]) != op["signature"]:
                    raise tools.ToolError(
                        f"file changed while apply_patch was staging: {op['path']}"
                    )
            document["state"] = "committing"
            _write_patch_journal(journal_path, document)
            for index, op in enumerate(final_operations):
                if cancellation_is_requested(cancel_requested):
                    raise _PatchCancellationRequested(
                        "apply_patch cancelled before its atomic commit completed"
                    )
                path = op["path"]
                if _file_signature(path) != op["signature"]:
                    raise tools.ToolError(
                        f"file changed while apply_patch was committing: {path}"
                    )
                item = by_path[path]
                if op["kind"] in {"create", "update"}:
                    os.replace(item["candidate"], path)
                else:
                    os.remove(path)
                _fsync_directory(os.path.dirname(path) or ".")
                document["applied_count"] = index + 1
                _write_patch_journal(journal_path, document)
            document["state"] = "committed"
            _write_patch_journal(journal_path, document)
            _cleanup_patch_journal(document, journal_path, rollback=False)
        except Exception as exc:
            try:
                _recover_patch_journal(journal_path)
            except Exception as recovery_error:
                raise tools.ToolError(
                    f"apply_patch failed and durable rollback is pending: {recovery_error}"
                ) from exc
            raise

    lines = [f"Applied {len(operations)} file operation(s) as one recoverable batch:"]
    for op in operations:
        detail = (f" ({op['replacements']} replacement(s))"
                  if "replacements" in op else "")
        lines.append(f"- {op['action']} {op['path']}{detail}")
    return "\n".join(lines)


async def apply_patch(args):
    """Create, replace text in, or delete one or more text files as one patch."""
    public_args = dict(args or {})
    raw_args = _normalize_patch_args(public_args)
    prepared = _prepare_patch(raw_args)
    broker_operation: dict[str, Any] = {}
    try:
        from capability_broker import (
            capability_request_fingerprint,
            current_capability_invocation,
        )

        invocation = current_capability_invocation()
        if invocation is not None:
            request_fingerprint = capability_request_fingerprint(
                "apply_patch", public_args,
            )
            identity = {
                "chat_id": str(invocation.chat_id or ""),
                "run_id": str(invocation.run_id or ""),
                "outer_tool_call_id": str(invocation.outer_tool_call_id or ""),
                "nested_call_id": str(invocation.nested_call_id or ""),
                "idempotency_key": str(invocation.idempotency_key or ""),
            }
            operation_id = "capop_" + hashlib.sha256(
                json.dumps(
                    identity,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            broker_operation = {
                "operation_id": operation_id,
                "request_fingerprint": request_fingerprint,
                **identity,
            }
    except Exception:
        # Direct/programmatic use remains supported; it receives a random WAL
        # identity but never becomes a second semantic operation authority.
        broker_operation = {}
    cancel_requested = threading.Event()
    transaction = asyncio.create_task(
        asyncio.to_thread(
            _apply_direct_patch,
            prepared,
            cancel_requested=cancel_requested,
            broker_operation=broker_operation,
        ),
        name="variant1-apply-patch-transaction",
    )
    try:
        result = await asyncio.shield(transaction)
    except asyncio.CancelledError as cancellation:
        cancel_requested.set()
        # Do not return control while the filesystem transaction still owns
        # candidates or rollback evidence. Repeated cancellation only delays
        # the join; it cannot expose a partially committed batch as idle.
        while not transaction.done():
            try:
                await asyncio.shield(transaction)
            except asyncio.CancelledError:
                continue
        try:
            transaction.result()
        except Exception:
            pass
        raise cancellation
    return result


# -- registration ------------------------------------------------------------
# (name, handler, category, params, description, meta?)
# meta: optional dict with when / avoid / prefer_over / hidden
def _defs():
    return [
        # Unified file spine only — no hidden list/search/append/move aliases.
        ("read_file", read_file, "files",
         {"path": {"type": "string", "required": True,
                   "desc": "exact path of an already-known text file"},
          "offset": {"type": "int", "required": False,
                     "default": None,
                     "minimum": 1,
                     "desc": "line number to start a typed window (1-indexed); None keeps complete mode"},
          "limit": {"type": "int", "required": False,
                    "default": None,
                    "minimum": 1,
                    "desc": f"Maximum lines for a typed window; None keeps complete mode; positive values are capped at {MAX_READ_LINES} lines or {MAX_READ_BYTES} bytes."}},
         "Read one text file at an exact, already-known path. With no offset/limit, "
         f"Python receives the complete text up to {MAX_COMPLETE_PROGRAMMATIC_BYTES} bytes while model display stays bounded. "
         "Explicit offset/limit returns a typed window with completeness and continuation metadata. "
         "Larger complete reads return a scoped artifact reference rather than a partial string. This does not search folders; use glob "
         "to find a file by name.",
          {"when": "you need the contents of a file whose exact path is already known",
           "avoid": "finding files by name (use glob) or searching inside files (use grep)",
           "result_projection": "file-content-v2",
           "schema_revision": "variant1.read-file.v2"}),
        ("glob", glob_files, "files",
         {"path": {"type": "string", "required": False,
                   "desc": "folder to list/search under (default current directory)"},
          "pattern": {"type": "string", "required": False,
                      "desc": "glob such as *.py or src/**/*.ts; omit or '*' to list the folder"}},
         "List a folder, or recursively find files by name. "
         "Omit pattern (or use '*') to list one directory; pass a pattern to search "
         "recursively for matching filenames.",
         {"when": "listing a directory or finding files by name under a path",
          "avoid": "reading contents (use read_file) or searching inside file text (use grep)"}),
        ("grep", grep_files, "files",
         {"path": {"type": "string", "required": False,
                   "desc": "folder or file to search under (default current directory)"},
          "pattern": {"type": "string", "required": True,
                      "desc": "regex or literal text to find inside files"},
          "glob": {"type": "string", "required": False,
                   "desc": "optional filename filter (e.g. *.py)"},
          "limit": {"type": "int", "required": False,
                    "desc": "max matches (default 80)"}},
         "Search file contents (regex or literal). Prefer this over shell find/rg.",
         {"when": "finding symbols, strings, or text inside files",
          "avoid": "finding files by name only (use glob) or reading a whole known file "
                   "(use read_file)"}),
        ("apply_patch", apply_patch, "files",
         {"changes": {
              "type": "array", "required": False, "minItems": 1, "maxItems": 50,
              "coerce_singleton_object": True,
              "desc": "file operations applied together",
             "items": {
                 "type": "object", "additionalProperties": False,
                 "properties": {
                     "path": {"type": "string", "required": True,
                              "desc": "text file path"},
                     "action": {"type": "string", "required": False,
                                 "enum": ["create", "write", "replace", "delete"]},
                     "content": {"type": "string", "required": False,
                                 "desc": "complete content for write"},
                     "find": {"type": "string", "required": False,
                              "desc": "exact non-empty text for replace"},
                     "replace": {"type": "string", "required": False,
                                 "desc": "replacement text for replace"},
                     "count": {"type": "int", "required": False, "minimum": 0,
                               "desc": "replacement limit; 0 means all"},
                 },
             },
         },
          "change": {
              "type": "object", "required": False,
              "desc": "one file operation; normalized into the changes batch",
          },
          "path": {"type": "string", "required": False,
                   "desc": "direct single-operation text file path"},
          "action": {"type": "string", "required": False,
                     "enum": ["create", "write", "replace", "delete"]},
          "content": {"type": "string", "required": False},
          "find": {"type": "string", "required": False},
          "replace": {"type": "string", "required": False},
          "count": {"type": "int", "required": False, "minimum": 0}},
         "Apply ordered text-file changes as one recoverable batch (a path may repeat): "
         "use path+content (action='create' or action='write' is optional) to create or overwrite, "
         "path+find+replace (action='replace' is optional) "
         "for exact edits, or action='delete' with path."),
    ]


BUILTIN_NAMES = [d[0] for d in _defs()]


def register(registry):
    """Register the compact built-in file capabilities."""
    try:
        _recover_patch_transactions()
    except Exception as exc:
        # Keep startup available but leave the durable journal in place. A new
        # patch refuses to proceed until recovery succeeds.
        print(f"[apply_patch] transaction recovery pending: {exc}", flush=True)

    for row in _defs():
        name, fn, cat, params, desc = row[:5]
        meta = row[5] if len(row) > 5 else {}
        registry.register(tools.Tool(
            name, desc, fn, category=cat, params=params, **(meta or {})))
# end of builtin tool registrations
