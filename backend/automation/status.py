"""Terminal status projection shared by automation execution and history."""
def terminal_status(value):
    status = str(value or "").strip().lower()
    status = {"succeeded": "ok", "completed": "ok", "failed": "error", "canceled": "cancelled"}.get(status, status)
    return status if status in {"ok", "error", "skipped", "interrupted", "cancelled"} else "error"
