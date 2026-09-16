"""Public Grok adapter imports; implementation uses the shared peer bridge."""
from .grok_install import GrokPeerError, grok_executable
from .grok_runtime import GrokIntegration, get_grok_integration

__all__ = ["GrokPeerError", "grok_executable", "GrokIntegration", "get_grok_integration"]
