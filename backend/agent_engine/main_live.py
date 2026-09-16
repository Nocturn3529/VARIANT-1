"""Typed main-chat live bag (non-serializable run objects).

Native snapshots store JSON-safe ``RunState`` only. Non-serializable
objects for the main chat loop (Task and ports) live on
``MainTaskRuntime.live`` (a ``MainChatLive`` instance), created when the graph
is built and filled in prepare.

**Live ``Task`` is sole authority** for task data during the graph. Nodes
mutate ``live.task`` in place. ``RunState["task"]`` is a
checkpoint mirror only — projected via ``project_live_task_bundle`` at prepare,
node-exit durability, and explicit checkpoint/restore boundaries.

**Loop routing** (``route``, ``step``, ``actions``, mood/reply, …) lives only in
``RunState["main"]["loop"]`` — it is not mirrored onto the live bag.

Attribute access only (no MutableMapping dual API).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


MAIN_LIVE_KEY = "_main_chat_live"


@dataclass
class MainChatLive:
    """Non-serializable live state for one main-chat graph run.

    Does **not** hold loop routing crumbs — those belong in RunState only.
    """

    task: Any = None
    loop_ports: Any = None
    img_holder: Any = None
    pending_image: Any = None
    img_token: Any = None
    run: Any = None
    max_out: int = 0
    engaged_groups: Any = field(default_factory=set)
    disclosed: Any = field(default_factory=set)
    checkpoint_ref: Any = None
    context_receipt: Any = None

    def clear(self) -> None:
        """Reset all live handles for a new prepare (same runtime object)."""
        self.task = None
        self.loop_ports = None
        self.img_holder = None
        self.pending_image = None
        self.img_token = None
        self.run = None
        self.max_out = 0
        self.engaged_groups = set()
        self.disclosed = set()
        self.checkpoint_ref = None
        self.context_receipt = None

    def assign(
        self,
        *,
        task: Any = None,
        loop_ports: Any = None,
        img_holder: Any = None,
        img_token: Any = None,
        run: Any = None,
        max_out: int = 0,
        engaged_groups: Any = None,
        disclosed: Any = None,
        checkpoint_ref: Any = None,
        context_receipt: Any = None,
        pending_image: Any = None,
    ) -> None:
        """Bulk-fill after prepare (replaces dict-style update)."""
        self.task = task
        self.loop_ports = loop_ports
        self.img_holder = img_holder
        self.img_token = img_token
        self.run = run
        self.max_out = int(max_out or 0)
        self.engaged_groups = set(engaged_groups or set())
        self.disclosed = set(disclosed or set())
        self.checkpoint_ref = checkpoint_ref
        self.context_receipt = context_receipt
        self.pending_image = pending_image
