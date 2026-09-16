"""Tool registry + surface construction for the composition root.

Builds the tool registry/config, messaging gateway, and desktop tool
registration. Model-facing discovery is bound later from the complete enabled
registry.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from messaging_gateway import credentials as messaging_credentials
import tools
from messaging_gateway import MessagingGateway

@dataclass(frozen=True)
class ToolSurface:
    registry: Any
    tools_cfg: Any
    messaging_credentials: Any
    gateway: Any
    desktop_control: Any


def build_tool_surface(
    *,
    app_root: str,
    data_dir: str,
    config_dir: str,
    desktop_control: Any,
    router: Any = None,
    token_getter: Callable[[str], Awaitable[str]] | None = None,
) -> ToolSurface:
    """Construct registry, config, gateway, and register base tools.

    ``token_getter`` is used by the messaging gateway. When omitted, a default
    getter that reads the encrypted messaging credential store is used.
    """
    tools_path = (
        os.environ.get("VARIANT1_TOOLS_CONFIG")
        or os.path.join(config_dir, "tools.json")
    )
    tools_cfg = tools.ToolsConfig(tools_path)
    registry = tools.ToolRegistry()

    async def configured_web_search(args: dict):
        from tools_web import web_search

        return await web_search(args, config=tools_cfg.web_search, router=router)

    tools.register_builtins(
        registry,
        web_search_handler=configured_web_search,
    )

    credential_store = messaging_credentials.MessagingCredentialStore(
        os.path.join(config_dir, "messaging_credentials.json"),
        legacy_connector_path=os.path.join(config_dir, "connector_auth.json"),
    )

    async def _default_token(adapter: str) -> str:
        return credential_store.token(adapter)

    getter = token_getter or _default_token
    messaging_config_path = (
        os.environ.get("VARIANT1_MESSAGING_CONFIG")
        or os.path.join(config_dir, "messaging.json")
    )
    gateway = MessagingGateway(
        messaging_config_path,
        token_getter=getter,
        credential_status_getter=credential_store.configured,
        credential_required_getter=credential_store.supports,
        credential_fields_getter=credential_store.credentials,
        attachment_root=os.path.join(data_dir, "attachments", "messaging"),
    )

    # Desktop control (App Control, Phase A): perception + action tools.
    desktop_control.register(registry)
    return ToolSurface(
        registry=registry,
        tools_cfg=tools_cfg,
        messaging_credentials=credential_store,
        gateway=gateway,
        desktop_control=desktop_control,
    )
