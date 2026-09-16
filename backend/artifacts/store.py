"""Small content-addressed artifact store.

Artifact hashes are identity, not authority.  The broker records a scope on
every reference; the catalog repository enforces that grant at read/export
boundaries.  Filesystem paths are derived only from validated SHA-256 digests.
"""

from __future__ import annotations

import hashlib
import errno
from contextlib import contextmanager, nullcontext
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
from typing import Any, BinaryIO, Iterator

from core_invariants import (
    cancellation_is_requested,
    canonical_digest,
    canonical_json_bytes,
)
from tool_core import ArtifactRef, json_safe

from .models import (
    ARTIFACT_MANIFEST_SCHEMA,
    ArtifactExport,
    ArtifactIntegrityError,
    ArtifactManifest,
    ArtifactMetadata,
)


_REF_RE = re.compile(r"^artifact://sha256/([0-9a-f]{64})$")
_DEFAULT_MEDIA_TYPE = "application/octet-stream"
_DEFAULT_KIND = "capability_payload"
_STREAM_CHUNK_BYTES = 64 * 1024
_MAX_STREAM_CHUNK_BYTES = 4 * 1024 * 1024


class ArtifactExportCancelled(RuntimeError):
    pass


class ArtifactExportCancellation:
    """Thread-safe cancellation ordered against final file publication."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True

    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    @contextmanager
    def publication_guard(self):
        with self._lock:
            if self._cancelled:
                raise ArtifactExportCancelled("artifact export was cancelled")
            yield
_MAX_MANIFEST_ITEMS = 500


class ContentAddressedArtifactStore:
    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        self._lock = threading.RLock()
        self._grant_path = os.path.join(self.root, "artifact-grants.sqlite3")
        self._initialize_grants()

    def _initialize_grants(self) -> None:
        with self._lock:
            os.makedirs(self.root, exist_ok=True)
            with sqlite3.connect(self._grant_path, timeout=10.0) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS artifact_object (
                        ref TEXT PRIMARY KEY,
                        sha256 TEXT NOT NULL UNIQUE,
                        bytes INTEGER NOT NULL,
                        media_type TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        created_at REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS artifact_grant (
                        scope TEXT NOT NULL,
                        ref TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        bytes INTEGER NOT NULL,
                        media_type TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        PRIMARY KEY(scope, ref)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS artifact_semantic_grant (
                        scope TEXT NOT NULL,
                        ref TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        bytes INTEGER NOT NULL,
                        media_type TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        PRIMARY KEY(scope, ref, media_type, kind)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_artifact_semantic_kind
                    ON artifact_semantic_grant(scope, kind, created_at, ref)
                    """
                )

                # Stores created before the object catalog existed kept the same
                # immutable metadata on each grant. Backfill one deterministic
                # row without touching the content files or existing grants.
                conn.execute(
                    """
                    INSERT OR IGNORE INTO artifact_object(
                        ref, sha256, bytes, media_type, kind, created_at
                    )
                    SELECT g.ref, g.sha256, g.bytes, g.media_type, g.kind,
                           g.created_at
                    FROM artifact_grant AS g
                    WHERE g.rowid = (
                        SELECT candidate.rowid
                        FROM artifact_grant AS candidate
                        WHERE candidate.ref = g.ref
                        ORDER BY candidate.created_at ASC,
                                 candidate.scope ASC
                        LIMIT 1
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO artifact_semantic_grant(
                        scope, ref, sha256, bytes, media_type, kind, created_at
                    )
                    SELECT scope, ref, sha256, bytes, media_type, kind, created_at
                    FROM artifact_grant
                    """
                )

    def _record_grant(self, ref: ArtifactRef) -> None:
        with sqlite3.connect(self._grant_path, timeout=10.0) as conn:
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute(
                """
                INSERT OR IGNORE INTO artifact_object(
                    ref, sha256, bytes, media_type, kind, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    ref.ref,
                    ref.sha256,
                    int(ref.bytes),
                    ref.media_type,
                    ref.kind,
                    time.time(),
                ),
            )
            if not ref.scope:
                return
            conn.execute(
                """
                INSERT OR IGNORE INTO artifact_grant(
                    scope, ref, sha256, bytes, media_type, kind, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ref.scope,
                    ref.ref,
                    ref.sha256,
                    int(ref.bytes),
                    ref.media_type,
                    ref.kind,
                    time.time(),
                ),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO artifact_semantic_grant(
                    scope, ref, sha256, bytes, media_type, kind, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ref.scope,
                    ref.ref,
                    ref.sha256,
                    int(ref.bytes),
                    ref.media_type,
                    ref.kind,
                    time.time(),
                ),
            )

    @staticmethod
    def _clean_scope(scope: str) -> str:
        clean = str(scope or "").strip()
        if not clean:
            raise PermissionError("artifact scope is required")
        return clean

    @staticmethod
    def _ref_parts(ref: str | ArtifactRef) -> tuple[str, str]:
        ref_text = ref.ref if isinstance(ref, ArtifactRef) else str(ref or "")
        match = _REF_RE.fullmatch(ref_text)
        if not match:
            raise ValueError("invalid artifact reference")
        return ref_text, match.group(1)

    @staticmethod
    def _chunk_size(value: int) -> int:
        size = int(value)
        if size < 1 or size > _MAX_STREAM_CHUNK_BYTES:
            raise ValueError(
                f"chunk_size must be between 1 and {_MAX_STREAM_CHUNK_BYTES}"
            )
        return size

    def _path(self, digest: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("artifact digest must be lowercase SHA-256")
        return os.path.join(self.root, digest[:2], digest[2:4], digest)

    def put_bytes(
        self,
        payload: bytes,
        *,
        media_type: str = "application/octet-stream",
        kind: str = "capability_payload",
        scope: str = "",
    ) -> ArtifactRef:
        raw = bytes(payload)
        digest = hashlib.sha256(raw).hexdigest()
        target = self._path(digest)
        existing_valid = False
        if os.path.isfile(target):
            try:
                existing_valid = self._hash_path(target) == (digest, len(raw))
            except OSError:
                existing_valid = False
        if not existing_valid:
            parent = os.path.dirname(target)
            os.makedirs(parent, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=f".{digest}.", dir=parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                with self._lock:
                    os.replace(temporary, target)
                    temporary = ""
            finally:
                try:
                    if temporary and os.path.exists(temporary):
                        os.remove(temporary)
                except OSError:
                    pass
        ref = ArtifactRef(
            ref=f"artifact://sha256/{digest}",
            sha256=digest,
            bytes=len(raw),
            media_type=str(media_type or _DEFAULT_MEDIA_TYPE),
            kind=str(kind or _DEFAULT_KIND),
            scope=str(scope or ""),
        )
        with self._lock:
            self._record_grant(ref)
        return ref

    def put_file(
        self,
        path: str | os.PathLike[str],
        *,
        media_type: str = "application/octet-stream",
        kind: str = "capability_payload",
        scope: str = "",
        max_bytes: int | None = None,
        chunk_size: int = 1024 * 1024,
    ) -> ArtifactRef:
        """Stream one file into CAS without constructing a whole-file buffer."""

        source = os.path.abspath(os.fspath(path))
        size = self._chunk_size(chunk_size)
        bound = None if max_bytes is None else max(0, int(max_bytes))
        staging = os.path.join(self.root, ".ingest")
        os.makedirs(staging, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="artifact-", dir=staging)
        digest = hashlib.sha256()
        total = 0
        try:
            with open(source, "rb") as reader, os.fdopen(fd, "wb") as writer:
                while True:
                    chunk = reader.read(size)
                    if not chunk:
                        break
                    total += len(chunk)
                    if bound is not None and total > bound:
                        raise ValueError(f"artifact file exceeds {bound} bytes")
                    digest.update(chunk)
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            sha256 = digest.hexdigest()
            target = self._path(sha256)
            existing_valid = False
            if os.path.isfile(target):
                try:
                    existing_valid = self._hash_path(target) == (sha256, total)
                except OSError:
                    existing_valid = False
            if not existing_valid:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with self._lock:
                    os.replace(temporary, target)
                    temporary = ""
            ref = ArtifactRef(
                ref=f"artifact://sha256/{sha256}",
                sha256=sha256,
                bytes=total,
                media_type=str(media_type or _DEFAULT_MEDIA_TYPE),
                kind=str(kind or _DEFAULT_KIND),
                scope=str(scope or ""),
            )
            with self._lock:
                self._record_grant(ref)
            return ref
        finally:
            if temporary:
                try:
                    os.remove(temporary)
                except FileNotFoundError:
                    pass

    def put_text(self, value: str, *, kind: str, scope: str = "") -> ArtifactRef:
        return self.put_bytes(
            str(value).encode("utf-8"),
            media_type="text/plain; charset=utf-8",
            kind=kind,
            scope=scope,
        )

    def put_json(self, value: Any, *, kind: str, scope: str = "") -> ArtifactRef:
        payload = canonical_json_bytes(json_safe(value))
        return self.put_bytes(
            payload,
            media_type="application/json",
            kind=kind,
            scope=scope,
        )

    def read_bytes(self, ref: str) -> bytes:
        _ref_text, digest = self._ref_parts(ref)
        with open(self._path(digest), "rb") as handle:
            return handle.read()

    def read_bytes_scoped(self, ref: str, scope: str) -> bytes:
        self.stat(ref, scope=scope)
        return self.read_bytes(ref)

    def stat(
        self,
        ref: str | ArtifactRef,
        *,
        scope: str | None = None,
        verify: bool = False,
    ) -> ArtifactMetadata:
        """Return typed metadata for an object or an explicit scoped grant.

        ``scope=None`` is the host-level object lookup used by composition and
        migration code. Passing a scope enforces that exact grant. ``verify``
        additionally streams the object and checks both its length and digest.
        """

        ref_text, digest = self._ref_parts(ref)
        clean_scope = self._clean_scope(scope) if scope is not None else ""
        hint_media = ref.media_type if isinstance(ref, ArtifactRef) else ""
        hint_kind = ref.kind if isinstance(ref, ArtifactRef) else ""
        with self._lock:
            with sqlite3.connect(self._grant_path, timeout=10.0) as conn:
                conn.row_factory = sqlite3.Row
                if clean_scope:
                    row = conn.execute(
                        """
                        SELECT ref, sha256, bytes, media_type, kind, created_at
                        FROM artifact_semantic_grant WHERE scope=? AND ref=?
                        ORDER BY CASE WHEN media_type=? AND kind=? THEN 0 ELSE 1 END,
                                 created_at DESC
                        LIMIT 1
                        """,
                        (clean_scope, ref_text, hint_media, hint_kind),
                    ).fetchone()
                    if row is None:
                        raise PermissionError(
                            "artifact is outside this scope's grant"
                        )
                else:
                    row = conn.execute(
                        """
                        SELECT ref, sha256, bytes, media_type, kind, created_at
                        FROM artifact_object WHERE ref=?
                        """,
                        (ref_text,),
                    ).fetchone()
            if row is None:
                # A blob may predate the metadata table and have no grant. It is
                # still a valid CAS object; register conservative metadata so
                # future stat/grant calls are stable.
                path = self._path(digest)
                if not os.path.isfile(path):
                    raise FileNotFoundError(ref_text)
                file_stat = os.stat(path)
                metadata = ArtifactMetadata(
                    ref=ref_text,
                    sha256=digest,
                    bytes=int(file_stat.st_size),
                    media_type=_DEFAULT_MEDIA_TYPE,
                    kind=_DEFAULT_KIND,
                    created_at=float(file_stat.st_mtime),
                )
                self._record_grant(ArtifactRef(
                    ref=metadata.ref,
                    sha256=metadata.sha256,
                    bytes=metadata.bytes,
                    media_type=metadata.media_type,
                    kind=metadata.kind,
                ))
            else:
                metadata = ArtifactMetadata(
                    ref=str(row["ref"]),
                    sha256=str(row["sha256"]),
                    bytes=int(row["bytes"]),
                    media_type=str(row["media_type"]),
                    kind=str(row["kind"]),
                    created_at=float(row["created_at"]),
                    scope=clean_scope,
                )

        path = self._path(digest)
        if not os.path.isfile(path):
            raise FileNotFoundError(ref_text)
        if metadata.sha256 != digest:
            raise ArtifactIntegrityError(
                "artifact metadata digest does not match its reference"
            )
        if verify:
            actual_digest, actual_bytes = self._hash_path(path)
            if (
                actual_digest != metadata.sha256
                or actual_bytes != metadata.bytes
            ):
                raise ArtifactIntegrityError(
                    "artifact bytes do not match their content-addressed identity"
                )
        return metadata

    @staticmethod
    def _hash_path(path: str) -> tuple[str, int]:
        digest = hashlib.sha256()
        total = 0
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(_STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                digest.update(chunk)
        return digest.hexdigest(), total

    def grant(
        self,
        ref: str | ArtifactRef,
        scope: str,
        *,
        source_scope: str | None = None,
        verify: bool = True,
    ) -> ArtifactRef:
        """Grant an existing object to another scope without copying bytes.

        Host composition may omit ``source_scope``. Scoped callers should pass
        it so metadata is inherited only from an artifact they can already
        access. The target scope is always explicit.
        """

        target_scope = self._clean_scope(scope)
        metadata = self.stat(ref, scope=source_scope, verify=verify)
        granted = ArtifactRef(
            ref=metadata.ref,
            sha256=metadata.sha256,
            bytes=metadata.bytes,
            media_type=metadata.media_type,
            kind=metadata.kind,
            scope=target_scope,
        )
        with self._lock:
            self._record_grant(granted)
        return granted

    def manifest(
        self,
        scope: str,
        *,
        limit: int = 200,
        after_ref: str = "",
        verify: bool = False,
    ) -> ArtifactManifest:
        """Return a deterministic, bounded page of a scope's artifact grants."""

        clean_scope = self._clean_scope(scope)
        cap = max(1, min(int(limit), _MAX_MANIFEST_ITEMS))
        cursor = str(after_ref or "").strip()
        if cursor:
            self._ref_parts(cursor)
        with self._lock:
            with sqlite3.connect(self._grant_path, timeout=10.0) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT ref, sha256, bytes, media_type, kind, created_at
                    FROM (
                        SELECT ref, sha256, bytes, media_type, kind, created_at,
                               ROW_NUMBER() OVER (
                                   PARTITION BY ref
                                   ORDER BY created_at DESC, kind DESC, media_type DESC
                               ) AS semantic_rank
                        FROM artifact_semantic_grant
                        WHERE scope=? AND ref>?
                    )
                    WHERE semantic_rank=1
                    ORDER BY ref ASC
                    LIMIT ?
                    """,
                    (clean_scope, cursor, cap + 1),
                ).fetchall()

        has_more = len(rows) > cap
        page_rows = rows[:cap]
        items = tuple(
            ArtifactMetadata(
                ref=str(row["ref"]),
                sha256=str(row["sha256"]),
                bytes=int(row["bytes"]),
                media_type=str(row["media_type"]),
                kind=str(row["kind"]),
                created_at=float(row["created_at"]),
                scope=clean_scope,
            )
            for row in page_rows
        )
        if verify:
            items = tuple(
                self.stat(item.ref, scope=clean_scope, verify=True)
                for item in items
            )
        next_after_ref = items[-1].ref if has_more and items else ""
        digest_payload = {
            "schema": ARTIFACT_MANIFEST_SCHEMA,
            "scope": clean_scope,
            "items": [item.to_dict() for item in items],
            "has_more": has_more,
            "next_after_ref": next_after_ref,
        }
        digest = canonical_digest(digest_payload)
        return ArtifactManifest(
            scope=clean_scope,
            items=items,
            has_more=has_more,
            next_after_ref=next_after_ref,
            total_bytes=sum(item.bytes for item in items),
            digest=digest,
        )

    def open_reader(
        self,
        ref: str | ArtifactRef,
        *,
        scope: str | None = None,
    ) -> BinaryIO:
        """Open a binary reader after resolving the optional scoped grant."""

        metadata = self.stat(ref, scope=scope)
        return open(self._path(metadata.sha256), "rb")

    def iter_bytes(
        self,
        ref: str | ArtifactRef,
        *,
        scope: str | None = None,
        chunk_size: int = _STREAM_CHUNK_BYTES,
        verify: bool = True,
    ) -> Iterator[bytes]:
        """Stream bounded chunks, optionally verifying identity at EOF."""

        size = self._chunk_size(chunk_size)
        metadata = self.stat(ref, scope=scope)
        path = self._path(metadata.sha256)

        def _chunks() -> Iterator[bytes]:
            digest = hashlib.sha256()
            total = 0
            with open(path, "rb") as handle:
                while True:
                    chunk = handle.read(size)
                    if not chunk:
                        break
                    total += len(chunk)
                    if verify:
                        digest.update(chunk)
                    yield chunk
            if verify and (
                total != metadata.bytes
                or digest.hexdigest() != metadata.sha256
            ):
                raise ArtifactIntegrityError(
                    "artifact bytes do not match their content-addressed identity"
                )

        return _chunks()

    def export_to(
        self,
        ref: str | ArtifactRef,
        destination: str | os.PathLike[str],
        *,
        scope: str | None = None,
        overwrite: bool = False,
        verify: bool = True,
        chunk_size: int = _STREAM_CHUNK_BYTES,
        cancellation: Any | None = None,
        pre_publish: Any | None = None,
    ) -> ArtifactExport:
        """Atomically export an artifact and return a portable receipt."""

        def cancelled() -> bool:
            return cancellation_is_requested(cancellation)

        if cancelled():
            raise ArtifactExportCancelled("artifact export was cancelled")
        metadata = self.stat(ref, scope=scope)
        target = os.path.abspath(os.fspath(destination))
        if target == self._path(metadata.sha256):
            raise ValueError("artifact cannot be exported over its CAS object")
        parent = os.path.dirname(target)
        os.makedirs(parent, exist_ok=True)
        if os.path.exists(target) and not overwrite:
            raise FileExistsError(errno.EEXIST,
                                  "Destination exists; choose another path or pass overwrite=True",
                                  target)

        fd, temporary = tempfile.mkstemp(prefix=".variant1-artifact-", dir=parent)
        written = 0
        try:
            with os.fdopen(fd, "wb") as handle:
                for chunk in self.iter_bytes(
                    metadata.ref,
                    scope=scope,
                    chunk_size=chunk_size,
                    verify=verify,
                ):
                    if cancelled():
                        raise ArtifactExportCancelled(
                            "artifact export was cancelled"
                        )
                    handle.write(chunk)
                    written += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if written != metadata.bytes:
                raise ArtifactIntegrityError(
                    "exported byte count does not match artifact metadata"
                )
            guard_factory = getattr(cancellation, "publication_guard", None)
            guard = guard_factory() if callable(guard_factory) else nullcontext()
            with guard:
                if cancelled():
                    raise ArtifactExportCancelled("artifact export was cancelled")
                if callable(pre_publish):
                    pre_publish()
                if overwrite:
                    os.replace(temporary, target)
                    temporary = ""
                else:
                    # A hard-link from the completed same-directory temporary
                    # file is an atomic create-if-absent publication. Unlike
                    # os.replace, it can never overwrite a concurrent winner.
                    try:
                        os.link(temporary, target)
                    except FileExistsError as exc:
                        raise FileExistsError(
                            errno.EEXIST,
                            "Destination exists; choose another path or pass overwrite=True",
                            target,
                        ) from exc
                    os.remove(temporary)
                    temporary = ""
        finally:
            if temporary:
                try:
                    os.remove(temporary)
                except FileNotFoundError:
                    pass
        return ArtifactExport(
            ref=metadata.ref,
            sha256=metadata.sha256,
            bytes=written,
            destination=target,
            verified=bool(verify),
        )

    def list_scope(
        self,
        scope: str,
        *,
        limit: int = 20,
        kind: str = "",
    ) -> list[dict[str, Any]]:
        clean_scope = str(scope or "").strip()
        if not clean_scope:
            return []
        cap = max(1, min(int(limit), 200))
        clean_kind = str(kind or "").strip()
        with self._lock:
            with sqlite3.connect(self._grant_path, timeout=10.0) as conn:
                conn.row_factory = sqlite3.Row
                if clean_kind:
                    rows = conn.execute(
                        """
                        SELECT ref, sha256, bytes, media_type, kind, created_at
                        FROM artifact_semantic_grant WHERE scope=? AND kind=?
                        ORDER BY created_at DESC, ref ASC LIMIT ?
                        """,
                        (clean_scope, clean_kind, cap),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT ref, sha256, bytes, media_type, kind, created_at
                        FROM artifact_semantic_grant WHERE scope=?
                        ORDER BY created_at DESC, ref ASC LIMIT ?
                        """,
                        (clean_scope, cap),
                    ).fetchall()
        return [dict(row) for row in rows]

    def exists(self, ref: str) -> bool:
        try:
            _ref_text, digest = self._ref_parts(ref)
        except ValueError:
            return False
        return os.path.isfile(self._path(digest))
