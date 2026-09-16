"""Run-scoped identity carried across Work Fabric operations.

``WorkScope`` is deliberately small and immutable.  It contains host-owned
identifiers only; live handles, clients, and mutable domain state belong in
their respective services.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
import re
from typing import Any, Iterable, Iterator, Mapping


_MAX_ID_CHARS = 512
SCOPE_TEXT_FIELDS = (
    "chat_id", "conversation_id", "branch_id", "workspace_id", "goal_id",
    "goal_run_id", "step_id", "worktree_id", "catalog_release_id",
)
SCOPE_INT_FIELDS = ("workspace_revision", "attempt", "kernel_generation")
_SQL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


def _identifier(value: Any, field: str) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) > _MAX_ID_CHARS:
        raise ValueError(f"work scope {field} exceeds {_MAX_ID_CHARS} characters")
    if "\x00" in text:
        raise ValueError(f"work scope {field} contains a NUL character")
    return text


def _nonnegative_int(value: Any, field: str) -> int:
    if value in (None, ""):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"work scope {field} must be an integer") from exc
    if number < 0:
        raise ValueError(f"work scope {field} cannot be negative")
    return number


@dataclass(frozen=True)
class WorkScope:
    """Bounded correlation identity for one unit of product work."""

    chat_id: str = ""
    conversation_id: str = ""
    branch_id: str = ""
    workspace_id: str = ""
    workspace_revision: int = 0
    goal_id: str = ""
    goal_run_id: str = ""
    step_id: str = ""
    attempt: int = 0
    worktree_id: str = ""
    kernel_generation: int = 0
    catalog_release_id: str = ""

    def __post_init__(self) -> None:
        for field in (
            "chat_id",
            "conversation_id",
            "branch_id",
            "workspace_id",
            "goal_id",
            "goal_run_id",
            "step_id",
            "worktree_id",
            "catalog_release_id",
        ):
            object.__setattr__(self, field, _identifier(getattr(self, field), field))
        for field in ("workspace_revision", "attempt", "kernel_generation"):
            object.__setattr__(
                self,
                field,
                _nonnegative_int(getattr(self, field), field),
            )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "WorkScope":
        """Coerce a mapping, including an envelope containing ``scope``."""

        if value is None:
            return EMPTY_WORK_SCOPE
        raw: Mapping[str, Any] = value
        nested = raw.get("scope")
        if isinstance(nested, Mapping):
            raw = nested
        return cls(
            chat_id=_identifier(raw.get("chat_id"), "chat_id"),
            conversation_id=_identifier(
                raw.get("conversation_id"), "conversation_id"
            ),
            branch_id=_identifier(raw.get("branch_id"), "branch_id"),
            workspace_id=_identifier(raw.get("workspace_id"), "workspace_id"),
            workspace_revision=_nonnegative_int(
                raw.get("workspace_revision"), "workspace_revision"
            ),
            goal_id=_identifier(raw.get("goal_id"), "goal_id"),
            goal_run_id=_identifier(raw.get("goal_run_id"), "goal_run_id"),
            step_id=_identifier(raw.get("step_id"), "step_id"),
            attempt=_nonnegative_int(raw.get("attempt"), "attempt"),
            worktree_id=_identifier(raw.get("worktree_id"), "worktree_id"),
            kernel_generation=_nonnegative_int(
                raw.get("kernel_generation"), "kernel_generation"
            ),
            catalog_release_id=_identifier(
                raw.get("catalog_release_id"), "catalog_release_id"
            ),
        )

    def to_dict(self, *, include_empty: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "chat_id": self.chat_id,
            "conversation_id": self.conversation_id,
            "branch_id": self.branch_id,
            "workspace_id": self.workspace_id,
            "workspace_revision": int(self.workspace_revision),
            "goal_id": self.goal_id,
            "goal_run_id": self.goal_run_id,
            "step_id": self.step_id,
            "attempt": int(self.attempt),
            "worktree_id": self.worktree_id,
            "kernel_generation": int(self.kernel_generation),
            "catalog_release_id": self.catalog_release_id,
        }
        if include_empty:
            return data
        return {key: value for key, value in data.items() if value not in ("", 0)}

    @property
    def empty(self) -> bool:
        return not bool(self.to_dict(include_empty=False))

    def with_updates(self, **updates: Any) -> "WorkScope":
        unknown = set(updates).difference(self.__dataclass_fields__)
        if unknown:
            raise TypeError(f"unknown WorkScope field(s): {', '.join(sorted(unknown))}")
        return replace(self, **updates)


EMPTY_WORK_SCOPE = WorkScope()


def coerce_work_scope(value: Any = None) -> WorkScope:
    """Return ``value`` as a WorkScope without accepting arbitrary objects."""

    if value is None:
        return EMPTY_WORK_SCOPE
    if isinstance(value, WorkScope):
        return value
    if isinstance(value, Mapping):
        return WorkScope.from_mapping(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        mapped = to_dict()
        if isinstance(mapped, Mapping):
            return WorkScope.from_mapping(mapped)
    raise TypeError(f"cannot coerce {type(value).__name__} to WorkScope")


def effective_work_scope(context: Any) -> WorkScope:
    """Project one admitted invocation onto its complete effective scope."""

    scope = coerce_work_scope(context.work_scope)
    if context.chat_id and not scope.chat_id:
        scope = scope.with_updates(chat_id=context.chat_id)
    return scope


def scoped_owner_id(
    owner_kind: Any,
    owner_id: Any,
    scope: WorkScope | Mapping[str, Any] | None,
) -> str:
    """Resolve the shared chat/run owner identity for checkpoint bindings."""

    current = str(owner_id or "")
    if str(owner_kind or "").casefold() != "chat":
        return current
    resolved = coerce_work_scope(scope)
    return (
        resolved.branch_id
        or resolved.chat_id
        or resolved.conversation_id
        or current
    )


def work_scope_visible(
    record_scope: WorkScope | Mapping[str, Any] | None,
    requested_scope: WorkScope | Mapping[str, Any] | None,
) -> bool:
    """Return whether a scoped record is visible from an exact caller scope.

    Empty fields on a durable record are intentionally unpinned.  Every field
    the record *does* pin must match; transports must not invent smaller
    chat/workspace-only variants of this rule.
    """

    record = coerce_work_scope(record_scope)
    requested = coerce_work_scope(requested_scope)
    for field, expected in record.to_dict().items():
        if expected not in ("", 0) and expected != getattr(requested, field):
            return False
    return True


def append_json_scope_visibility(
    clauses: list[str],
    params: list[Any],
    column: str,
    requested_scope: WorkScope | Mapping[str, Any] | None,
    *,
    omit_fields: Iterable[str] = (),
) -> None:
    """Append the one exact WorkScope predicate for a JSON SQLite column."""

    if requested_scope is None:
        return
    if not _SQL_IDENTIFIER.fullmatch(str(column)):
        raise ValueError(f"invalid internal scope column: {column!r}")
    scope = coerce_work_scope(requested_scope)
    omitted = frozenset(str(field) for field in omit_fields)
    for field in SCOPE_TEXT_FIELDS:
        if field in omitted:
            continue
        path = f"$.{field}"
        clauses.append(
            f"(COALESCE(json_extract({column}, ?),'')='' "
            f"OR COALESCE(json_extract({column}, ?),'')=?)"
        )
        params.extend((path, path, str(getattr(scope, field) or "")))
    for field in SCOPE_INT_FIELDS:
        if field in omitted:
            continue
        path = f"$.{field}"
        clauses.append(
            f"(COALESCE(json_extract({column}, ?),0)=0 "
            f"OR CAST(COALESCE(json_extract({column}, ?),0) AS INTEGER)=?)"
        )
        params.extend((path, path, int(getattr(scope, field) or 0)))


def append_column_scope_visibility(
    clauses: list[str],
    params: list[Any],
    requested_scope: WorkScope | Mapping[str, Any] | None,
    *,
    alias: str = "",
    omit_fields: Iterable[str] = (),
) -> None:
    """Append the same predicate for repositories with normalized scope columns."""

    if requested_scope is None:
        return
    if alias and not _SQL_IDENTIFIER.fullmatch(str(alias)):
        raise ValueError(f"invalid internal scope alias: {alias!r}")
    scope = coerce_work_scope(requested_scope)
    omitted = frozenset(str(field) for field in omit_fields)
    prefix = f"{alias}." if alias else ""
    for field in SCOPE_TEXT_FIELDS:
        if field in omitted:
            continue
        clauses.append(f"({prefix}{field}='' OR {prefix}{field}=?)")
        params.append(str(getattr(scope, field) or ""))
    for field in SCOPE_INT_FIELDS:
        if field in omitted:
            continue
        clauses.append(f"({prefix}{field}=0 OR {prefix}{field}=?)")
        params.append(int(getattr(scope, field) or 0))


_CURRENT_WORK_SCOPE: ContextVar[WorkScope] = ContextVar(
    "variant1_work_scope",
    default=EMPTY_WORK_SCOPE,
)


def current_work_scope() -> WorkScope:
    """Return the scope bound to the current async/context-local execution."""

    return _CURRENT_WORK_SCOPE.get()


def replace_current_work_scope(
    scope: WorkScope | Mapping[str, Any] | None,
) -> WorkScope:
    """Replace the scope inside an already-bound run after exact resolution."""

    resolved = coerce_work_scope(scope)
    _CURRENT_WORK_SCOPE.set(resolved)
    return resolved


@contextmanager
def bind_work_scope(scope: WorkScope | Mapping[str, Any] | None) -> Iterator[WorkScope]:
    """Temporarily bind a scope and reliably restore the previous binding."""

    resolved = coerce_work_scope(scope)
    token = _CURRENT_WORK_SCOPE.set(resolved)
    try:
        yield resolved
    finally:
        _CURRENT_WORK_SCOPE.reset(token)


__all__ = [
    "EMPTY_WORK_SCOPE",
    "SCOPE_INT_FIELDS",
    "SCOPE_TEXT_FIELDS",
    "WorkScope",
    "append_column_scope_visibility",
    "append_json_scope_visibility",
    "bind_work_scope",
    "coerce_work_scope",
    "current_work_scope",
    "effective_work_scope",
    "replace_current_work_scope",
    "scoped_owner_id",
    "work_scope_visible",
]
