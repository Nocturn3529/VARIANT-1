"""Task- or control-scoped Desktop Fabric access for original seed tools."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator


_CURRENT_FABRIC: ContextVar[Any] = ContextVar("variant1_desktop_fabric", default=None)
_CURRENT_HOST: ContextVar[Any] = ContextVar(
    "variant1_desktop_fabric_host", default=None)
_CONTROL_FABRIC_ATTR = "_variant1_desktop_fabric"
_CONTROL_HOST_ATTR = "_variant1_desktop_host"


def _host_control(host: Any, fabric: Any = None) -> Any:
    control = getattr(host, "desktop_control", None) if host is not None else None
    if control is None and fabric is not None:
        control = getattr(getattr(fabric, "adapter", None), "control", None)
    return control


def install_desktop_fabric(fabric: Any, host: Any = None) -> None:
    """Bind one composed Fabric to its own DesktopControl instance.

    Tool registries close over that control, so this avoids process-global host
    state leaking into another registry, test, or restarted host.
    """

    control = _host_control(host, fabric)
    if control is None:
        return
    setattr(control, _CONTROL_FABRIC_ATTR, fabric)
    setattr(control, _CONTROL_HOST_ATTR, host)


def uninstall_desktop_fabric(host: Any, fabric: Any = None) -> None:
    """Remove a control-scoped binding during host teardown."""

    control = _host_control(host, fabric)
    if control is None:
        return
    current = getattr(control, _CONTROL_FABRIC_ATTR, None)
    if fabric is not None and current is not fabric:
        return
    setattr(control, _CONTROL_FABRIC_ATTR, None)
    setattr(control, _CONTROL_HOST_ATTR, None)


def current_desktop_fabric(control: Any = None) -> Any:
    return _CURRENT_FABRIC.get() or getattr(
        control, _CONTROL_FABRIC_ATTR, None
    )


def current_desktop_host(control: Any = None) -> Any:
    return _CURRENT_HOST.get() or getattr(control, _CONTROL_HOST_ATTR, None)


@contextmanager
def bind_desktop_fabric(fabric: Any, host: Any = None) -> Iterator[Any]:
    """Bind a fabric for the current task, used by tests and nested runs."""
    fabric_token = _CURRENT_FABRIC.set(fabric)
    host_token = _CURRENT_HOST.set(host)
    try:
        yield fabric
    finally:
        _CURRENT_FABRIC.reset(fabric_token)
        _CURRENT_HOST.reset(host_token)
