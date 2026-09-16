"""
Durable scheduled-agent and webhook-trigger definitions for VARIANT-1.

Turns VARIANT-1 proactive: a task is a saved prompt with a trigger. The scheduler
runs due tasks; a webhook lets an external event fire one. Each run goes through
the agent headlessly and the reply is delivered as a proactive message.

Storage: config/automations.json -> {"tasks": [ {task}, ... ]}.

Task shape:
  {
    "id": "<short id>",
    "name": "Morning focus",
    "enabled": true,
    "prompt": "Give me one calm, concrete focus for the day.",
    "trigger": {"type": "daily", "time": "08:00"}        # or:
               {"type": "interval", "seconds": 3600}      # every hour
               {"type": "weekly", "day": "mon", "time": "09:00"}
               {"type": "webhook", "token": "<random>"}   # fired by POST /hook/<token>
    "durable_checkpoints": true,                           # default; set false for ephemeral
    "last_run": 0.0
  }

is_due() is a pure function so it's easy to unit-test.
"""

from __future__ import annotations

import json
import os
import secrets
import copy
from contextlib import suppress
import threading
import time
from datetime import datetime, timedelta
from durable_document import DocumentLoadError, load_document

try:
    from croniter import croniter
    HAS_CRONITER = True
except Exception:                       # optional dependency — cron triggers degrade
    croniter = None
    HAS_CRONITER = False

_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MISFIRE_POLICIES = {"latest", "skip"}


def execution_task_snapshot(task: dict) -> dict:
    """Freeze execution settings without mutable scheduling bookkeeping."""
    return copy.deepcopy({key: task[key] for key in (
        "id", "name", "prompt", "trigger", "enabled", "durable_checkpoints",
        "misfire_policy", "model_route", "cancellation_epoch",
    ) if key in task})


def cancellation_epoch(task: dict | None) -> int:
    try:
        return max(0, int((task or {}).get("cancellation_epoch") or 0))
    except (TypeError, ValueError):
        return 0


class AutomationPersistenceError(RuntimeError):
    pass


def _discard_pending_claims(task: dict, *, status: str) -> bool:
    """Cancel accepted-but-unfinished occurrences owned by a disabled task.

    Keeping ``last_run`` at the accepted scheduled occurrence is deliberate:
    re-enabling a definition must not replay work that was explicitly discarded
    while the definition was disabled.
    """

    changed = False
    if list(task.get("trigger_claims") or ()):
        task.pop("trigger_claims", None)
        task["last_trigger_status"] = str(status or "cancelled")[:40]
        changed = True
    if isinstance(task.get("scheduled_claim"), dict):
        task.pop("scheduled_claim", None)
        task["last_schedule_status"] = str(status or "cancelled")[:40]
        changed = True
    return changed


def _replace_with_retry(source: str, destination: str) -> None:
    """Settle brief Windows sharing/AV races without weakening atomic replace."""

    for attempt in range(4):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt >= 3:
                raise
            time.sleep(0.02 * (2 ** attempt))


def schedule_error(trigger: dict | None) -> str:
    """Return a user-visible reason a saved schedule cannot be evaluated."""
    trigger = trigger or {}
    if trigger.get("type") != "cron":
        return ""
    expr = str(trigger.get("expr") or "").strip()
    if not expr:
        return "Cron expression is empty."
    if not HAS_CRONITER:
        return "Cron scheduling is unavailable because croniter is not installed."
    try:
        croniter(expr, datetime.now()).get_next(datetime)
    except Exception as exc:
        return f"Invalid cron expression: {exc}"
    return ""


def _cron_due(expr: str, last: float, now_ts: float, *, catch_up: bool = True) -> bool:
    """True if a cron-expression trigger has a scheduled time at/before now that we
    haven't run since. Using "most recent fire <= now > last_run" gives free
    CATCH-UP: if the app was off when a fire time passed, the next tick still runs
    it once (rather than silently skipping it). Needs the optional `croniter`."""
    if not HAS_CRONITER or not (expr or "").strip():
        return False
    try:
        prev = croniter(expr, datetime.fromtimestamp(now_ts)).get_prev(datetime)
        if not catch_up and now_ts - prev.timestamp() > 60:
            return False
        return prev.timestamp() > float(last or 0)
    except Exception:
        return False


def _parse_hhmm(s: str):
    try:
        hh, mm = str(s).split(":")
        return max(0, min(23, int(hh))), max(0, min(59, int(mm)))
    except Exception:
        return 9, 0


