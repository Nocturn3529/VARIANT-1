"""Compatibility name for VARIANT-1's shared process-tree owner.

The CPython worker starts behind its bootstrap ownership gate. Resource limits
are explicit opt-ins; ownership and descendant cleanup always apply. The Win32 implementation lives once in
``process_tree`` and is shared with model, speech, and extension workers.
"""

from __future__ import annotations

from process_tree import OwnedProcessTree


class KernelJobObject(OwnedProcessTree):
    """Own exactly one CPython generation and descendants; limits are opt-in."""


__all__ = ["KernelJobObject"]
