"""
Persisted run history for the automation product.

Storage is a capped JSON file in the writable data dir, fail-soft and atomically
rewritten like the other small stores in this backend.
"""

import json
import os
import tempfile
import threading
import time
from .status import terminal_status

MAX_RUNS = 500
MAX_SUMMARY_CHARS = 300


def _now() -> float:
    return time.time()


def _clip(s: str, n: int = MAX_SUMMARY_CHARS) -> str:
    s = " ".join(str(s or "").split())
    return s[:n] + ("..." if len(s) > n else "")


class AutomationHistoryStore:
    def __init__(self, path: str, max_runs: int = MAX_RUNS):
        self.path = path
        self.max_runs = max_runs
        self._lock = threading.Lock()
        self.runs = []
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            rows = d.get("runs", []) if isinstance(d, dict) else []
            self.runs = [r for r in rows if isinstance(r, dict) and r.get("automation_id")]
            if len(self.runs) > self.max_runs:
                self.runs = self.runs[-self.max_runs:]
        except FileNotFoundError:
            self.runs = []
        except Exception as e:
            print(f"[automation_history] load failed ({e}); empty", flush=True)
            self.runs = []

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"runs": self.runs}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:
            print(f"[automation_history] save failed: {e}", flush=True)

    def add(self, automation_id: str, started_at: float, finished_at: float,
            status: str, summary: str = "") -> dict:
        status = terminal_status(status)
        row = {
            "automation_id": str(automation_id or "") or "unknown",
            "started_at": float(started_at or _now()),
            "finished_at": float(finished_at or _now()),
            "status": status,
            "summary": _clip(summary),
        }
        with self._lock:
            self.runs.append(row)
            if len(self.runs) > self.max_runs:
                self.runs = self.runs[-self.max_runs:]
            self.save()
            return dict(row)

    def list(self, automation_id: str = "", limit: int = 100):
        limit = max(1, min(int(limit or 100), self.max_runs))
        aid = str(automation_id or "")
        out = []
        with self._lock:
            for row in reversed(self.runs):
                if aid and row.get("automation_id") != aid:
                    continue
                out.append(dict(row))
                if len(out) >= limit:
                    break
        return out

    def count(self) -> int:
        with self._lock:
            return len(self.runs)
