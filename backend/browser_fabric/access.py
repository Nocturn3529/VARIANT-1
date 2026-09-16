"""Process-wide Browser Fabric access for the original browser seed tools."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator


_INSTALLED_FABRIC: Any = None
_INSTALLED_HOST: Any = None
_CURRENT_FABRIC: ContextVar[Any] = ContextVar("variant1_browser_fabric", default=None)
_CURRENT_HOST: ContextVar[Any] = ContextVar("variant1_browser_fabric_host", default=None)


def install_browser_fabric(fabric: Any, host: Any = None) -> None:
    """Remember the host-composed fabric used by browser_navigate."""
    global _INSTALLED_FABRIC, _INSTALLED_HOST
    _INSTALLED_FABRIC = fabric
    _INSTALLED_HOST = host


def current_browser_fabric() -> Any:
    return _CURRENT_FABRIC.get() or _INSTALLED_FABRIC


def current_browser_host() -> Any:
    return _CURRENT_HOST.get() or _INSTALLED_HOST


@contextmanager
def bind_browser_fabric(fabric: Any, host: Any = None) -> Iterator[Any]:
    """Bind a fabric for the current task, used by tests and nested runs."""
    fabric_token = _CURRENT_FABRIC.set(fabric)
    host_token = _CURRENT_HOST.set(host)
    try:
        yield fabric
    finally:
        _CURRENT_FABRIC.reset(fabric_token)
        _CURRENT_HOST.reset(host_token)
