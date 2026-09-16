"""Typed protocols for AppHost service boundaries.

Ports builders and domain code should depend on these contracts (or concrete
implementations) rather than the server module bag.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Protocol, runtime_checkable


@runtime_checkable
class RouterPort(Protocol):
    mode: str
    reasoning: bool
    cfg: dict

    def stream(self, messages, **kwargs) -> AsyncIterator[str]: ...
    def wants_local_engine(self) -> bool: ...
    def push_model_route(self, route: dict | None) -> Any: ...
    def reset_model_route(self, token: Any) -> None: ...
    def context_limit_tokens(self, route: dict | None = None) -> int: ...
    def projection_budget_tokens(self, route: dict | None = None) -> int: ...
    def save_config(self) -> None: ...


@runtime_checkable
class ToolRegistryPort(Protocol):
    def get(self, name: str) -> Any: ...
    def specs(self, enabled=None) -> list: ...


@runtime_checkable
class ActivityHubPort(Protocol):
    async def broadcast(self, message: dict) -> None: ...