def _misfire_policy(task: dict) -> str:
    value = str(task.get("misfire_policy") or "latest").strip().lower()
    return value if value in MISFIRE_POLICIES else "latest"


def _latest_daily(now: datetime, hh: int, mm: int) -> datetime:
    occurrence = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return occurrence if occurrence <= now else occurrence - timedelta(days=1)


def _latest_weekday(now: datetime, hh: int, mm: int) -> datetime:
    occurrence = _latest_daily(now, hh, mm)
    while occurrence.weekday() >= 5:
        occurrence -= timedelta(days=1)
    return occurrence


def is_due(task: dict, now_ts: float) -> bool:
    """Whether a scheduled task should run now. Webhook tasks are never due here."""
    if not task.get("enabled"):
        return False
    tr = task.get("trigger", {}) or {}
    ttype = tr.get("type")
    last = float(task.get("last_run") or 0)

    if ttype == "interval":
        sec = max(60, int(tr.get("seconds", 3600)))
        return (now_ts - last) >= sec

    if ttype == "cron":
        return _cron_due(tr.get("expr", ""), last, now_ts,
                         catch_up=_misfire_policy(task) == "latest")

    now = datetime.fromtimestamp(now_ts)
    if ttype == "daily":
        hh, mm = _parse_hhmm(tr.get("time", "09:00"))
        target = (_latest_daily(now, hh, mm) if _misfire_policy(task) == "latest"
                  else now.replace(hour=hh, minute=mm, second=0, microsecond=0))
        return now >= target and last < target.timestamp()

    if ttype == "weekdays":
        hh, mm = _parse_hhmm(tr.get("time", "09:00"))
        if _misfire_policy(task) == "latest":
            target = _latest_weekday(now, hh, mm)
        else:
            if now.weekday() >= 5:
                return False
            target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return now >= target and last < target.timestamp()

    if ttype == "weekly":
        day = str(tr.get("day", "mon")).lower()[:3]
        if day not in _DAYS:
            return False
        hh, mm = _parse_hhmm(tr.get("time", "09:00"))
        # Most recent occurrence of (target weekday at HH:MM) at/before now. Using
        # that (rather than requiring today == target weekday) gives free CATCH-UP:
        # if the app was off all of the target day, the next tick still runs it
        # once, like the cron path -- instead of silently skipping the whole week.
        days_since = (now.weekday() - _DAYS.index(day)) % 7
        occ = (now - timedelta(days=days_since)).replace(
            hour=hh, minute=mm, second=0, microsecond=0)
        if occ > now:                       # today is the day but before the time
            occ -= timedelta(days=7)
        if _misfire_policy(task) == "skip":
            if now.weekday() != _DAYS.index(day) or occ.date() != now.date():
                return False
        return now >= occ and last < occ.timestamp()

    return False  # webhook or unknown -> not time-triggered


def describe_trigger(tr: dict) -> str:
    t = (tr or {}).get("type")
    if t == "interval":
        s = int(tr.get("seconds", 3600))
        return f"every {s // 3600}h" if s >= 3600 and s % 3600 == 0 else f"every {max(1, s // 60)} min"
    if t == "daily":
        return f"daily at {tr.get('time', '09:00')}"
    if t == "weekdays":
        return f"weekdays at {tr.get('time', '09:00')}"
    if t == "weekly":
        return f"{tr.get('day', 'mon')} at {tr.get('time', '09:00')}"
    if t == "cron":
        return f"cron: {tr.get('expr', '')}".rstrip()
    if t == "webhook":
        return "on webhook"
    return "unknown"


def durable_checkpoints_enabled(task: dict | None) -> bool:
    """Durable native snapshots are on by default for automations.

    Existing automations that do not have the field become durable automatically.
    Set ``durable_checkpoints: false`` on a saved automation to keep that run
    ephemeral.
    """
    if not isinstance(task, dict):
        return True
    return task.get("durable_checkpoints", True) is not False


class AutomationStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self.tasks = []
        self._load_failure = None
        self.load()

    def load(self):
        with self._lock:
            try:
                d = load_document(self.path, {"tasks": []}, valid=lambda value: all(
                    isinstance(task, dict) for task in value.get("tasks", [])
                ))
            except DocumentLoadError as exc:
                self._load_failure = exc
                raise AutomationPersistenceError(str(exc)) from exc
            self.tasks = d.get("tasks", [])
            self._load_failure = None

    def _write_tasks(self, tasks: list[dict]) -> None:
        if self._load_failure is not None:
            raise AutomationPersistenceError("Reload the automation document successfully before saving") from self._load_failure
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"tasks": tasks}, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            _replace_with_retry(tmp, self.path)
        except Exception as e:
            with suppress(OSError):
                os.remove(tmp)
            raise AutomationPersistenceError(
                f"automation store could not be persisted: {e}"
            ) from e

    def _replace_tasks(self, tasks: list[dict]) -> None:
        with self._lock:
            snapshot = copy.deepcopy(list(tasks))
            self._write_tasks(snapshot)
            self.tasks = snapshot

    def save(self):
        with self._lock:
            self._write_tasks(copy.deepcopy(self.tasks))

    def list(self):
        out = []
        with self._lock:
            tasks = copy.deepcopy(self.tasks)
        for t in tasks:
            trigger = t.get("trigger", {}) or {}
            error = schedule_error(trigger)
            out.append({
                "id": t.get("id"),
                "name": t.get("name", "(unnamed)"),
                "enabled": bool(t.get("enabled")),
                "prompt": t.get("prompt", ""),
                "trigger": trigger,
                "schedule": describe_trigger(trigger),
                "schedule_error": error,
                "schedule_runnable": bool(t.get("enabled")) and not error,
                "last_run": t.get("last_run", 0),
                "durable_checkpoints": durable_checkpoints_enabled(t),
                "model_route": copy.deepcopy(t.get("model_route") or {}),
                "misfire_policy": _misfire_policy(t),
                "trigger_claims": len(list(t.get("trigger_claims") or ())),
                "scheduled_claim": copy.deepcopy(t.get("scheduled_claim")),
                "webhook_token": (t.get("trigger", {}) or {}).get("token", "")
                                 if (t.get("trigger", {}) or {}).get("type") == "webhook" else "",
            })
        return out

    def get(self, tid: str):
        with self._lock:
            for t in self.tasks:
                if t.get("id") == tid:
                    return copy.deepcopy(t)
        return None

    def get_by_webhook(self, token: str):
        if not token:
            return None
        with self._lock:
            for t in self.tasks:
                tr = t.get("trigger", {}) or {}
                if tr.get("type") == "webhook" and tr.get("token") == token and t.get("enabled"):
                    return copy.deepcopy(t)
        return None

    def add(self, name: str, prompt: str, trigger: dict, enabled: bool = True, *,
            misfire_policy: str = "latest", model_route: dict | None = None) -> dict:
        trigger = dict(trigger or {})
        if trigger.get("type") == "webhook" and not trigger.get("token"):
            trigger["token"] = secrets.token_hex(8)
        task = {"id": secrets.token_hex(6), "name": name or "(unnamed)",
                "enabled": bool(enabled), "prompt": prompt or "",
                "trigger": trigger, "durable_checkpoints": True,
                "misfire_policy": (misfire_policy if misfire_policy in MISFIRE_POLICIES
                                   else "latest"),
                "last_run": 0, "model_route": copy.deepcopy(model_route or {}),
                "cancellation_epoch": 0}
        with self._lock:
            next_tasks = copy.deepcopy(self.tasks)
            next_tasks.append(task)
            self._replace_tasks(next_tasks)
        return copy.deepcopy(task)

    def update(self, tid: str, fields: dict) -> bool:
        with self._lock:
            next_tasks = copy.deepcopy(self.tasks)
            t = next((row for row in next_tasks if row.get("id") == tid), None)
            if not t:
                return False
            for k in ("name", "prompt", "enabled", "trigger", "durable_checkpoints",
                      "misfire_policy", "model_route"):
                if k in fields:
                    if k == "durable_checkpoints":
                        t[k] = bool(fields[k])
                    elif k == "enabled":
                        requested = bool(fields[k])
                        if not requested:
                            t["cancellation_epoch"] = cancellation_epoch(t) + 1
                        t[k] = requested
                    elif k == "misfire_policy":
                        value = str(fields[k] or "").strip().lower()
                        t[k] = value if value in MISFIRE_POLICIES else "latest"
                    else:
                        t[k] = fields[k]
            if not t.get("enabled"):
                _discard_pending_claims(t, status="cancelled_disabled")
            self._replace_tasks(next_tasks)
            return True

    def pin_model_route(self, tid: str, route: dict) -> dict:
        """One-time upgrade for saved definitions that predate route capture."""
        with self._lock:
            tasks = copy.deepcopy(self.tasks)
            task = next((row for row in tasks if row.get("id") == tid), None)
            if task is None:
                return copy.deepcopy(route)
            if not task.get("model_route"):
                task["model_route"] = copy.deepcopy(route)
                self._replace_tasks(tasks)
            return copy.deepcopy(task["model_route"])

    def remove(self, tid: str) -> bool:
        with self._lock:
            next_tasks = [
                copy.deepcopy(t) for t in self.tasks if t.get("id") != tid
            ]
            if len(next_tasks) == len(self.tasks):
                return False
            self._replace_tasks(next_tasks)
            return True

    def admit_trigger(
        self,
        tid: str,
        *,
        payload: str = "",
        source: str = "trigger",
        claim_id: str = "",
    ) -> dict:
        """Durably admit one externally acknowledged trigger occurrence."""

        with self._lock:
            next_tasks = copy.deepcopy(self.tasks)
            task = next((row for row in next_tasks if row.get("id") == tid), None)
            if task is None:
                raise LookupError(f"unknown automation: {tid}")
            if not task.get("enabled"):
                raise RuntimeError("automation is disabled")
            claims = [
                dict(item) for item in list(task.get("trigger_claims") or ())
                if isinstance(item, dict)
            ]
            identity = str(claim_id or "").strip() or (
                f"trigger:{tid}:{secrets.token_hex(12)}"
            )
            prior = next(
                (item for item in claims if str(item.get("claim_id") or "") == identity),
                None,
            )
            if prior is not None:
                if (
                    str(prior.get("payload") or "") != str(payload or "")[:8000]
                    or str(prior.get("source") or "") != str(source or "trigger")[:40]
                ):
                    raise RuntimeError("automation trigger claim identity conflict")
                return copy.deepcopy(prior)
            if len(claims) >= 100:
                raise RuntimeError("automation has too many uncompleted trigger claims")
            claim = {
                "claim_id": identity,
                "payload": str(payload or "")[:8000],
                "source": str(source or "trigger")[:40],
                "claimed_at": time.time(),
                "status": "claimed",
                "task_snapshot": execution_task_snapshot(task),
            }
            claims.append(claim)
            task["trigger_claims"] = claims
            self._replace_tasks(next_tasks)
            return copy.deepcopy(claim)

    def task_for_claim(self, tid: str, claim_id: str) -> dict | None:
        """Return the occurrence's accepted settings, including legacy claims."""
        with self._lock:
            task = next((row for row in self.tasks if row.get("id") == tid), None)
            if task is None:
                return None
            claims = [task.get("scheduled_claim"), *list(task.get("trigger_claims") or ())]
            claim = next((item for item in claims if isinstance(item, dict)
                          and str(item.get("claim_id") or "") == claim_id), None)
            if claim is None:
                return None
            return execution_task_snapshot(claim.get("task_snapshot") or task)

    def claim_is_active(
        self, tid: str, claim_id: str, *, claim_kind: str = "",
    ) -> bool:
        """Whether an accepted background occurrence is still executable.

        Claims are the durable eligibility record.  This deliberately does
        more than checking the definition's current ``enabled`` bit: a claim
        retired by disable must stay retired if the definition is later
        re-enabled while a stale recovery copy is still in memory.
        """

        identity = str(claim_id or "").strip()
        kind = str(claim_kind or "").strip().lower()
        if not identity:
            return False
        with self._lock:
            task = next((row for row in self.tasks if row.get("id") == tid), None)
            if task is None or not task.get("enabled"):
                return False
            if kind in {"", "scheduled"}:
                scheduled = task.get("scheduled_claim")
                if (
                    isinstance(scheduled, dict)
                    and str(scheduled.get("claim_id") or "") == identity
                ):
                    return True
            if kind in {"", "trigger"}:
                return any(
                    isinstance(claim, dict)
                    and str(claim.get("claim_id") or "") == identity
                    for claim in list(task.get("trigger_claims") or ())
                )
            return False

    def complete_trigger_claim(
        self, tid: str, claim_id: str, *, status: str,
    ) -> bool:
        with self._lock:
            next_tasks = copy.deepcopy(self.tasks)
            task = next((row for row in next_tasks if row.get("id") == tid), None)
            if task is None:
                return False
            claims = [
                dict(item) for item in list(task.get("trigger_claims") or ())
                if isinstance(item, dict)
            ]
            kept = [
                item for item in claims
                if str(item.get("claim_id") or "") != str(claim_id or "")
            ]
            if len(kept) == len(claims):
                return False
            if kept:
                task["trigger_claims"] = kept
            else:
                task.pop("trigger_claims", None)
            task["last_trigger_status"] = str(status or "completed")[:40]
            self._replace_tasks(next_tasks)
            return True

    def recover_trigger_claims(self) -> list[dict]:
        with self._lock:
            next_tasks = copy.deepcopy(self.tasks)
            rows = []
            changed = False
            for task in next_tasks:
                if not task.get("enabled"):
                    changed = (
                        _discard_pending_claims(
                            task, status="cancelled_disabled",
                        )
                        or changed
                    )
                    continue
                for claim in list(task.get("trigger_claims") or ()):
                    if not isinstance(claim, dict) or not claim.get("claim_id"):
                        continue
                    rows.append({
                        "claim_id": str(claim["claim_id"]),
                        "payload": str(claim.get("payload") or ""),
                        "source": str(claim.get("source") or "trigger"),
                        "task": copy.deepcopy(task),
                    })
            if changed:
                self._replace_tasks(next_tasks)
            return rows

    def claim_due_tasks(self, now_ts: float = None) -> list[dict]:
        """Persist occurrence consumption before the scheduler launches work."""

        now_ts = time.time() if now_ts is None else float(now_ts)
        with self._lock:
            next_tasks = copy.deepcopy(self.tasks)
            claims: list[dict] = []
            for task in next_tasks:
                # An accepted occurrence remains the owner until the runner
                # records its terminal disposition. A later interval must not
                # overwrite the only restart-recovery pointer.
                if isinstance(task.get("scheduled_claim"), dict):
                    continue
                if not is_due(task, now_ts):
                    continue
                task_id = str(task.get("id") or "")
                if not task_id:
                    continue
                claim_id = (
                    f"schedule:{task_id}:"
                    + secrets.token_hex(8)
                )
                previous_last_run = float(task.get("last_run") or 0.0)
                task["last_run"] = now_ts
                task["scheduled_claim"] = {
                    "claim_id": claim_id,
                    "occurrence_at": now_ts,
                    "claimed_at": time.time(),
                    "status": "claimed",
                    "previous_last_run": previous_last_run,
                    "task_snapshot": execution_task_snapshot(task),
                }
                claims.append({
                    "claim_id": claim_id,
                    "task": copy.deepcopy(task),
                })
            if claims:
                self._replace_tasks(next_tasks)
            return claims

    def recover_scheduled_claims(self) -> list[dict]:
        """Return accepted occurrences left unfinished by a prior process."""

        with self._lock:
            next_tasks = copy.deepcopy(self.tasks)
            claims: list[dict] = []
            changed = False
            for task in next_tasks:
                if not task.get("enabled"):
                    changed = (
                        _discard_pending_claims(
                            task, status="cancelled_disabled",
                        )
                        or changed
                    )
                    continue
                claim = task.get("scheduled_claim")
                if not isinstance(claim, dict):
                    continue
                claim_id = str(claim.get("claim_id") or "").strip()
                if not claim_id:
                    continue
                claims.append({
                    "claim_id": claim_id,
                    "task": copy.deepcopy(task),
                    "recovered": True,
                })
            if changed:
                self._replace_tasks(next_tasks)
            return claims

    def complete_scheduled_claim(
        self,
        tid: str,
        claim_id: str,
        *,
        status: str,
    ) -> bool:
        with self._lock:
            next_tasks = copy.deepcopy(self.tasks)
            task = next((row for row in next_tasks if row.get("id") == tid), None)
            if task is None:
                return False
            claim = task.get("scheduled_claim")
            if (
                not isinstance(claim, dict)
                or str(claim.get("claim_id") or "") != str(claim_id or "")
            ):
                return False
            task.pop("scheduled_claim", None)
            task["last_schedule_status"] = str(status or "completed")[:40]
            self._replace_tasks(next_tasks)
            return True

    def release_skipped_scheduled_claim(self, tid: str, claim_id: str) -> bool:
        """Return an unexecuted occurrence to the due scheduler."""

        with self._lock:
            next_tasks = copy.deepcopy(self.tasks)
            task = next((row for row in next_tasks if row.get("id") == tid), None)
            if task is None:
                return False
            claim = task.get("scheduled_claim")
            if (
                not isinstance(claim, dict)
                or str(claim.get("claim_id") or "") != str(claim_id or "")
            ):
                return False
            task["last_run"] = float(claim.get("previous_last_run") or 0.0)
            task.pop("scheduled_claim", None)
            task["last_schedule_status"] = "skipped"
            self._replace_tasks(next_tasks)
            return True
