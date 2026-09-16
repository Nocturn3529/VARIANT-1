"""Provider-neutral prompt-cache identity for one durable VARIANT-1 chat.

The identity belongs to VARIANT-1's model request, not to any one provider.  The
router resolves it once and carries it through local/cloud routing and retries.
Wire adapters may then project the opaque key through a declaratively supported
request field or header.  Providers without an explicit cache-key control still
receive the same identity in VARIANT-1's request receipt while using their native
prefix-cache behaviour.

Raw chat, conversation, branch, thread, and run identifiers never leave this
module.  The key is a versioned SHA-256 derivative, so request manifests and
provider-visible cache controls cannot disclose local durable identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any, Mapping


_KEY_PREFIX = "variant1-pc-v1-"
_BODY_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


@dataclass(frozen=True, slots=True)
class PromptCacheIdentity:
    """Opaque stable cache owner derived from one VARIANT-1 runtime scope."""

    key: str
    scope: str
    source: str

    def receipt(
        self,
        *,
        application: str,
        native_key_sent: bool,
        cache_enabled: bool | None = None,
    ) -> dict[str, Any]:
        row: dict[str, Any] = {
            "identity_available": True,
            "key_id": self.key,
            "scope": self.scope,
            "source": self.source,
            "application": str(application or "identity_only")[:80],
            "native_key_sent": bool(native_key_sent),
        }
        if cache_enabled is not None:
            row["cache_enabled"] = bool(cache_enabled)
        return row


def _opaque_identity(scope: str, source: str, value: str) -> PromptCacheIdentity:
    canonical = f"variant1.prompt-cache.v1\0{scope}\0{value}".encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return PromptCacheIdentity(
        key=f"{_KEY_PREFIX}{digest[:40]}",
        scope=str(scope or "request")[:40],
        source=str(source or "unknown")[:80],
    )


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _scope_value(scope: Any, name: str) -> str:
    if scope is None:
        return ""
    if isinstance(scope, Mapping):
        return _clean(scope.get(name))
    return _clean(getattr(scope, name, ""))


def resolve_prompt_cache_identity(
    explicit: str | PromptCacheIdentity | None = None,
    *,
    context: Any = None,
) -> PromptCacheIdentity | None:
    """Resolve the most durable available owner for the current model call.

    Durable runtime-chat identity wins over window/session/run identity.  The
    latter values are compatibility fallbacks for background and direct model
    calls that do not belong to a persisted chat.
    """

    if isinstance(explicit, PromptCacheIdentity):
        return explicit
    value = _clean(explicit)
    if value:
        return _opaque_identity("explicit", "explicit", value)

    if context is None:
        try:
            from run_context import current_run_context

            context = current_run_context()
        except Exception:
            context = None
    if context is None:
        return None

    metadata = getattr(context, "metadata", None)
    metadata = metadata if isinstance(metadata, Mapping) else {}
    work_scope = getattr(context, "work_scope", None)
    chat_session = getattr(context, "chat_session", None)

    candidates: tuple[tuple[str, str, str], ...] = (
        ("durable_chat", "work_scope.chat_id", _scope_value(work_scope, "chat_id")),
        ("durable_chat", "metadata.chat_id", _clean(metadata.get("chat_id"))),
        (
            "durable_chat",
            "chat_session.active.turn_session_id",
            _clean(getattr(getattr(chat_session, "active", None), "turn_session_id", "")),
        ),
        (
            "durable_chat",
            "chat_session.turn_session_id",
            _clean(getattr(chat_session, "turn_session_id", "")),
        ),
        (
            "durable_chat",
            "chat_session.viewed_session_id",
            _clean(getattr(chat_session, "viewed_session_id", "")),
        ),
        ("session", "run_context.session_id", _clean(getattr(context, "session_id", ""))),
    )
    for scope, source, candidate in candidates:
        if candidate:
            return _opaque_identity(scope, source, candidate)

    conversation_id = (
        _scope_value(work_scope, "conversation_id")
        or _clean(metadata.get("conversation_id"))
    )
    branch_id = (
        _scope_value(work_scope, "branch_id")
        or _clean(metadata.get("branch_id"))
    )
    if conversation_id:
        return _opaque_identity(
            "conversation_branch",
            "work_scope.conversation_branch",
            f"{conversation_id}\0{branch_id}",
        )

    thread_id = _clean(getattr(context, "thread_id", ""))
    if thread_id:
        return _opaque_identity("thread", "run_context.thread_id", thread_id)
    run_id = _clean(getattr(context, "run_id", ""))
    if run_id:
        return _opaque_identity("run", "run_context.run_id", run_id)
    return None


def apply_prompt_cache_identity(
    identity: PromptCacheIdentity | None,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    body_field: str = "",
    header_name: str = "",
    fallback_application: str = "provider_prefix_cache",
    cache_enabled: bool | None = None,
) -> dict[str, Any]:
    """Project an identity through declarative wire controls and make a receipt.

    Unknown request fields are never invented.  A profile must declare a native
    top-level body field or header; otherwise the adapter records that the
    provider/runtime is relying on prefix caching.
    """

    if identity is None:
        row: dict[str, Any] = {
            "identity_available": False,
            "key_id": None,
            "scope": None,
            "source": None,
            "application": "unavailable",
            "native_key_sent": False,
        }
        if cache_enabled is not None:
            row["cache_enabled"] = bool(cache_enabled)
        return row

    clean_body = str(body_field or "").strip()
    clean_header = str(header_name or "").strip()
    if clean_body:
        if payload is None or _BODY_FIELD.fullmatch(clean_body) is None:
            raise ValueError("invalid prompt-cache request field declaration")
    if clean_header:
        if headers is None or _HEADER_NAME.fullmatch(clean_header) is None:
            raise ValueError("invalid prompt-cache header declaration")
    applications = []
    if clean_body:
        payload.setdefault(clean_body, identity.key)
        applications.append(f"body.{clean_body}")
    if clean_header:
        headers.setdefault(clean_header, identity.key)
        applications.append(f"header.{clean_header.casefold()}")
    return identity.receipt(
        application='+'.join(applications) or fallback_application,
        native_key_sent=bool(applications),
        cache_enabled=cache_enabled,
    )


__all__ = [
    "PromptCacheIdentity",
    "apply_prompt_cache_identity",
    "resolve_prompt_cache_identity",
]
