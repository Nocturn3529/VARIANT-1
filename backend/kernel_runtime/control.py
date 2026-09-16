"""One host-control implementation shared by mounted kernel APIs and UI."""

from __future__ import annotations

from typing import Any



def manager(host: Any) -> Any:
    return host.require_runtime().kernel


def status(host: Any, chat_id: str) -> dict[str, Any]:
    return manager(host).status(str(chat_id))


def history(
    host: Any, chat_id: str, *, after_sequence: int = 0, limit: int = 100
) -> dict[str, Any]:
    return manager(host).execution_history(
        str(chat_id),
        after_sequence=max(0, int(after_sequence)),
        limit=max(1, min(int(limit), 500)),
    )


async def namespace(
    host: Any, chat_id: str, *, limit: int = 100
) -> dict[str, Any]:
    return await manager(host).bounded_namespace_view(
        str(chat_id), limit=max(1, min(int(limit), 500))
    )


def export_notebook(
    host: Any, chat_id: str, *, after_sequence: int = 0, limit: int = 200
) -> dict[str, Any]:
    return manager(host).export_notebook(
        str(chat_id),
        after_sequence=max(0, int(after_sequence)),
        limit=max(1, min(int(limit), 500)),
    )


async def interrupt(
    host: Any,
    chat_id: str,
    *,
    intent: str = "stop",
) -> dict[str, Any]:
    return await manager(host).interrupt(str(chat_id), intent=intent)


async def restart(
    host: Any, chat_id: str, *, reason: str
) -> dict[str, Any]:
    # The explanation is not a checkpoint-policy boundary. Internal lifecycle
    # callers (including a cold catalog rebase) still select their own boundary.
    result = await manager(host).restart(str(chat_id), reason="operator_restart")
    return {**result, "requested_reason": str(reason or "kernel_restart")[:512]}


__all__ = [
    "export_notebook",
    "history",
    "interrupt",
    "manager",
    "namespace",
    "restart",
    "status",
]
