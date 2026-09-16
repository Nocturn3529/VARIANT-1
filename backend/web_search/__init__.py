"""Configured web-search providers and optional managed SearXNG runtime."""

from . import providers, search
from .searxng import SearxngServer

__all__ = ["SearxngServer", "providers", "search"]
