"""Argument-vector Git adapter with machine-readable parsers.

No method invokes a shell.  Observations disable optional locks, external
diffs, text conversion, paging, and terminal prompts so results are stable in
headless and packaged hosts.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import shutil
import time
from typing import Any, Sequence

from execution_hosts.local import run_bounded_child

from .models import (
    BranchRecord,
    CommitRecord,
    DiffFile,
    GitCommandError,
    GitUnavailable,
    StatusEntry,
    StatusSnapshot,
)


_MAX_OUTPUT_BYTES = 128 * 1024 * 1024
_MAX_DIAGNOSTIC_CHARS = 4000


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogateescape")


def _normal_path(value: str) -> str:
    return str(value or "").replace("\\", "/")


@dataclass(frozen=True, slots=True)
class GitResult:
    arguments: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def text(self) -> str:
        # Git terminates scalar output with a line ending.  Do not use strip():
        # repository paths may legally begin or end with whitespace.
        return _decode(self.stdout).rstrip("\r\n")


@dataclass(frozen=True, slots=True)
class RepositoryObservation:
    root: str
    git_dir: str
    common_dir: str
    object_format: str
    default_branch: str
    head_oid: str
    branch: str
    remotes: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class WorktreeObservation:
    root: str
    head_oid: str
    branch_ref: str
    detached: bool
    bare: bool
    locked: str
    prunable: str


@dataclass(frozen=True, slots=True)
class DiffObservation:
    files: tuple[DiffFile, ...]
    patch: bytes


class GitProcess:
    """Bounded synchronous Git execution and porcelain parsing."""

    def __init__(
        self,
        executable: str | None = None,
        *,
        timeout_s: float = 30.0,
        max_output_bytes: int = _MAX_OUTPUT_BYTES,
    ) -> None:
        selected = str(executable or shutil.which("git") or "").strip()
        if not selected:
            raise GitUnavailable("Git executable was not found")
        self.executable = os.path.abspath(selected) if os.path.isabs(selected) else selected
        self.timeout_s = max(0.1, min(float(timeout_s), 3600.0))
        self.max_output_bytes = max(64 * 1024, int(max_output_bytes))

    @staticmethod
    def _environment(*, mutating: bool) -> dict[str, str]:
        environment = dict(os.environ)
        environment.update({
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "GIT_EDITOR": "true",
            "GIT_SEQUENCE_EDITOR": "true",
            "LC_ALL": "C",
            "LANG": "C",
        })
        if not mutating:
            environment["GIT_OPTIONAL_LOCKS"] = "0"
        return environment

    def run(
        self,
        root: str,
        arguments: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        timeout_s: float | None = None,
        check: bool = True,
        mutating: bool = False,
    ) -> GitResult:
        base = os.path.realpath(os.path.abspath(str(root or ".")))
        argv = tuple(str(item) for item in arguments)
        if any("\x00" in item for item in argv):
            raise ValueError("Git arguments cannot contain NUL characters")
        command = [self.executable, "-c", "core.quotepath=false", "-c", "core.longpaths=true",
                   "-c", "core.hooksPath=" + os.devnull, "-C", base, *argv]
        try:
            completed = run_bounded_child(
                command,
                cwd=base,
                env=self._environment(mutating=mutating),
                input_bytes=input_bytes,
                timeout=(self.timeout_s if timeout_s is None else float(timeout_s)),
                max_stdout_bytes=self.max_output_bytes,
                max_stderr_bytes=self.max_output_bytes,
            )
        except (FileNotFoundError, NotADirectoryError) as exc:
            missing = getattr(exc, "filename", None)
            if not os.path.isdir(base):
                raise GitCommandError(
                    f"Git working directory is unavailable: {base}",
                    arguments=argv,
                    returncode=-1,
                ) from exc
            if isinstance(exc, FileNotFoundError):
                exe = os.path.normcase(os.path.normpath(str(self.executable)))
                if (
                    not missing
                    or os.path.normcase(os.path.normpath(str(missing))) == exe
                ):
                    raise GitUnavailable(
                        f"Git executable is unavailable: {self.executable}"
                    ) from exc
            raise GitCommandError(
                f"Git command could not start: {exc}", arguments=argv, returncode=-1
            ) from exc
        except OSError as exc:
            raise GitCommandError(
                f"Git command could not start: {exc}", arguments=argv, returncode=-1
            ) from exc
        if completed.output_limit_exceeded:
            raise GitCommandError(
                "Git command output exceeded the configured bound",
                arguments=argv,
                returncode=int(completed.returncode),
            )
        if completed.timed_out:
            raise GitCommandError(
                "Git command timed out",
                arguments=argv,
                returncode=-1,
            )
        stdout = bytes(completed.stdout or b"")
        stderr = bytes(completed.stderr or b"")
        if len(stdout) > self.max_output_bytes or len(stderr) > self.max_output_bytes:
            raise GitCommandError(
                "Git command output exceeded the configured bound",
                arguments=argv,
                returncode=int(completed.returncode),
            )
        result = GitResult(argv, int(completed.returncode), stdout, stderr)
        if check and result.returncode:
            diagnostic = _decode(stderr)[:_MAX_DIAGNOSTIC_CHARS].strip()
            raise GitCommandError(
                diagnostic or f"Git command exited with {result.returncode}",
                arguments=argv,
                returncode=result.returncode,
                stderr=diagnostic,
            )
        return result

    def resolve(self, root: str, ref: str = "HEAD", *, allow_missing: bool = False) -> str:
        target = str(ref or "HEAD").strip()
        result = self.run(
            root,
            ["rev-parse", "--verify", "--end-of-options", f"{target}^{{commit}}"],
            check=not allow_missing,
        )
        return result.text if result.returncode == 0 else ""

    def current_branch(self, root: str) -> str:
        result = self.run(
            root,
            ["symbolic-ref", "--quiet", "--short", "HEAD"],
            check=False,
        )
        return result.text if result.returncode == 0 else ""

    def repository(
        self,
        path: str,
        *,
        timeout_s: float | None = None,
    ) -> RepositoryObservation:
        deadline = (
            time.perf_counter() + max(0.05, float(timeout_s))
            if timeout_s is not None else None
        )

        def remaining() -> float | None:
            if deadline is None:
                return None
            value = deadline - time.perf_counter()
            if value <= 0:
                raise GitCommandError(
                    "Git repository discovery timed out",
                    arguments=("rev-parse",),
                    returncode=-1,
                )
            return value

        candidate = os.path.realpath(os.path.abspath(str(path or ".")))
        if os.path.isfile(candidate):
            candidate = os.path.dirname(candidate)
        root = os.path.realpath(self.run(
            candidate,
            ["rev-parse", "--show-toplevel"],
            timeout_s=remaining(),
        ).text)
        git_dir = os.path.realpath(self.run(
            root,
            ["rev-parse", "--absolute-git-dir"],
            timeout_s=remaining(),
        ).text)
        common_raw = self.run(
            root,
            ["rev-parse", "--git-common-dir"],
            timeout_s=remaining(),
        ).text
        common_dir = os.path.realpath(
            common_raw if os.path.isabs(common_raw) else os.path.join(root, common_raw)
        )
        object_format_result = self.run(
            root,
            ["rev-parse", "--show-object-format"],
            check=False,
            timeout_s=remaining(),
        )
        object_format = (
            object_format_result.text
            if object_format_result.returncode == 0 else "sha1"
        )
        head_result = self.run(
            root,
            ["rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"],
            check=False,
            timeout_s=remaining(),
        )
        head_oid = head_result.text if head_result.returncode == 0 else ""
        branch_result = self.run(
            root,
            ["symbolic-ref", "--quiet", "--short", "HEAD"],
            check=False,
            timeout_s=remaining(),
        )
        branch = branch_result.text if branch_result.returncode == 0 else ""

        default_branch = ""
        remote_head = self.run(
            root,
            ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
            check=False,
            timeout_s=remaining(),
        )
        if remote_head.returncode == 0 and "/" in remote_head.text:
            default_branch = remote_head.text.split("/", 1)[1]
        if not default_branch:
            for candidate_name in ("main", "master"):
                if self.run(
                    root,
                    ["show-ref", "--verify", "--quiet", f"refs/heads/{candidate_name}"],
                    check=False,
                    timeout_s=remaining(),
                ).returncode == 0:
                    default_branch = candidate_name
                    break
        default_branch = default_branch or branch

        remote_bytes = self.run(
            root,
            ["remote", "-v"],
            check=False,
            timeout_s=remaining(),
        ).stdout
        remotes_by_key: dict[tuple[str, str], dict[str, str]] = {}
        for line in remote_bytes.splitlines():
            fields = line.split(b"\t", 1)
            if len(fields) != 2:
                continue
            name = _decode(fields[0])
            suffix = _decode(fields[1])
            if suffix.endswith(" (fetch)"):
                url, kind = suffix[:-8], "fetch"
            elif suffix.endswith(" (push)"):
                url, kind = suffix[:-7], "push"
            else:
                url, kind = suffix, "unknown"
            remotes_by_key[(name, kind)] = {"name": name, "kind": kind, "url": url}
        remotes = tuple(
            remotes_by_key[key]
            for key in sorted(remotes_by_key, key=lambda item: (item[0], item[1]))
        )
        return RepositoryObservation(
            root=root,
            git_dir=git_dir,
            common_dir=common_dir,
            object_format=object_format,
            default_branch=default_branch,
            head_oid=head_oid,
            branch=branch,
            remotes=remotes,
        )

    @staticmethod
    def parse_status(raw: bytes) -> tuple[dict[str, str], tuple[StatusEntry, ...]]:
        if raw and not raw.endswith(b"\0"):
            raise GitCommandError("truncated porcelain v2 status stream")
        tokens = raw.split(b"\0")
        headers: dict[str, str] = {}
        entries: list[StatusEntry] = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            index += 1
            if not token:
                continue
            text = _decode(token)
            if text.startswith("# "):
                key, _, value = text[2:].partition(" ")
                headers[key] = value
                continue
            if text.startswith("1 "):
                fields = text.split(" ", 8)
                if len(fields) != 9:
                    raise GitCommandError("invalid porcelain v2 ordinary record")
                xy = fields[1]
                entries.append(StatusEntry(
                    path=_normal_path(fields[8]),
                    record_type="ordinary",
                    index_status=xy[:1] or ".",
                    worktree_status=xy[1:2] or ".",
                    submodule=fields[2],
                    head_mode=fields[3],
                    index_mode=fields[4],
                    worktree_mode=fields[5],
                    head_oid=fields[6],
                    index_oid=fields[7],
                ))
                continue
            if text.startswith("2 "):
                fields = text.split(" ", 9)
                if len(fields) != 10 or index >= len(tokens):
                    raise GitCommandError("invalid porcelain v2 rename/copy record")
                xy = fields[1]
                original = _decode(tokens[index])
                index += 1
                entries.append(StatusEntry(
                    path=_normal_path(fields[9]),
                    record_type="renamed" if xy[:1] == "R" or xy[1:2] == "R" else "copied",
                    index_status=xy[:1] or ".",
                    worktree_status=xy[1:2] or ".",
                    original_path=_normal_path(original),
                    submodule=fields[2],
                    head_mode=fields[3],
                    index_mode=fields[4],
                    worktree_mode=fields[5],
                    head_oid=fields[6],
                    index_oid=fields[7],
                ))
                continue
            if text.startswith("u "):
                fields = text.split(" ", 10)
                if len(fields) != 11:
                    raise GitCommandError("invalid porcelain v2 unmerged record")
                xy = fields[1]
                entries.append(StatusEntry(
                    path=_normal_path(fields[10]),
                    record_type="unmerged",
                    index_status=xy[:1] or "U",
                    worktree_status=xy[1:2] or "U",
                    submodule=fields[2],
                    head_mode=fields[3],
                    index_mode=fields[4],
                    worktree_mode=fields[6],
                    head_oid=fields[7],
                    index_oid=fields[8],
                ))
                continue
            if text.startswith("? "):
                entries.append(StatusEntry(
                    path=_normal_path(text[2:]),
                    record_type="untracked",
                    index_status="?",
                    worktree_status="?",
                ))
                continue
            if text.startswith("! "):
                entries.append(StatusEntry(
                    path=_normal_path(text[2:]),
                    record_type="ignored",
                    index_status="!",
                    worktree_status="!",
                ))
                continue
            raise GitCommandError("unknown porcelain v2 status record")
        return headers, tuple(entries)

    def status(
        self,
        root: str,
        *,
        repository_id: str = "",
        include_untracked: bool = True,
        include_ignored: bool = False,
        fingerprint_content: bool = True,
        timeout_s: float | None = None,
    ) -> StatusSnapshot:
        deadline = (
            time.perf_counter() + max(0.05, float(timeout_s))
            if timeout_s is not None else None
        )

        def remaining() -> float | None:
            if deadline is None:
                return None
            value = deadline - time.perf_counter()
            if value <= 0:
                raise GitCommandError(
                    "Git status observation timed out",
                    arguments=("status",),
                    returncode=-1,
                )
            return value

        args = ["status", "--porcelain=v2", "--branch", "-z"]
        args.append("--untracked-files=all" if include_untracked else "--untracked-files=no")
        if include_ignored:
            args.append("--ignored=matching")
        raw = self.run(root, args, timeout_s=remaining()).stdout
        headers, entries = self.parse_status(raw)
        ahead = behind = 0
        ab = headers.get("branch.ab", "")
        if ab:
            for part in ab.split():
                if part.startswith("+"):
                    ahead = int(part[1:] or 0)
                elif part.startswith("-"):
                    behind = int(part[1:] or 0)
        branch_head = headers.get("branch.head", "")
        branch = "" if branch_head == "(detached)" else branch_head
        head_oid = headers.get("branch.oid", "")
        if head_oid == "(initial)":
            head_oid = ""
        fingerprint_material = bytearray(
            head_oid.encode("ascii", errors="ignore") + b"\0status\0" + raw
        )
        if fingerprint_content:
            # Porcelain records describe *that* a tracked path changed, not its
            # exact bytes.  Include binary patches for both index and worktree
            # and stream every untracked file so a review cannot stay current
            # when content changes under the same status shape.
            for label, diff_args in (
                (b"index", ["diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv"]),
                (b"worktree", ["diff", "--binary", "--no-ext-diff", "--no-textconv"]),
            ):
                payload = self.run(root, diff_args, timeout_s=remaining()).stdout
                fingerprint_material.extend(b"\0" + label + b"\0")
                fingerprint_material.extend(hashlib.sha256(payload).digest())
            root_real = os.path.realpath(os.path.abspath(root))
            admitted = 0
            for entry in entries:
                if entry.record_type != "untracked":
                    continue
                remaining()
                absolute = os.path.abspath(
                    os.path.join(root_real, entry.path.replace("/", os.sep))
                )
                try:
                    if os.path.normcase(os.path.commonpath([root_real, absolute])) != os.path.normcase(root_real):
                        raise GitCommandError("untracked path escapes the repository")
                    info = os.lstat(absolute)
                except (OSError, ValueError) as exc:
                    raise GitCommandError(
                        f"cannot fingerprint untracked path {entry.path!r}: {exc}"
                    ) from exc
                digest = hashlib.sha256()
                if os.path.islink(absolute):
                    digest.update(os.readlink(absolute).encode("utf-8", errors="surrogateescape"))
                elif os.path.isfile(absolute):
                    resolved_file = os.path.realpath(absolute)
                    if os.path.normcase(os.path.commonpath([root_real, resolved_file])) != os.path.normcase(root_real):
                        raise GitCommandError("untracked file resolves outside the repository")
                    admitted += int(info.st_size)
                    if admitted > self.max_output_bytes:
                        raise GitCommandError(
                            "untracked content exceeds the exact fingerprint bound"
                        )
                    with open(absolute, "rb") as handle:
                        while True:
                            remaining()
                            chunk = handle.read(1024 * 1024)
                            if not chunk:
                                break
                            digest.update(chunk)
                else:
                    raise GitCommandError(
                        f"unsupported untracked filesystem object: {entry.path!r}"
                    )
                fingerprint_material.extend(
                    b"\0untracked\0"
                    + entry.path.encode("utf-8", errors="surrogateescape")
                    + b"\0"
                    + digest.digest()
                )
        fingerprint = hashlib.sha256(bytes(fingerprint_material)).hexdigest()
        return StatusSnapshot(
            repository_id=str(repository_id or ""),
            root=os.path.realpath(root),
            head_oid=head_oid,
            branch=branch,
            upstream=headers.get("branch.upstream", ""),
            ahead=ahead,
            behind=behind,
            entries=entries,
            fingerprint=fingerprint,
            observed_at=time.time(),
        )

    def branches(self, root: str, *, include_remote: bool = True) -> tuple[BranchRecord, ...]:
        format_value = (
            "%(refname)%00%(refname:short)%00%(objectname)%00%(HEAD)%00"
            "%(upstream:short)%00%(upstream:track)%00%(subject)%00"
        )
        prefixes = ["refs/heads"]
        if include_remote:
            prefixes.append("refs/remotes")
        raw = self.run(
            root,
            ["for-each-ref", f"--format={format_value}", "--sort=refname", *prefixes],
        ).stdout
        fields = raw.replace(b"\r\n", b"\n").split(b"\0")
        rows: list[BranchRecord] = []
        cursor = 0
        while cursor + 6 < len(fields):
            full_name = _decode(fields[cursor]).lstrip("\n")
            if not full_name:
                break
            short = _decode(fields[cursor + 1])
            oid = _decode(fields[cursor + 2])
            current = _decode(fields[cursor + 3]).strip() == "*"
            upstream = _decode(fields[cursor + 4])
            track = _decode(fields[cursor + 5])
            subject = _decode(fields[cursor + 6])
            rows.append(BranchRecord(
                name=short,
                full_name=full_name,
                oid=oid,
                current=current,
                remote=full_name.startswith("refs/remotes/"),
                upstream=upstream,
                upstream_track=track,
                subject=subject,
            ))
            cursor += 7
        return tuple(rows)

    def commits(self, root: str, *, ref: str = "HEAD", limit: int = 30) -> tuple[CommitRecord, ...]:
        bounded = max(1, min(int(limit), 500))
        oid = self.resolve(root, str(ref or "HEAD"))
        format_value = "%x1e%H%x00%P%x00%an%x00%ae%x00%at%x00%ct%x00%s"
        raw = self.run(
            root,
            ["log", "--no-decorate", f"--max-count={bounded}", f"--format={format_value}", oid],
        ).stdout.replace(b"\r\n", b"\n")
        rows: list[CommitRecord] = []
        for record in raw.split(b"\x1e"):
            record = record.strip(b"\n")
            if not record:
                continue
            fields = record.split(b"\0", 6)
            if len(fields) != 7:
                raise GitCommandError("invalid machine-readable Git log record")
            rows.append(CommitRecord(
                oid=_decode(fields[0]),
                parents=tuple(item for item in _decode(fields[1]).split(" ") if item),
                author_name=_decode(fields[2]),
                author_email=_decode(fields[3]),
                authored_at=int(_decode(fields[4]) or 0),
                committed_at=int(_decode(fields[5]) or 0),
                subject=_decode(fields[6]),
            ))
        return tuple(rows)

    @staticmethod
    def _parse_raw_diff(raw: bytes) -> list[dict[str, Any]]:
        if raw and not raw.endswith(b"\0"):
            raise GitCommandError("truncated --raw -z diff stream")
        tokens = raw.split(b"\0")
        output: list[dict[str, Any]] = []
        cursor = 0
        while cursor < len(tokens):
            metadata = tokens[cursor]
            cursor += 1
            if not metadata:
                continue
            if not metadata.startswith(b":") or cursor >= len(tokens):
                raise GitCommandError("invalid --raw -z diff record")
            parts = metadata[1:].split(b" ")
            if len(parts) != 5:
                raise GitCommandError("invalid --raw -z diff metadata")
            status_token = _decode(parts[4])
            kind = status_token[:1]
            score_text = status_token[1:]
            first_path = _normal_path(_decode(tokens[cursor]))
            cursor += 1
            original_path = ""
            path = first_path
            if kind in {"R", "C"}:
                if cursor >= len(tokens):
                    raise GitCommandError("truncated rename/copy diff record")
                original_path = first_path
                path = _normal_path(_decode(tokens[cursor]))
                cursor += 1
            output.append({
                "path": path,
                "original_path": original_path,
                "status": kind,
                "score": int(score_text or 0),
                "old_mode": _decode(parts[0]),
                "new_mode": _decode(parts[1]),
                "old_oid": _decode(parts[2]),
                "new_oid": _decode(parts[3]),
            })
        return output

    @staticmethod
    def _parse_numstat(raw: bytes) -> dict[str, tuple[int | None, int | None, bool]]:
        if raw and not raw.endswith(b"\0"):
            raise GitCommandError("truncated --numstat -z stream")
        tokens = raw.split(b"\0")
        output: dict[str, tuple[int | None, int | None, bool]] = {}
        cursor = 0
        while cursor < len(tokens):
            token = tokens[cursor]
            cursor += 1
            if not token:
                continue
            fields = token.split(b"\t", 2)
            if len(fields) != 3:
                raise GitCommandError("invalid --numstat -z record")
            add_raw, delete_raw, path_raw = fields
            path = _normal_path(_decode(path_raw))
            if not path:
                # Rename/copy numstat has an empty path followed by old/new.
                if cursor + 1 >= len(tokens):
                    raise GitCommandError("truncated rename numstat record")
                cursor += 1  # old path
                path = _normal_path(_decode(tokens[cursor]))
                cursor += 1
            binary = add_raw == b"-" or delete_raw == b"-"
            output[path] = (
                None if binary else int(add_raw or 0),
                None if binary else int(delete_raw or 0),
                binary,
            )
        return output

    def diff(
        self,
        root: str,
        selector: Sequence[str],
        *,
        paths: Sequence[str] | None = None,
        context: int = 3,
    ) -> DiffObservation:
        suffix = ["--"] + [str(item) for item in (paths or ())]
        shared = ["--no-ext-diff", "--no-textconv", "--find-renames", "--find-copies"]
        raw = self.run(
            root,
            ["diff", "--raw", "-z", "--no-abbrev", "--full-index", *shared, *selector, *suffix],
        ).stdout
        numstat = self.run(
            root,
            ["diff", "--numstat", "-z", *shared, *selector, *suffix],
        ).stdout
        patch = self.run(
            root,
            [
                "diff", "--binary", f"--unified={max(0, min(int(context), 100))}",
                "--src-prefix=a/", "--dst-prefix=b/", *shared, *selector, *suffix,
            ],
        ).stdout
        counts = self._parse_numstat(numstat)
        files = []
        for item in self._parse_raw_diff(raw):
            additions, deletions, binary = counts.get(item["path"], (0, 0, False))
            files.append(DiffFile(
                **item,
                additions=additions,
                deletions=deletions,
                binary=binary,
            ))
        return DiffObservation(tuple(files), patch)

    def worktrees(self, root: str) -> tuple[WorktreeObservation, ...]:
        raw = self.run(root, ["worktree", "list", "--porcelain", "-z"]).stdout
        if raw and not raw.endswith(b"\0"):
            raise GitCommandError("truncated worktree porcelain stream")
        tokens = raw.split(b"\0")
        rows: list[WorktreeObservation] = []
        current: dict[str, str | bool] = {}
        for token in tokens:
            if not token:
                if current.get("worktree"):
                    rows.append(WorktreeObservation(
                        root=os.path.realpath(str(current.get("worktree") or "")),
                        head_oid=str(current.get("HEAD") or ""),
                        branch_ref=str(current.get("branch") or ""),
                        detached=bool(current.get("detached")),
                        bare=bool(current.get("bare")),
                        locked=str(current.get("locked") or ""),
                        prunable=str(current.get("prunable") or ""),
                    ))
                    current = {}
                continue
            text = _decode(token)
            key, separator, value = text.partition(" ")
            current[key] = value if separator else True
        if current.get("worktree"):
            rows.append(WorktreeObservation(
                root=os.path.realpath(str(current.get("worktree") or "")),
                head_oid=str(current.get("HEAD") or ""),
                branch_ref=str(current.get("branch") or ""),
                detached=bool(current.get("detached")),
                bare=bool(current.get("bare")),
                locked=str(current.get("locked") or ""),
                prunable=str(current.get("prunable") or ""),
            ))
        return tuple(rows)

    def merge_base(self, root: str, left: str, right: str = "HEAD") -> str:
        return self.run(root, ["merge-base", str(left), str(right)]).text

    def is_ancestor(self, root: str, ancestor: str, descendant: str) -> bool:
        result = self.run(
            root, ["merge-base", "--is-ancestor", str(ancestor), str(descendant)], check=False
        )
        if result.returncode not in {0, 1}:
            diagnostic = _decode(result.stderr)[:_MAX_DIAGNOSTIC_CHARS].strip()
            raise GitCommandError(
                diagnostic or "Git ancestry check failed",
                arguments=result.arguments,
                returncode=result.returncode,
                stderr=diagnostic,
            )
        return result.returncode == 0

    def unique_commit_count(self, root: str, base_oid: str, head: str = "HEAD") -> int:
        return int(self.run(root, ["rev-list", "--count", str(head), "--not", str(base_oid), "--"]).text or 0)


__all__ = [
    "DiffObservation",
    "GitProcess",
    "GitResult",
    "RepositoryObservation",
    "WorktreeObservation",
]
