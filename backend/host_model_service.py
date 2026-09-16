"""Concrete model, vision, and engine-management runtime service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

import host_flags
import host_status
from model_runtime import engine_manager

if TYPE_CHECKING:
    from app_host import AppHost


@dataclass
class ModelService:
    host: "AppHost"

    def vision_config(self) -> dict:
        return host_flags.vision_cfg_from_router(self.host.router)

    def vision_state(self) -> tuple[bool, str]:
        cfg = self.vision_config()
        route = (
            self.host.router.mode
            if self.host.router.mode in {"local", "cloud"}
            else "local"
        )
        if route == "local":
            capable = bool(
                cfg.get("local_capable", False)
                and getattr(self.host.router.engine, "mmproj", "")
            )
            return (
                capable and self.host.router.engine_ready,
                cfg.get("local_route", "single"),
            )
        return (
            bool(self.host.router.cloud_route_ready(require_vision=True)),
            cfg.get("cloud_route", "text"),
        )

    def config_status(self) -> dict:
        return host_status.config_status_msg(self.host)

    def doctor_snapshot(self) -> dict:
        return host_status.doctor_snapshot(self.host)

    def scan_models(self):
        return engine_manager.scan_models(self.host.data_dir)

    async def restart_engine(
        self,
        model_path,
        mmproj_path="",
        *,
        should_apply: Callable[[], bool] | None = None,
    ):
        result = await engine_manager.restart_engine(
            self.host.router,
            self.host.hub,
            self.host.engine_status_message,
            model_path,
            mmproj_path,
            should_apply=should_apply,
        )
        return result

    async def restart_engine_keep_model(self):
        return await engine_manager.restart_engine_keep_model(
            self.host.router,
            self.host.hub,
            self.host.engine_status_message,
        )

    async def reconcile_local_engine(self):
        return await engine_manager.reconcile_local_engine(self.host.router)
