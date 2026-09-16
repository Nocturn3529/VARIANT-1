"""Concrete memory runtime boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

import host_memory_ops
import host_tool_surface

if TYPE_CHECKING:
    from app_host import AppHost


@dataclass
class MemoryService:
    host: "AppHost"
    store: Any

    def profile_items(self) -> list:
        return host_memory_ops.profile_items(self.host)

    async def consolidate_once(self) -> int:
        return await host_tool_surface.consolidate_memory_once(self.host)
