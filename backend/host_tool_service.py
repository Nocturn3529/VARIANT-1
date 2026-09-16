"""Concrete tool-surface runtime service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import host_status

if TYPE_CHECKING:
    from app_host import AppHost


@dataclass
class ToolSurfaceService:
    host: "AppHost"

    def state(self) -> dict:
        return host_status.tools_state(self.host)
