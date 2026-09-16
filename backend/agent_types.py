"""Small typed values shared by agent-loop hosts and tool execution."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ToolBatchResult:
    """Call-bound results from one provider tool-call batch."""

    text: str = ""
    outcomes: list[dict] = field(default_factory=list)
    receipts: list[dict] = field(default_factory=list)
    executed: bool = False
    had_error: bool = False
    cancelled: bool = False
    terminate: bool = False
