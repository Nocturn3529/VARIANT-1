"""Concrete process lifecycle runtime service."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import TYPE_CHECKING

from model_runtime.hardware import detect_hardware
import host_orphan
import lifecycle
import memory_tools

if TYPE_CHECKING:
    from app_host import AppHost


@dataclass
class LifecycleService:
    host: "AppHost"

    async def start_critical_services(self) -> None:
        import server_lifespan

        await server_lifespan.start_critical_services(self.host)

    async def start_optional_services(self) -> None:
        import server_lifespan

        await server_lifespan.start_optional_services(self.host)

    async def automation_loop(self) -> None:
        await self.host.require_runtime().workflows.automation_loop()

    async def consolidation_loop(self) -> None:
        await memory_tools.consolidation_loop(
            self.host.require_runtime().memory.consolidate_once)

    def dev_reset_on_launch(self) -> None:
        runtime = self.host.require_runtime()
        catalog = runtime.catalog
        lifecycle.dev_reset_on_launch(runtime.memory.store)
        if os.environ.get("VARIANT1_DEV_RESET", "").strip().lower() in {
            "1", "true", "yes", "on",
        }:
            catalog.rebase_disposable_test_state()

    async def check_orphaned_task(self) -> None:
        await host_orphan.check_orphaned_task(self.host)

    @staticmethod
    def detect_hardware() -> dict:
        return detect_hardware()

    async def start_workers(self) -> list:
        import server_lifespan

        return await server_lifespan.start_workers(self.host)

    async def shutdown(self) -> None:
        import server_lifespan

        await server_lifespan.shutdown(self.host)
