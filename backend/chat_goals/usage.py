"""Charge cumulative provider measurements once per physical request."""
from __future__ import annotations

import threading

from llm_usage import usage_token


def _count(value):
    return usage_token(value) or 0


def _uncached_input(usage):
    if usage.get("uncached_input_tokens") is not None:
        return _count(usage["uncached_input_tokens"])
    if usage.get("input_tokens") is not None:
        return max(0, _count(usage["input_tokens"]) - _count(usage.get("cached_input_tokens")))
    return None


def goal_tokens(usage):
    return (_uncached_input(usage) or 0) + _count(usage.get("output_tokens"))


class GoalRequestUsage:
    """Lives on the HTTP request ref. Partial samples settle even on cancellation."""

    def __init__(self, goal_id, apply):
        self.goal_id = goal_id
        self._apply = apply
        self._counts = {}
        self._charged = 0
        self._final = False
        self._lock = threading.Lock()

    def observe_partial(self, usage):
        with self._lock:
            if not self._final:
                for key in ("uncached_input_tokens", "output_tokens"):
                    if usage.get(key) is not None:
                        self._counts[key] = _count(usage[key])

    def settle(self, usage=None):
        with self._lock:
            if usage is None and self._final:
                return
            if usage is not None:
                reported = usage.get("reported_fields", usage)
                components = (("uncached_input_tokens", "input_tokens", _uncached_input(usage)),
                              ("output_tokens", "output_tokens", usage.get("output_tokens")))
                for key, raw_key, value in components:
                    # Normalization fills absent fields with zero. A sparse
                    # receipt must retain measurements from earlier chunks.
                    if value is not None and (raw_key in reported or key not in self._counts or value):
                        self._counts[key] = _count(value)
            total = goal_tokens(self._counts)
            delta = total - self._charged
            if delta:
                self._apply(self.goal_id, delta)
                self._charged = total
            if usage is not None:
                self._final = True
