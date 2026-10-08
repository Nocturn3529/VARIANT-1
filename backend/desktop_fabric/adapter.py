"""Shared live-desktop adapter types; production uses ``CuaDesktopAdapter``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from .models import (
    AppRecord,
    DesktopElement,
    WindowRecord,
)


@dataclass(frozen=True, slots=True)
class AdapterObservation:
    uia: tuple[dict[str, Any], ...] = ()
    visual: tuple[dict[str, Any], ...] = ()
    ocr: tuple[dict[str, Any], ...] = ()
    uia_generation: int = 0
    completeness: str = "uia-only"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdapterCapture:
    png: bytes
    width: int
    height: int
    provenance: str
    occlusion_independent: bool = False
    minimized: bool = False
    stale: bool = False
    coordinate_transform: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdapterDispatch:
    delivered: bool
    method: str
    readback: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdapterFocus:
    result: Any
    hwnd: int
    observation: AdapterObservation


@runtime_checkable
class DesktopLiveAdapter(Protocol):
    def catalog(self, *, backend_instance_id: str) -> tuple[list[AppRecord], list[WindowRecord]]: ...
    def validate_window(self, window: WindowRecord) -> Mapping[str, Any]: ...
    async def observe(self, window: WindowRecord, *, mode: str) -> AdapterObservation: ...
    async def capture(self, window: WindowRecord) -> AdapterCapture: ...
    async def preflight(
        self, window: WindowRecord, *, action: str, delivery: str,
        arguments: Mapping[str, Any],
    ) -> None: ...
    async def dispatch(
        self, window: WindowRecord, *, action: str, delivery: str,
        element: DesktopElement | None, arguments: Mapping[str, Any],
    ) -> AdapterDispatch: ...
    async def focus(self, window: WindowRecord) -> AdapterFocus: ...
    async def focus_session(self, arguments: Mapping[str, Any]) -> AdapterFocus: ...
    def capability_report(self) -> Mapping[str, Any]: ...
    def close(self) -> None: ...


__all__ = [
    "AdapterCapture", "AdapterDispatch", "AdapterFocus", "AdapterObservation",
    "DesktopLiveAdapter",
]
