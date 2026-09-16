"""Pinned IDs for VARIANT-1's single provider-visible persistent-Python surface."""

ACTION_SURFACE = "trusted-local.v1"
IPYTHON_SCHEMA_REVISION = "ipython.portable.v6"
CHAT_GRAPH_REVISION = "chat.ipython.v2"
WORKER_GRAPH_REVISION = "worker.ipython.v2"
DISCLOSURE_PROFILE = "disclosure.topk.v1"
DISCLOSURE_REVISION = "1"
DISCLOSURE_WIDTH = 5

def is_action_surface(value: str) -> bool:
    return str(value or "") == ACTION_SURFACE


def canonical_action_surface(value: str) -> str:
    raw = str(value or "").strip()
    return raw


def canonical_graph_revision(value: str) -> str:
    raw = str(value or "").strip()
    return raw
