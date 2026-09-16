"""Immutable executable extension packages and contribution catalog v2."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Mapping

from core_invariants import cancellation_is_requested, canonical_json as _stable
from execution_hosts.local import inherited_environment, run_bounded_child
from .manifests_v2 import ExtensionManifest, ExtensionManifestError, load_extension_manifest, source_manifest
from .manifests_v2 import _IGNORED as _SOURCE_IGNORED
from .skill_format import parse_skill


class ExtensionPackageError(RuntimeError):
    pass


def _semver(value: str) -> tuple[int, int, int]:
    core = str(value).split("-", 1)[0]
    try:
        return tuple(int(part) for part in core.split("."))  # type: ignore[return-value]
    except Exception as exc:
        raise ExtensionPackageError(f"invalid compatibility version: {value}") from exc


def _range_allows(version: str, expression: str) -> bool:
    current = _semver(version)
    for token in str(expression or "").split():
        if token.startswith(">=") and current < _semver(token[2:]): return False
        if token.startswith(">") and current <= _semver(token[1:]): return False
        if token.startswith("<=") and current > _semver(token[2:]): return False
        if token.startswith("<") and current >= _semver(token[1:]): return False
        if token.startswith("=") and current != _semver(token[1:]): return False
    return True


def build_extension_environment(
    source: Path,
    environment: Path,
    lock: Path | None,
    *,
    cancellation_requested: Callable[[], bool] | None = None,
) -> Mapping[str, Any]:
    """Build the one immutable dependency environment used by all packages."""

    environment.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--target",
        str(environment),
    ]
    if lock is not None:
        command.extend(["--requirement", str(lock)])
        if lock.name == "requirements.lock":
            command.append("--require-hashes")
    elif (source / "pyproject.toml").is_file() or (source / "setup.py").is_file():
        command.append(str(source))
    else:
        return {"command": [], "stdout": "", "mode": "source_only"}
    completed = run_bounded_child(
        command,
        cwd=str(source),
        env=inherited_environment(),
        timeout=180,
        cancellation_requested=cancellation_requested,
        max_stdout_bytes=8 * 1024 * 1024,
        max_stderr_bytes=8 * 1024 * 1024,
    )
    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    if completed.cancelled:
        raise ExtensionPackageError("extension package operation was cancelled")
    if completed.output_limit_exceeded:
        raise ExtensionPackageError("extension environment output exceeded 8 MiB")
    if completed.timed_out:
        raise ExtensionPackageError("extension environment installation timed out")
    if completed.returncode != 0:
        diagnostic = (stderr or stdout or "pip failed")[-4000:]
        raise ExtensionPackageError(diagnostic)
    return {
        "command": ["python", "-m", "pip", "install", "--target", "<isolated>"],
        "stdout": stdout[-2000:],
        "mode": "isolated_target",
    }


class ExtensionPackageService:
    """Process-owned package authority; it never imports plugin code in-process."""

    def __init__(self, database_path: str, root: str, *, variant1_version: str = "0.1.0",
                 environment_builder: Callable[..., Mapping[str, Any]] | None = None) -> None:
        self.path = os.path.abspath(database_path)
        self.root = Path(root).resolve(); self.root.mkdir(parents=True, exist_ok=True)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.variant1_version = str(variant1_version)
        self.environment_builder = environment_builder or build_extension_environment
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL"); conn.execute("PRAGMA synchronous=FULL")
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS extension_package_v2(
              package_id TEXT NOT NULL, version TEXT NOT NULL, package_digest TEXT NOT NULL UNIQUE,
              name TEXT NOT NULL, manifest_json TEXT NOT NULL, source_manifest_json TEXT NOT NULL,
              source_path TEXT NOT NULL, environment_path TEXT NOT NULL, worker_json TEXT NOT NULL,
              status TEXT NOT NULL, installed_at REAL NOT NULL, PRIMARY KEY(package_id,version));
            CREATE TABLE IF NOT EXISTS extension_activation_v2(
              package_id TEXT PRIMARY KEY, package_digest TEXT NOT NULL, catalog_revision TEXT NOT NULL,
              predecessor_digest TEXT NOT NULL DEFAULT '', activated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS extension_catalog_pointer_v2(
              singleton INTEGER PRIMARY KEY CHECK(singleton=1), catalog_revision TEXT NOT NULL,
              updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS extension_contribution_v2(
              package_digest TEXT NOT NULL, kind TEXT NOT NULL, contribution_id TEXT NOT NULL,
              descriptor_json TEXT NOT NULL, descriptor_digest TEXT NOT NULL,
              PRIMARY KEY(package_digest,kind,contribution_id));
            CREATE TABLE IF NOT EXISTS extension_pin_v2(
              chat_id TEXT NOT NULL, package_id TEXT NOT NULL, package_digest TEXT NOT NULL,
              catalog_revision TEXT NOT NULL, pinned_at REAL NOT NULL, PRIMARY KEY(chat_id,package_id));
            CREATE TABLE IF NOT EXISTS extension_dev_mount_v2(
              chat_id TEXT NOT NULL, package_id TEXT NOT NULL, source_path TEXT NOT NULL,
              source_digest TEXT NOT NULL, manifest_json TEXT NOT NULL, mounted_at REAL NOT NULL,
              PRIMARY KEY(chat_id,package_id));
            CREATE TABLE IF NOT EXISTS extension_event_v2(
              sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
              package_id TEXT NOT NULL, package_digest TEXT NOT NULL, payload_json TEXT NOT NULL,
              created_at REAL NOT NULL);
            """)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row; conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def retire_legacy_catalog(self) -> list[str]:
        """Remove packages and tables owned by the retired extension catalog."""

        legacy_tables = {
            "extension_skill_source",
            "extension_skill_usage",
            "extension_skill_proposal",
            "extension_app_source",
            "extension_app_state",
        }
        package_ids: set[str] = set()
        digests: set[str] = set()
        with self._lock, self._connect() as conn:
            existing = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for table in ("extension_skill_source", "extension_app_source"):
                if table in existing:
                    package_ids.update(
                        str(row[0]) for row in conn.execute(
                            f"SELECT DISTINCT package_id FROM {table}"
                        ).fetchall() if str(row[0] or "")
                    )
            if not package_ids and not (legacy_tables & existing):
                return []
            conn.execute("BEGIN IMMEDIATE")
            for package_id in sorted(package_ids):
                digests.update(
                    str(row[0]) for row in conn.execute(
                        "SELECT package_digest FROM extension_package_v2 WHERE package_id=?",
                        (package_id,),
                    ).fetchall()
                )
                conn.execute("DELETE FROM extension_activation_v2 WHERE package_id=?", (package_id,))
                conn.execute("DELETE FROM extension_pin_v2 WHERE package_id=?", (package_id,))
                conn.execute("DELETE FROM extension_dev_mount_v2 WHERE package_id=?", (package_id,))
                conn.execute("DELETE FROM extension_event_v2 WHERE package_id=?", (package_id,))
                conn.execute("DELETE FROM extension_package_v2 WHERE package_id=?", (package_id,))
            for digest in sorted(digests):
                conn.execute(
                    "DELETE FROM extension_contribution_v2 WHERE package_digest=?",
                    (digest,),
                )
                if "extension_worker_operation_v2" in existing:
                    conn.execute(
                        "DELETE FROM extension_worker_operation_v2 WHERE package_digest=?",
                        (digest,),
                    )
            for table in sorted(legacy_tables & existing):
                conn.execute(f"DROP TABLE {table}")
            rows = [dict(row) for row in conn.execute(
                "SELECT c.package_digest,c.kind,c.contribution_id,c.descriptor_digest "
                "FROM extension_activation_v2 a JOIN extension_contribution_v2 c "
                "ON c.package_digest=a.package_digest"
            ).fetchall()]
            revision = self._catalog_revision(rows)
            conn.execute(
                "INSERT INTO extension_catalog_pointer_v2 VALUES (1,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET "
                "catalog_revision=excluded.catalog_revision,updated_at=excluded.updated_at",
                (revision, time.time()),
            )
            conn.commit()
        for digest in digests:
            shutil.rmtree(self.root / "packages" / digest, ignore_errors=True)
        return sorted(package_ids)

    def _compatible(self, manifest: ExtensionManifest) -> None:
        compatibility = dict(manifest.compatibility)
        if compatibility.get("variant1") and not _range_allows(self.variant1_version, str(compatibility["variant1"])):
            raise ExtensionPackageError("package is incompatible with this VARIANT-1 version")
        py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        if compatibility.get("python") and not _range_allows(py, str(compatibility["python"])):
            raise ExtensionPackageError("package is incompatible with this Python version")
        platforms = [str(item).lower() for item in compatibility.get("platforms") or []]
        effective = "win32-x64" if sys.platform == "win32" and sys.maxsize > 2**32 else sys.platform.lower()
        if platforms and effective not in platforms:
            raise ExtensionPackageError(f"package does not support {effective}")

    @staticmethod
    def _check_cancelled(
        cancellation_requested: Callable[[], bool] | None,
    ) -> None:
        if cancellation_is_requested(cancellation_requested):
            raise ExtensionPackageError("extension package operation cancelled")

    def _build_environment(
        self,
        source: Path,
        environment: Path,
        lock: Path | None,
        cancellation_requested: Callable[[], bool] | None,
    ) -> Mapping[str, Any]:
        """Invoke the builder while preserving the injectable three-arg seam."""

        builder = self.environment_builder
        try:
            parameters = inspect.signature(builder).parameters.values()
        except (TypeError, ValueError):
            parameters = ()
        accepts_cancellation = any(
            item.name == "cancellation_requested"
            or item.kind is inspect.Parameter.VAR_KEYWORD
            for item in parameters
        )
        if accepts_cancellation:
            return builder(
                source,
                environment,
                lock,
                cancellation_requested=cancellation_requested,
            )
        return builder(source, environment, lock)

    @staticmethod
    def _publish_package_root(staged_root: Path, package_root: Path) -> None:
        """Atomically publish a digest directory despite transient Windows locks."""

        if package_root.exists():
            return
        for attempt in range(20):
            try:
                os.rename(staged_root, package_root)
                return
            except FileExistsError:
                if package_root.exists():
                    return
                raise
            except PermissionError:
                if package_root.exists():
                    return
                if attempt == 19:
                    raise
                time.sleep(0.025 * (attempt + 1))

    @staticmethod
    def _validate_contributions(root: Path, manifest: ExtensionManifest) -> list[dict[str, Any]]:
        rows = []
        for kind in sorted(manifest.contributions):
            for descriptor in manifest.contributions[kind]:
                item = dict(descriptor)
                for field in ("path", "entry", "input_schema"):
                    if not item.get(field): continue
                    target = (root / str(item[field])).resolve()
                    try: target.relative_to(root)
                    except ValueError as exc: raise ExtensionPackageError(f"{kind}/{item['id']} escapes package root") from exc
                    if not target.is_file(): raise ExtensionPackageError(f"missing contribution file: {item[field]}")
                    if field == "input_schema":
                        value = json.loads(target.read_text(encoding="utf-8"))
                        if not isinstance(value, Mapping): raise ExtensionPackageError("input schema must be an object")
                    item[f"{field}_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
                    if kind == "skills" and field == "path":
                        metadata, body = parse_skill(target.read_text(encoding="utf-8"))
                        item["name"] = str(
                            item.get("name") or metadata.get("name") or item["id"]
                        ).strip()[:200]
                        item["description"] = str(
                            item.get("description")
                            or metadata.get("description")
                            or "(no description)"
                        ).strip()[:500]
                        item["body_sha256"] = hashlib.sha256(
                            body.encode("utf-8")
                        ).hexdigest()
                        item["body_chars"] = len(body)
                        skill_root = target.parent
                        item["resources"] = [
                            {"path": child.relative_to(skill_root).as_posix(),
                             "sha256": hashlib.sha256(child.read_bytes()).hexdigest(),
                             "bytes": child.stat().st_size}
                            for child in sorted(
                                (candidate for candidate in skill_root.rglob("*") if candidate.is_file()),
                                key=lambda candidate: candidate.relative_to(skill_root).as_posix(),
                            )
                        ]
                digest = hashlib.sha256(_stable(item).encode()).hexdigest()
                rows.append({"kind": kind, "id": item["id"], "descriptor": item, "descriptor_digest": digest})
        for raw in manifest.entrypoints.get("mcp_servers") or ():
            item = dict(raw)
            digest = hashlib.sha256(_stable(item).encode()).hexdigest()
            rows.append({"kind": "mcp_server", "id": item["id"],
                         "descriptor": item, "descriptor_digest": digest})
        return rows

    @staticmethod
    def _catalog_revision(rows: list[Mapping[str, Any]]) -> str:
        projection = [{"package_digest": row["package_digest"], "kind": row["kind"],
                       "id": row["contribution_id"], "digest": row["descriptor_digest"]}
                      for row in sorted(rows, key=lambda x: (x["package_digest"], x["kind"], x["contribution_id"]))]
        return hashlib.sha256(_stable(projection).encode()).hexdigest()

    def install(
        self,
        source: str,
        *,
        activate: bool = True,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        self._check_cancelled(cancellation_requested)
        source_root = Path(source).absolute()
        if not source_root.is_dir():
            raise ExtensionPackageError("extension source directory is unavailable")
        manifest = load_extension_manifest(source_root); self._compatible(manifest)
        files, digest = source_manifest(source_root)
        contributions = self._validate_contributions(source_root, manifest)
        self._check_cancelled(cancellation_requested)
        with self._lock, self._connect() as conn:
            prior = conn.execute("SELECT * FROM extension_package_v2 WHERE package_id=? AND version=?",
                                 (manifest.package_id, manifest.version)).fetchone()
            if prior is not None:
                if str(prior["package_digest"]) != digest:
                    raise ExtensionPackageError("package version is immutable and already has a different digest")
                self._check_cancelled(cancellation_requested)
                if activate: self._activate(conn, manifest.package_id, digest)
                self._check_cancelled(cancellation_requested)
                conn.commit(); return self.inspect(manifest.package_id, version=manifest.version)
        package_root = self.root / "packages" / digest
        staged = Path(tempfile.mkdtemp(prefix="extension-", dir=str(self.root)))
        try:
            staged_root = staged / digest; staged_source = staged_root / "source"
            def copy_file(source_file, destination_file, *args, **kwargs):
                self._check_cancelled(cancellation_requested)
                return shutil.copy2(source_file, destination_file, *args, **kwargs)

            shutil.copytree(
                source_root,
                staged_source,
                ignore=shutil.ignore_patterns(*sorted(_SOURCE_IGNORED)),
                copy_function=copy_file,
            )
            self._check_cancelled(cancellation_requested)
            staged_files, staged_digest = source_manifest(staged_source)
            if staged_digest != digest or staged_files != files:
                raise ExtensionPackageError(
                    "extension source changed during staging; nothing was published"
                )
            environment = staged_root / "environment"; build: Mapping[str, Any] = {"mode": "source_only"}
            lock = next((staged_source / name for name in ("requirements.lock", "requirements.txt") if (staged_source / name).is_file()), None)
            if manifest.entrypoints.get("python") or lock:
                build = self._build_environment(
                    staged_source, environment, lock, cancellation_requested
                )
            self._check_cancelled(cancellation_requested)
            worker = {"schema": "variant1.extension-worker.v2", "python": sys.executable,
                      "launcher": "same_variant1_backend",
                      "protocol": "variant1.extension-worker.protocol.v1",
                      "module": dict(manifest.entrypoints.get("python") or {}).get("module"),
                      "environment": str(package_root / "environment"),
                      "source": str(package_root / "source"),
                      "import_paths": [str(package_root / "environment"),
                                       str(package_root / "source" / "python"),
                                       str(package_root / "source")],
                      "out_of_process": True,
                      "build": dict(build)}
            package_root.parent.mkdir(parents=True, exist_ok=True)
            self._check_cancelled(cancellation_requested)
            self._publish_package_root(staged_root, package_root)
            self._check_cancelled(cancellation_requested)
            published_files, published_digest = source_manifest(package_root / "source")
            if published_digest != digest or published_files != files:
                raise ExtensionPackageError(
                    "published extension source does not match its staged digest"
                )
        finally:
            shutil.rmtree(staged, ignore_errors=True)
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._check_cancelled(cancellation_requested)
            conn.execute("INSERT INTO extension_package_v2 VALUES (?,?,?,?,?,?,?,?,?,'ready',?)",
                         (manifest.package_id, manifest.version, digest, manifest.name,
                          _stable(manifest.to_dict()), _stable(files), str(package_root / "source"),
                          str(package_root / "environment"), _stable(worker), now))
            for row in contributions:
                conn.execute("INSERT INTO extension_contribution_v2 VALUES (?,?,?,?,?)",
                             (digest, row["kind"], row["id"], _stable(row["descriptor"]), row["descriptor_digest"]))
            self._check_cancelled(cancellation_requested)
            if activate: self._activate(conn, manifest.package_id, digest)
            conn.execute("INSERT INTO extension_event_v2(event_type,package_id,package_digest,payload_json,created_at) VALUES ('package.installed',?,?,?,?)",
                         (manifest.package_id, digest, _stable({"version": manifest.version, "activated": activate}), now))
            self._check_cancelled(cancellation_requested)
            conn.commit()
        return self.inspect(manifest.package_id, version=manifest.version)

    def _activate(self, conn: sqlite3.Connection, package_id: str, digest: str) -> str:
        active = conn.execute("SELECT package_digest FROM extension_activation_v2 WHERE package_id=?", (package_id,)).fetchone()
        predecessor = str(active[0]) if active and str(active[0]) != digest else ""
        active_digests = [str(row[0]) for row in conn.execute("SELECT package_digest FROM extension_activation_v2 WHERE package_id<>?", (package_id,)).fetchall()] + [digest]
        placeholders = ",".join("?" for _ in active_digests)
        packages = [dict(row) for row in conn.execute(
            f"SELECT package_id,version,package_digest,manifest_json FROM extension_package_v2 "
            f"WHERE package_digest IN ({placeholders})", active_digests,
        ).fetchall()]
        versions = {str(row["package_id"]): str(row["version"]) for row in packages}
        for row in packages:
            dependencies = dict(json.loads(str(row["manifest_json"])).get("dependencies") or {})
            for dependency_id, requirement in dict(dependencies.get("plugins") or {}).items():
                selected = versions.get(str(dependency_id))
                if selected is None or not _range_allows(selected, str(requirement)):
                    raise ExtensionPackageError(
                        f"unsatisfied plugin dependency for {row['package_id']}: "
                        f"{dependency_id} {requirement}"
                    )
        rows = [dict(row) for row in conn.execute(f"SELECT package_digest,kind,contribution_id,descriptor_digest FROM extension_contribution_v2 WHERE package_digest IN ({placeholders})", active_digests).fetchall()]
        owners: dict[tuple[str, str], str] = {}
        for row in rows:
            key = (str(row["kind"]), str(row["contribution_id"]))
            prior = owners.get(key)
            if prior is not None and prior != str(row["package_digest"]):
                raise ExtensionPackageError(
                    f"active contribution collision: {key[0]}/{key[1]}"
                )
            owners[key] = str(row["package_digest"])
        revision = self._catalog_revision(rows)
        conn.execute("INSERT INTO extension_activation_v2 VALUES (?,?,?,?,?) ON CONFLICT(package_id) DO UPDATE SET package_digest=excluded.package_digest,catalog_revision=excluded.catalog_revision,predecessor_digest=excluded.predecessor_digest,activated_at=excluded.activated_at",
                     (package_id, digest, revision, predecessor, time.time()))
        conn.execute("INSERT INTO extension_catalog_pointer_v2 VALUES (1,?,?) "
                     "ON CONFLICT(singleton) DO UPDATE SET catalog_revision=excluded.catalog_revision,updated_at=excluded.updated_at",
                     (revision, time.time()))
        return revision

    def activate(
        self,
        package_id: str,
        *,
        version: str = "",
        digest: str = "",
        projection: Callable[[sqlite3.Connection, Mapping[str, Any]], None]
        | None = None,
    ) -> dict[str, Any]:
        """Activate one already-materialized immutable package revision."""

        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if digest:
                target = conn.execute(
                    "SELECT package_digest FROM extension_package_v2 "
                    "WHERE package_id=? AND package_digest=? AND status='ready'",
                    (str(package_id), str(digest)),
                ).fetchone()
            elif version:
                target = conn.execute(
                    "SELECT package_digest FROM extension_package_v2 "
                    "WHERE package_id=? AND version=? AND status='ready'",
                    (str(package_id), str(version)),
                ).fetchone()
            else:
                target = conn.execute(
                    "SELECT package_digest FROM extension_package_v2 "
                    "WHERE package_id=? AND status='ready' "
                    "ORDER BY installed_at DESC,version DESC LIMIT 1",
                    (str(package_id),),
                ).fetchone()
            if target is None:
                conn.rollback()
                raise LookupError("package revision is unavailable")
            selected = str(target[0])
            revision = self._activate(conn, str(package_id), selected)
            conn.execute(
                "INSERT INTO extension_event_v2(event_type,package_id,package_digest,"
                "payload_json,created_at) VALUES ('package.activated',?,?,?,?)",
                (str(package_id), selected, _stable({"catalog_revision": revision}), time.time()),
            )
            if projection is not None:
                projection(conn, {
                    "package_id": str(package_id),
                    "package_digest": selected,
                    "catalog_revision": revision,
                    "active": True,
                })
            conn.commit()
        return self.inspect(str(package_id), digest=selected)

    def deactivate(
        self,
        package_id: str,
        *,
        projection: Callable[[sqlite3.Connection, Mapping[str, Any]], None]
        | None = None,
        preserve_pins: bool = False,
    ) -> bool:
        """Deactivate a package while retaining every immutable revision."""

        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                "SELECT package_digest FROM extension_activation_v2 WHERE package_id=?",
                (str(package_id),),
            ).fetchone()
            if active is None:
                if not preserve_pins:
                    conn.execute(
                        "DELETE FROM extension_pin_v2 WHERE package_id=?",
                        (str(package_id),),
                    )
                if projection is not None:
                    pointer = conn.execute(
                        "SELECT catalog_revision FROM extension_catalog_pointer_v2 "
                        "WHERE singleton=1"
                    ).fetchone()
                    projection(conn, {
                        "package_id": str(package_id),
                        "package_digest": "",
                        "catalog_revision": str(pointer[0]) if pointer else "",
                        "active": False,
                    })
                    conn.commit()
                else:
                    conn.rollback()
                return False
            digest = str(active[0])
            conn.execute(
                "DELETE FROM extension_activation_v2 WHERE package_id=?",
                (str(package_id),),
            )
            if not preserve_pins:
                conn.execute(
                    "DELETE FROM extension_pin_v2 WHERE package_id=?",
                    (str(package_id),),
                )
            rows = [dict(row) for row in conn.execute(
                "SELECT c.package_digest,c.kind,c.contribution_id,c.descriptor_digest "
                "FROM extension_activation_v2 a JOIN extension_contribution_v2 c "
                "ON c.package_digest=a.package_digest"
            ).fetchall()]
            revision = self._catalog_revision(rows)
            conn.execute(
                "INSERT INTO extension_catalog_pointer_v2 VALUES (1,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET "
                "catalog_revision=excluded.catalog_revision,updated_at=excluded.updated_at",
                (revision, time.time()),
            )
            conn.execute(
                "INSERT INTO extension_event_v2(event_type,package_id,package_digest,"
                "payload_json,created_at) VALUES ('package.deactivated',?,?,?,?)",
                (str(package_id), digest, _stable({"catalog_revision": revision}), time.time()),
            )
            if projection is not None:
                projection(conn, {
                    "package_id": str(package_id),
                    "package_digest": digest,
                    "catalog_revision": revision,
                    "active": False,
                })
            conn.commit()
        return True

    def update(
        self, source: str, *,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        self._check_cancelled(cancellation_requested)
        manifest = load_extension_manifest(Path(source).resolve())
        try: active = self.inspect(manifest.package_id)
        except LookupError:
            return self.install(
                source, activate=True,
                cancellation_requested=cancellation_requested,
            )
        if _semver(manifest.version) <= _semver(str(active["version"])):
            raise ExtensionPackageError("update version must be newer than the active version; use rollback for older versions")
        return self.install(
            source, activate=True,
            cancellation_requested=cancellation_requested,
        )

    def rollback(
        self, package_id: str, *, version: str = "",
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._check_cancelled(cancellation_requested)
            active = conn.execute("SELECT * FROM extension_activation_v2 WHERE package_id=?", (package_id,)).fetchone()
            if active is None: raise LookupError("package is not active")
            if version:
                target = conn.execute("SELECT package_digest FROM extension_package_v2 WHERE package_id=? AND version=? AND status='ready'", (package_id, version)).fetchone()
            else:
                target = (active["predecessor_digest"],) if active["predecessor_digest"] else None
            if not target: raise LookupError("rollback target is unavailable")
            digest = str(target[0]); revision = self._activate(conn, package_id, digest)
            conn.execute("INSERT INTO extension_event_v2(event_type,package_id,package_digest,payload_json,created_at) VALUES ('package.rolled_back',?,?,?,?)", (package_id, digest, _stable({"catalog_revision": revision}), time.time()))
            self._check_cancelled(cancellation_requested)
            conn.commit()
        return self.inspect(package_id, digest=digest)

    def pin(self, chat_id: str, package_id: str, *, version: str = "") -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            if version:
                row = conn.execute("SELECT package_digest FROM extension_package_v2 WHERE package_id=? AND version=?", (package_id, version)).fetchone()
            else:
                row = conn.execute("SELECT package_digest FROM extension_activation_v2 WHERE package_id=?", (package_id,)).fetchone()
            if row is None: raise LookupError("package version is unavailable")
            digest = str(row[0]); revision = hashlib.sha256(f"{package_id}:{digest}".encode()).hexdigest()
            conn.execute("INSERT INTO extension_pin_v2 VALUES (?,?,?,?,?) ON CONFLICT(chat_id,package_id) DO UPDATE SET package_digest=excluded.package_digest,catalog_revision=excluded.catalog_revision,pinned_at=excluded.pinned_at", (chat_id, package_id, digest, revision, time.time()))
        return {"chat_id": chat_id, "package_id": package_id, "package_digest": digest, "catalog_revision": revision}

    def dev_mount(self, path: str, *, chat_id: str) -> dict[str, Any]:
        root = Path(path).resolve(); manifest = load_extension_manifest(root); self._compatible(manifest)
        _files, digest = source_manifest(root); self._validate_contributions(root, manifest)
        with self._lock, self._connect() as conn:
            conn.execute("INSERT INTO extension_dev_mount_v2 VALUES (?,?,?,?,?,?) ON CONFLICT(chat_id,package_id) DO UPDATE SET source_path=excluded.source_path,source_digest=excluded.source_digest,manifest_json=excluded.manifest_json,mounted_at=excluded.mounted_at", (chat_id, manifest.package_id, str(root), digest, _stable(manifest.to_dict()), time.time()))
        return {"schema": "variant1.extension-dev-mount.v1", "chat_id": chat_id,
                "package_id": manifest.package_id, "source_path": str(root),
                "source_digest": digest, "immutable": False, "chat_local": True}

    def promote_dev_mount(
        self, chat_id: str, package_id: str, *,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        self._check_cancelled(cancellation_requested)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT source_path,source_digest FROM extension_dev_mount_v2 "
                "WHERE chat_id=? AND package_id=?", (chat_id, package_id),
            ).fetchone()
        if row is None: raise LookupError("developer mount is unavailable")
        _files, digest = source_manifest(str(row["source_path"]))
        if digest != str(row["source_digest"]):
            raise ExtensionPackageError("developer mount changed; validate and mount it again")
        installed = self.install(
            str(row["source_path"]), activate=True,
            cancellation_requested=cancellation_requested,
        )
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM extension_dev_mount_v2 WHERE chat_id=? AND package_id=?",
                         (chat_id, package_id))
        return installed

    def inspect(self, package_id: str, *, version: str = "", digest: str = "") -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            if digest: row = conn.execute("SELECT * FROM extension_package_v2 WHERE package_id=? AND package_digest=?", (package_id, digest)).fetchone()
            elif version: row = conn.execute("SELECT * FROM extension_package_v2 WHERE package_id=? AND version=?", (package_id, version)).fetchone()
            else: row = conn.execute(
                "SELECT p.* FROM extension_package_v2 p "
                "LEFT JOIN extension_activation_v2 a ON a.package_digest=p.package_digest "
                "WHERE p.package_id=? ORDER BY (a.package_digest IS NOT NULL) DESC,"
                "p.installed_at DESC,p.version DESC LIMIT 1", (package_id,)
            ).fetchone()
            if row is None: raise LookupError("unknown extension package")
            contributions = [dict(item) for item in conn.execute("SELECT kind,contribution_id,descriptor_json,descriptor_digest FROM extension_contribution_v2 WHERE package_digest=? ORDER BY kind,contribution_id", (row["package_digest"],)).fetchall()]
            active = conn.execute("SELECT package_digest,catalog_revision FROM extension_activation_v2 WHERE package_id=?", (package_id,)).fetchone()
            pointer = conn.execute("SELECT catalog_revision FROM extension_catalog_pointer_v2 WHERE singleton=1").fetchone()
        return {"schema": "variant1.extension-package.v2", "package_id": row["package_id"], "name": row["name"], "version": row["version"], "package_digest": row["package_digest"], "status": row["status"], "active": bool(active and active["package_digest"] == row["package_digest"]), "catalog_revision": str(pointer["catalog_revision"] if pointer else ""), "manifest": json.loads(row["manifest_json"]), "source_path": row["source_path"], "environment_path": row["environment_path"], "worker": json.loads(row["worker_json"]), "contributions": [{"kind": item["kind"], "id": item["contribution_id"], "descriptor": json.loads(item["descriptor_json"]), "descriptor_digest": item["descriptor_digest"]} for item in contributions], "immutable": True}

    def list_contributions(self, package_id: str, *, chat_id: str = "") -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            dev = conn.execute(
                "SELECT * FROM extension_dev_mount_v2 WHERE chat_id=? AND package_id=?",
                (chat_id, package_id),
            ).fetchone() if chat_id else None
            if dev is not None:
                root = Path(str(dev["source_path"])).resolve()
                manifest = load_extension_manifest(root)
                _files, current_digest = source_manifest(root)
                if current_digest != str(dev["source_digest"]):
                    raise ExtensionPackageError("developer mount changed; validate and mount it again")
                rows = self._validate_contributions(root, manifest)
                return [{**row, "package_digest": "dev:" + current_digest,
                         "development": True} for row in rows]
            pin = conn.execute("SELECT package_digest FROM extension_pin_v2 WHERE chat_id=? AND package_id=?", (chat_id, package_id)).fetchone() if chat_id else None
            active = conn.execute("SELECT package_digest FROM extension_activation_v2 WHERE package_id=?", (package_id,)).fetchone()
            selected = pin or active
            if selected is None: return []
            rows = conn.execute("SELECT kind,contribution_id,descriptor_json,descriptor_digest FROM extension_contribution_v2 WHERE package_digest=? ORDER BY kind,contribution_id", (selected[0],)).fetchall()
        return [{"kind": row["kind"], "id": row["contribution_id"], "descriptor": json.loads(row["descriptor_json"]), "descriptor_digest": row["descriptor_digest"], "package_digest": str(selected[0])} for row in rows]

    def resolved_contributions(
        self, *, kind: str = "", chat_id: str = ""
    ) -> list[dict[str, Any]]:
        """Return one pinned-or-active contribution set from the SQL catalog."""

        with self._lock, self._connect() as conn:
            params: list[Any] = []
            if chat_id:
                selected = (
                    "WITH selected AS ("
                    "SELECT a.package_id,COALESCE(p.package_digest,a.package_digest) "
                    "AS package_digest FROM extension_activation_v2 a "
                    "LEFT JOIN extension_pin_v2 p ON p.package_id=a.package_id "
                    "AND p.chat_id=? UNION SELECT p.package_id,p.package_digest "
                    "FROM extension_pin_v2 p WHERE p.chat_id=? AND NOT EXISTS "
                    "(SELECT 1 FROM extension_activation_v2 a WHERE a.package_id=p.package_id)) "
                )
                params.extend([str(chat_id), str(chat_id)])
            else:
                selected = (
                    "WITH selected AS (SELECT package_id,package_digest "
                    "FROM extension_activation_v2) "
                )
            clauses = []
            if kind:
                clauses.append("c.kind=?")
                params.append(str(kind))
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = conn.execute(
                selected
                + "SELECT s.package_id,p.name,p.version,p.package_digest,"
                "p.manifest_json,c.kind,c.contribution_id,c.descriptor_json,"
                "c.descriptor_digest FROM selected s JOIN extension_package_v2 p "
                "ON p.package_digest=s.package_digest JOIN extension_contribution_v2 c "
                "ON c.package_digest=s.package_digest"
                + where
                + " ORDER BY c.kind,c.contribution_id,s.package_id",
                tuple(params),
            ).fetchall()
        return [{
            "package_id": str(row["package_id"]),
            "package_name": str(row["name"]),
            "version": str(row["version"]),
            "package_digest": str(row["package_digest"]),
            "manifest": json.loads(row["manifest_json"]),
            "kind": str(row["kind"]),
            "id": str(row["contribution_id"]),
            "descriptor": json.loads(row["descriptor_json"]),
            "descriptor_digest": str(row["descriptor_digest"]),
        } for row in rows]

    def list_packages(
        self, *, query: str = "", active_only: bool = False, limit: int = 500
    ) -> list[dict[str, Any]]:
        cap = max(1, min(int(limit), 5000))
        if active_only:
            clauses = []
            params: list[Any] = []
            if query:
                clauses.append("(lower(p.package_id) LIKE ? OR lower(p.name) LIKE ?)")
                needle = f"%{str(query).casefold()}%"
                params.extend([needle, needle])
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            params.append(cap)
            with self._lock, self._connect() as conn:
                rows = conn.execute(
                    "SELECT p.*,a.package_digest AS active_digest,a.catalog_revision "
                    "FROM extension_activation_v2 a JOIN extension_package_v2 p "
                    "ON p.package_digest=a.package_digest" + where
                    + " ORDER BY p.name,p.package_id LIMIT ?",
                    tuple(params),
                ).fetchall()
            return [{
                "package_id": str(row["package_id"]),
                "name": str(row["name"]),
                "version": str(row["version"]),
                "package_digest": str(row["package_digest"]),
                "active_digest": str(row["active_digest"] or ""),
                "active": True,
                "catalog_revision": str(row["catalog_revision"] or ""),
                "manifest": json.loads(row["manifest_json"]),
                "installed_at": float(row["installed_at"]),
            } for row in rows]
        clauses = ["ranked.position=1"]
        params: list[Any] = []
        if query:
            clauses.append("(lower(ranked.package_id) LIKE ? OR lower(ranked.name) LIKE ?)")
            needle = f"%{str(query).casefold()}%"
            params.extend([needle, needle])
        params.append(cap)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "WITH ranked AS (SELECT p.*,ROW_NUMBER() OVER (PARTITION BY package_id "
                "ORDER BY installed_at DESC,version DESC) AS position "
                "FROM extension_package_v2 p WHERE p.status='ready') "
                "SELECT ranked.*,a.package_digest AS active_digest,a.catalog_revision "
                "FROM ranked LEFT JOIN extension_activation_v2 a "
                "ON a.package_id=ranked.package_id WHERE " + " AND ".join(clauses)
                + " ORDER BY ranked.name,ranked.package_id LIMIT ?",
                tuple(params),
            ).fetchall()
        return [{
            "package_id": str(row["package_id"]),
            "name": str(row["name"]),
            "version": str(row["version"]),
            "package_digest": str(row["package_digest"]),
            "active_digest": str(row["active_digest"] or ""),
            "active": str(row["active_digest"] or "") == str(row["package_digest"]),
            "catalog_revision": str(row["catalog_revision"] or ""),
            "manifest": json.loads(row["manifest_json"]),
            "installed_at": float(row["installed_at"]),
        } for row in rows]

    def search(self, query: str = "", *, kind: str = "", limit: int = 100) -> list[dict[str, Any]]:
        needle = str(query or "").casefold()
        cap = max(1, min(500, int(limit)))
        clauses: list[str] = []
        params: list[Any] = []
        if kind:
            clauses.append(
                "EXISTS (SELECT 1 FROM extension_contribution_v2 ck "
                "WHERE ck.package_digest=p.package_digest AND ck.kind=?)"
            )
            params.append(str(kind))
        if needle:
            clauses.append(
                "(lower(p.package_id) LIKE ? OR lower(p.name) LIKE ? OR EXISTS "
                "(SELECT 1 FROM extension_contribution_v2 cq WHERE "
                "cq.package_digest=p.package_digest AND "
                "(lower(cq.contribution_id) LIKE ? OR lower(cq.descriptor_json) LIKE ?)))"
            )
            like = f"%{needle}%"
            params.extend([like, like, like, like])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(cap)
        with self._lock, self._connect() as conn:
            selected = conn.execute(
                "SELECT p.package_id,p.name,p.version,p.package_digest,"
                "a.catalog_revision,p.manifest_json,p.installed_at "
                "FROM extension_activation_v2 a JOIN extension_package_v2 p "
                "ON p.package_digest=a.package_digest" + where
                + " ORDER BY p.name,p.package_id LIMIT ?",
                tuple(params),
            ).fetchall()
        rows = [{
            "package_id": str(row["package_id"]),
            "name": str(row["name"]),
            "version": str(row["version"]),
            "package_digest": str(row["package_digest"]),
            "active_digest": str(row["package_digest"]),
            "active": True,
            "catalog_revision": str(row["catalog_revision"]),
            "manifest": json.loads(row["manifest_json"]),
            "installed_at": float(row["installed_at"]),
        } for row in selected]
        output = []
        for row in rows:
            contributions = self.list_contributions(str(row["package_id"]))
            if kind:
                contributions = [
                    item for item in contributions if item["kind"] == kind
                ]
                if not contributions:
                    continue
            output.append({**dict(row), "contribution_count": len(contributions)})
        return output

    def read_resource(self, package_id: str, contribution_id: str, resource: str, *,
                      chat_id: str = "") -> dict[str, Any]:
        matches = [item for item in self.list_contributions(package_id, chat_id=chat_id)
                   if item["id"] == contribution_id and item["kind"] == "skills"]
        if len(matches) != 1: raise LookupError("unknown skill contribution")
        digest = matches[0]["package_digest"]
        with self._lock, self._connect() as conn:
            if str(digest).startswith("dev:"):
                row = conn.execute("SELECT source_path FROM extension_dev_mount_v2 WHERE chat_id=? AND package_id=?", (chat_id, package_id)).fetchone()
            else:
                row = conn.execute("SELECT source_path FROM extension_package_v2 WHERE package_digest=?", (digest,)).fetchone()
        descriptor = matches[0]["descriptor"]
        skill_root = (Path(str(row[0])) / str(descriptor["path"])).resolve().parent
        target = (skill_root / str(resource)).resolve()
        try: target.relative_to(skill_root)
        except ValueError as exc: raise ExtensionPackageError("resource escapes the skill root") from exc
        allowed = {str(item["path"]): str(item["sha256"]) for item in descriptor.get("resources") or []}
        relative = target.relative_to(skill_root).as_posix()
        if relative not in allowed or not target.is_file(): raise LookupError("resource is not in the pinned skill revision")
        data = target.read_bytes()
        if hashlib.sha256(data).hexdigest() != allowed[relative]:
            raise ExtensionPackageError("immutable skill resource digest changed")
        try: text = data.decode("utf-8")
        except UnicodeDecodeError: text = None
        return {"package_id": package_id, "package_digest": digest, "skill_id": contribution_id,
                "path": relative, "sha256": allowed[relative], "bytes": len(data), "text": text,
                "binary": text is None}


def create_extension_package_service(data_dir: str, *, variant1_version: str = "0.1.0",
                                     environment_builder=None) -> ExtensionPackageService:
    base = Path(data_dir).resolve() / "extensions"
    return ExtensionPackageService(str(base / "extensions.sqlite3"), str(base),
                                   variant1_version=variant1_version,
                                   environment_builder=environment_builder)


__all__ = ["ExtensionPackageError", "ExtensionPackageService", "create_extension_package_service"]
