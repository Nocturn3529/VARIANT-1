"""Test-only helpers for temporarily patching concrete runtime services."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from unittest.mock import patch


@contextmanager
def patch_host_runtime(host, **domains):
    runtime = host.require_runtime()
    with ExitStack() as stack:
        for domain, changes in domains.items():
            service = getattr(runtime, domain)
            for name, replacement in changes.items():
                stack.enter_context(patch.object(service, name, replacement))
        yield runtime
