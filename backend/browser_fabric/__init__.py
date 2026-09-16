"""VARIANT-1 durable multi-adapter Browser Fabric."""

from . import models as _models

from .adapters import (
    AdapterDownload,
    AdapterObservation,
    AdapterResult,
    AdapterTarget,
    BrowserAdapter,
    EmbeddedBrowserAdapter,
    ManagedPlaywrightAdapter,
)
from .handles import (
    element_handle_envelope,
    page_handle_envelope,
    session_handle_envelope,
    trace_handle_envelope,
)
from .models import *  # noqa: F401,F403 - public record/handle/error contracts
from .access import (
    bind_browser_fabric,
    current_browser_fabric,
    current_browser_host,
    install_browser_fabric,
)
from .binding import (
    BROWSER_BINDING_SCHEMA,
    BrowserBinding,
    CURRENT_BROWSER_BINDING,
    bind_browser_binding,
    browser_binding_snapshot,
    close_browser_binding,
    create_child_browser_binding_snapshot,
    current_browser_binding,
    ensure_browser_binding,
)
from .service import BrowserFabric, create_browser_fabric
from .store import BrowserFabricStore, default_browser_fabric_path, default_profile_root


__all__ = [
    "bind_browser_fabric",
    "bind_browser_binding",
    "browser_binding_snapshot",
    "close_browser_binding",
    "create_child_browser_binding_snapshot",
    "current_browser_binding",
    "current_browser_fabric",
    "current_browser_host",
    "install_browser_fabric",
    "AdapterDownload",
    "AdapterObservation",
    "AdapterResult",
    "AdapterTarget",
    "BrowserAdapter",
    "BrowserBinding",
    "BrowserFabric",
    "BrowserFabricStore",
    "BROWSER_BINDING_SCHEMA",
    "CURRENT_BROWSER_BINDING",
    "EmbeddedBrowserAdapter",
    "ManagedPlaywrightAdapter",
    "create_browser_fabric",
    "default_browser_fabric_path",
    "default_profile_root",
    "element_handle_envelope",
    "ensure_browser_binding",
    "page_handle_envelope",
    "session_handle_envelope",
    "trace_handle_envelope",
] + list(_models.__all__)
