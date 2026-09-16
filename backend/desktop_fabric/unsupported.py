"""Desktop live-adapter stub for non-Windows hosts.

Win32/UIA remains in ``WindowsDesktopAdapter``. macOS/Linux get an explicit
unsupported driver so fabric composition does not import or call windll/UIA.
"""

from __future__ import annotations

import sys
from typing import Any, Mapping

from .adapter import (
    AdapterCapture,
    AdapterDispatch,
    AdapterFocus,
    AdapterObservation,
)
from .models import (
    AppRecord,
    DesktopElement,
    DesktopUnavailable,
    WindowRecord,
)


def _reason() -> str:
    return (
        "Desktop automation is not supported on "
        f"{sys.platform}: Win32/UIA driver only. "
        "Use Windows, or pass a custom DesktopLiveAdapter."
    )


class UnsupportedDesktopAdapter:
    """Clear no-op seam: every live call raises DesktopUnavailable."""

    def __init__(self, *, platform: str | None = None) -> None:
        self.platform = platform or sys.platform

    def catalog(self, *, backend_instance_id: str) -> tuple[list[AppRecord], list[WindowRecord]]:
        raise DesktopUnavailable(self._reason())

    def validate_window(self, window: WindowRecord) -> Mapping[str, Any]:
        raise DesktopUnavailable(self._reason())

    async def observe(self, window: WindowRecord, *, mode: str) -> AdapterObservation:
        raise DesktopUnavailable(self._reason())

    async def capture(self, window: WindowRecord) -> AdapterCapture:
        raise DesktopUnavailable(self._reason())

    async def preflight(
        self, window: WindowRecord, *, action: str, delivery: str,
        arguments: Mapping[str, Any],
    ) -> None:
        raise DesktopUnavailable(self._reason())

    async def dispatch(
        self, window: WindowRecord, *, action: str, delivery: str,
        element: DesktopElement | None, arguments: Mapping[str, Any],
    ) -> AdapterDispatch:
        raise DesktopUnavailable(self._reason())

    async def focus(self, window: WindowRecord) -> AdapterFocus:
        raise DesktopUnavailable(self._reason())

    async def focus_session(self, arguments: Mapping[str, Any]) -> AdapterFocus:
        raise DesktopUnavailable(self._reason())

    def capability_report(self) -> Mapping[str, Any]:
        return {
            "adapter": "unsupported",
            "platform": self.platform,
            "supported": False,
            "reason": self._reason(),
            "windows_available": False,
            "driver_state": {
                "explicitly_bound_per_window": False,
                "checkpointed": False,
                "process_default": False,
                "run_registry": False,
            },
            "perception": {"uia": False, "screenshot": False, "ocr": False},
            "capture": {"mss_visible_pixels": False},
            "actions": {
                "host_selected_semantic_or_physical": False,
                "physical_input": False,
                "semantic_supported": [],
                "physical_supported": [],
            },
        }

    def close(self) -> None:
        return None


__all__ = ["UnsupportedDesktopAdapter"]
