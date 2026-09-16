"""Window targeting and checkpoint rehydration helpers."""

from __future__ import annotations

from dataclasses import dataclass
import unicodedata
from typing import Any, Awaitable, Callable, Dict, List

import tools
from . import elevation as delev
from . import errors as derr
from . import perception_recovery as prec
from . import window_context as wctx
from .session import DesktopSessionState


def target_meta(session: DesktopSessionState) -> Dict[str, Any]:
    return dict(session.target_meta or {})


def active_window(session: DesktopSessionState) -> Dict[str, Any]:
    return dict(session.active_window or {})


def hwnd_for(win) -> int:
    if win is None:
        return 0
    try:
        return int(getattr(win, "NativeWindowHandle", 0) or 0)
    except Exception:
        return 0


def sync_target_from_stack(session: DesktopSessionState) -> None:
    cur = session.window_stack.get_current()
    ctrl = session.window_stack.get_current_control()
    if cur is None:
        session.target_window = None
        session.target_meta = {"title": "", "query": ""}
        session.mark_updated()
        return
    session.target_window = ctrl
    session.target_meta = {
        "title": (cur.title or "").strip(),
        "query": (cur.query or cur.title or "").strip(),
    }
    session.mark_updated()


def queue_stack_event(session: DesktopSessionState, event: str, fields: dict) -> None:
    session.pending_stack_events.append((event, fields))
    session.mark_updated()


def set_target(
    session: DesktopSessionState,
    win,
    *,
    title: str = "",
    query: str = "",
    push: bool = False,
) -> wctx.WindowContext:
    ctx = wctx.WindowContext(
        hwnd=hwnd_for(win),
        title=(title or "").strip(),
        query=(query or title or "").strip(),
    )
    if push:
        session.window_stack.push(ctx, ctrl=win)
        queue_stack_event(
            session,
            "perception:window_pushed",
            wctx.pushed_activity_fields(
                ctx,
                depth=session.window_stack.depth(),
                stack_summary=session.window_stack.format_stack(),
            ),
        )
    else:
        session.window_stack.replace(ctx, ctrl=win)
    sync_target_from_stack(session)
    return ctx


def pop_window_context(
    session: DesktopSessionState,
) -> tuple[wctx.WindowContext | None, wctx.WindowContext | None]:
    popped, current = session.window_stack.restore_previous()
    if popped is not None:
        queue_stack_event(
            session,
            "perception:window_popped",
            wctx.popped_activity_fields(
                popped,
                previous=current,
                depth=session.window_stack.depth(),
                stack_summary=session.window_stack.format_stack(),
            ),
        )
    sync_target_from_stack(session)
    return popped, current


def promote_window_context(session: DesktopSessionState, index: int) -> wctx.WindowContext | None:
    ctx = session.window_stack.switch_to(index)
    if ctx is not None:
        queue_stack_event(
            session,
            "perception:window_switched",
            wctx.switched_activity_fields(
                ctx,
                index=index,
                depth=session.window_stack.depth(),
                stack_summary=session.window_stack.format_stack(),
            ),
        )
    sync_target_from_stack(session)
    return ctx


def _identity_from_raw(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        try:
            hwnd = int(raw.get("hwnd") or 0)
        except (TypeError, ValueError):
            hwnd = 0
        return {
            "hwnd": hwnd,
            "query": str(raw.get("query") or ""),
            "title": str(raw.get("title") or ""),
        }
    try:
        hwnd = int(getattr(raw, "hwnd", 0) or 0)
    except (TypeError, ValueError):
        hwnd = 0
    return {
        "hwnd": hwnd,
        "query": str(getattr(raw, "query", "") or ""),
        "title": str(getattr(raw, "title", "") or ""),
    }


def rehydrate_identities(session: DesktopSessionState) -> List[Dict[str, Any]]:
    """Ordered target identities to try during checkpoint restore."""
    raw_items: list[Any] = []
    raw_items.extend(list(reversed(session.window_stack.get_stack())))
    raw_items.append(session.active_window)
    raw_items.append(session.target_meta)
    scope = dict(session.scope or {})
    if scope.get("window_query") or scope.get("app_name"):
        raw_items.append({
            "query": scope.get("window_query") or scope.get("app_name") or "",
            "title": scope.get("window_query") or scope.get("app_name") or "",
        })

    seen: set[tuple[int, str, str]] = set()
    out: List[Dict[str, Any]] = []
    for raw in raw_items:
        ident = _identity_from_raw(raw)
        query = (ident.get("query") or ident.get("title") or "").strip()
        title = (ident.get("title") or query).strip()
        hwnd = int(ident.get("hwnd") or 0)
        if not (hwnd or query or title):
            continue
        key = (hwnd, query.lower(), title.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"hwnd": hwnd, "query": query, "title": title})
    return out


@dataclass
class RehydrateHooks:
    """UIA-specific callbacks supplied by ``desktop_control``."""

    run_uia: Callable[[Callable[[], Any]], Awaitable[Any]]
    load_uia: Callable[[], Any]
    find_window: Callable[[Any, str], tuple[Any, list]]
    find_window_by_hwnd: Callable[[Any, int], Any]
    bind_restored_target: Callable[[Any, str, str], None]
    bring_to_front: Callable[[Any], bool]
    save_state: Callable[[DesktopSessionState], DesktopSessionState]
    emit: Callable[[str], Awaitable[None]]
    emit_fields: Callable[[str, Dict[str, Any]], Awaitable[None]]
    log: Callable[[str], None]


async def rehydrate_bound_session(
    session: DesktopSessionState,
    hooks: RehydrateHooks,
) -> None:
    """Best-effort restore of live target handles from serializable metadata."""
    if session.target_window is not None:
        return
    state = dict(session.rehydrate_state or {})
    if state.get("attempted") and state.get("status") in {"success", "fallback", "partial"}:
        return

    identities = rehydrate_identities(session)
    if not identities:
        session.rehydrate_state = {
            "attempted": True,
            "status": "skipped",
            "reason": "no target metadata",
            "session_id": session.session_id,
        }
        hooks.log(f"[desktop] session {session.session_id}: target rehydrate skipped (no metadata)")
        return

    strict_hwnd = int((session.scope or {}).get("fabric_strict_hwnd") or 0)
    strict_pid = int((session.scope or {}).get("fabric_strict_pid") or 0)

    def _do():
        auto = hooks.load_uia()
        tried = []
        open_titles = []
        for ident in identities:
            hwnd = int(ident.get("hwnd") or 0)
            query = (ident.get("query") or ident.get("title") or "").strip()
            title = (ident.get("title") or query).strip()
            if hwnd:
                tried.append(f"hwnd:{hwnd}")
                win = hooks.find_window_by_hwnd(auto, hwnd)
                if win is not None and strict_pid:
                    try:
                        if int(getattr(win, "ProcessId", 0) or 0) != strict_pid:
                            win = None
                    except Exception:
                        win = None
                if win is not None:
                    name = title or query or f"#{hwnd}"
                    try:
                        name = (getattr(win, "Name", "") or name).strip()
                    except Exception:
                        pass
                    hooks.bind_restored_target(win, name, query or title or name)
                    activated = hooks.bring_to_front(win)
                    return {
                        "ok": True,
                        "mode": "hwnd",
                        "hwnd": hwnd,
                        "query": query,
                        "title": name,
                        "activated": bool(activated),
                        "tried": tried,
                    }
            if strict_hwnd:
                continue
            for q in [query, title]:
                q = (q or "").strip()
                if not q or q in tried:
                    continue
                tried.append(q)
                pick, titles = hooks.find_window(auto, q)
                open_titles = titles or open_titles
                if not pick:
                    continue
                win = pick["ctrl"]
                name = pick["name"] or q
                hooks.bind_restored_target(win, name, q)
                activated = hooks.bring_to_front(win)
                return {
                    "ok": True,
                    "mode": "query",
                    "hwnd": hwnd,
                    "query": q,
                    "title": name,
                    "activated": bool(activated),
                    "tried": tried,
                }
        return {
            "ok": False,
            "query": (identities[0].get("query") or identities[0].get("title") or ""),
            "tried": tried,
            "open_titles": sorted(set(open_titles))[:10],
        }

    try:
        result = await hooks.run_uia(_do)
    except Exception as e:
        session.rehydrate_state = {
            "attempted": True,
            "status": "fallback",
            "reason": f"{type(e).__name__}: {e}",
            "session_id": session.session_id,
        }
        hooks.log(f"[desktop] session {session.session_id}: target rehydrate failed ({e}); falling back")
        await hooks.emit_fields(
            "perception:target_rehydrate_fallback",
            {
                "text": f"Desktop target restore failed; falling back to foreground ({e})",
                "reason": str(e)[:200],
                "restore_status": "fallback",
            },
        )
        return

    if result.get("ok"):
        status = "success"
        if result.get("mode") == "query" and identities and identities[0].get("hwnd"):
            status = "partial"
        session.rehydrate_state = {
            "attempted": True,
            "status": status,
            "mode": result.get("mode", ""),
            "hwnd": int(result.get("hwnd") or 0),
            "query": result.get("query", ""),
            "title": result.get("title", ""),
            "activated": bool(result.get("activated")),
            "session_id": session.session_id,
        }
        hooks.save_state(session)
        hooks.log(
            f"[desktop] session {session.session_id}: rehydrated target "
            f"'{result.get('title')}' via {result.get('mode')} '{result.get('query') or result.get('hwnd')}'"
        )
        event = "perception:target_rehydrated" if status == "success" else "perception:target_rehydrate_partial"
        await hooks.emit_fields(
            event,
            {
                "text": f"Desktop target restored: {result.get('title') or result.get('query')}",
                "title": (result.get("title") or "")[:120],
                "query": (result.get("query") or "")[:120],
                "hwnd": int(result.get("hwnd") or 0),
                "mode": result.get("mode", ""),
                "activated": bool(result.get("activated")),
                "restore_status": status,
            },
        )
        return

    session.rehydrate_state = {
        "attempted": True,
        "status": "fallback",
        "query": result.get("query", ""),
        "tried": list(result.get("tried") or []),
        "open_titles": list(result.get("open_titles") or []),
        "session_id": session.session_id,
    }
    hooks.log(
        f"[desktop] session {session.session_id}: target rehydrate fallback "
        f"(tried={result.get('tried')}, open={result.get('open_titles')})"
    )
    await hooks.emit_fields(
        "perception:target_rehydrate_fallback",
        {
            "text": f"Desktop target not found for restore; falling back to foreground ({result.get('query')})",
            "query": (result.get("query") or "")[:120],
            "open_titles": ", ".join(result.get("open_titles") or [])[:300],
            "restore_status": "fallback",
        },
    )


def locked_target_alive(win) -> tuple[bool, str]:
    """Return (alive, reason_if_dead). MUST run on the UIA worker thread."""
    if win is None:
        return False, "no_handle"
    try:
        if win.Exists(0, 0):
            return True, ""
        return False, "window_closed"
    except Exception:
        return False, "handle_invalid"


def resolve_target(ctx, auto):
    """Return the live locked target, or record focus loss and use foreground."""
    sess = ctx.session
    if sess.target_window is not None:
        alive, reason = locked_target_alive(sess.target_window)
        if alive:
            return sess.target_window
        ctx._record_focus_loss(auto, reason)
        sess.target_window = None
        sess.mark_updated()
    if int((sess.scope or {}).get("fabric_strict_hwnd") or 0):
        return None
    return ctx._target_top(auto)


def _same_window(a, b) -> bool:
    if a is b:
        return True
    ah = hwnd_for(a)
    bh = hwnd_for(b)
    return bool(ah and bh and ah == bh)


def is_foreground_window(auto, win) -> bool:
    """Whether ``win`` is already the active top-level window."""
    try:
        fg = auto.GetForegroundControl()
        fg = fg.GetTopLevelControl() if fg else None
    except Exception:
        return False
    return bool(fg and _same_window(fg, win))


def _same_process(a, b) -> bool:
    try:
        apid = int(getattr(a, "ProcessId", 0) or 0)
        bpid = int(getattr(b, "ProcessId", 0) or 0)
        return bool(apid and bpid and apid == bpid)
    except Exception:
        return False


def _owned_window(child, parent) -> bool:
    """Whether ``child`` is in the Win32 owner chain of ``parent``."""
    child_hwnd = hwnd_for(child)
    parent_hwnd = hwnd_for(parent)
    if not child_hwnd or not parent_hwnd:
        return False
    try:
        import ctypes

        get_window = ctypes.windll.user32.GetWindow
        owner = int(get_window(child_hwnd, 4) or 0)  # GW_OWNER
        seen = set()
        while owner and owner not in seen:
            if owner == parent_hwnd:
                return True
            seen.add(owner)
            owner = int(get_window(owner, 4) or 0)
    except Exception:
        return False
    return False


def require_bound_target(ctx, auto, *, action: str = ""):
    """Return the live locked target, never an arbitrary foreground fallback."""
    sess = ctx.session
    target = sess.target_window
    public_action = (action or "desktop mutation").strip()
    if target is None:
        raise tools.ToolError(
            f"NO_DESKTOP_TARGET: {public_action} was not sent because no exact window is bound."
        )
    alive, reason = locked_target_alive(target)
    if alive:
        return target

    last_title = (
        sess.target_meta.get("title")
        or sess.target_meta.get("query")
        or sess.snapshot_title
        or "(unknown)"
    )
    ctx._record_focus_loss(auto, reason)
    raise tools.ToolError(
        f"DESKTOP_TARGET_LOST: computer.{public_action} was not sent "
        f"because the bound window '{last_title}' is no longer available. "
        "Open or focus the intended window again."
    )


def resolve_action_target(ctx, auto, *, action: str = ""):
    """Resolve the safe target for input, including a bound app's live modal.

    A Save/Open dialog belongs to the locked app and should keep its current
    field focus. An unrelated foreground window is never accepted as an input
    target; callers reactivate the locked app instead.
    """
    locked = require_bound_target(ctx, auto, action=action)
    fg = auto.GetForegroundControl()
    try:
        fg = fg.GetTopLevelControl() if fg else None
    except Exception:
        pass
    if fg is None or _same_window(fg, locked):
        return locked

    try:
        from . import modal_detect as mdetect

        modal = mdetect.scan_modal_uia(
            auto,
            locked,
            ctx.session.target_meta.get("title") or ctx.session.snapshot_title or "",
        )
        related = _same_process(fg, locked) or _owned_window(fg, locked)
        if (
            related
            and modal is not None
            and modal.present
            and modal.confidence in {"high", "medium"}
        ):
            return fg
    except Exception:
        pass
    return locked


def refocus_stack_top(ctx, win, *, title: str, query: str):
    """Re-bind the stack top after re-acquiring a window. UIA worker thread."""
    sess = ctx.session
    sess.window_stack.update_current_control(win, hwnd=hwnd_for(win))
    cur = sess.window_stack.get_current()
    if cur is not None:
        cur.title = (title or "").strip()
        cur.query = (query or title or "").strip()
    sync_target_from_stack(sess)


async def refocus_locked_window(ctx) -> tuple[bool, str]:
    """Restore a saved HWND first; title fallback is legacy-driver only."""
    sess = ctx.session
    cur = sess.window_stack.get_current()
    query = (
        (sess.target_meta.get("query") or sess.target_meta.get("title") or "").strip()
        or (cur.query or cur.title if cur else "")
    )
    if not query:
        return False, "no saved window target is available"

    saved_hwnd = int(
        (sess.scope or {}).get("fabric_strict_hwnd")
        or (cur.hwnd if cur else 0)
        or 0
    )
    saved_pid = int((sess.scope or {}).get("fabric_strict_pid") or 0)
    strict_hwnd = bool((sess.scope or {}).get("fabric_strict_hwnd"))

    def _do():
        auto = ctx._load_uia()
        exact = find_window_by_hwnd(ctx, auto, saved_hwnd) if saved_hwnd else None
        if exact is not None and saved_pid:
            try:
                if int(getattr(exact, "ProcessId", 0) or 0) != saved_pid:
                    exact = None
            except Exception:
                exact = None
        if exact is not None:
            name = _window_name(exact, cur.title if cur else query)
            if sess.window_stack.is_empty():
                ctx.set_target(exact, title=name, query=query)
            else:
                refocus_stack_top(ctx, exact, title=name, query=query)
            activated = bring_to_front(exact)
            note = "" if activated else " (couldn't confirm foreground)"
            return True, f"restored '{name}' by HWND{note}.", name
        if strict_hwnd:
            return False, (
                f"durable window HWND {saved_hwnd} is no longer available"
            ), ""
        pick, titles = find_window(ctx, auto, query)
        if not pick:
            avail = ", ".join(sorted(set(titles))[:10]) or "(none)"
            return False, (
                f"window '{query}' not found (open: {avail}) — the app may "
                "have been closed."
            ), ""
        win = pick["ctrl"]
        name = pick["name"] or query
        if sess.window_stack.is_empty():
            ctx.set_target(win, title=name, query=query)
        else:
            refocus_stack_top(ctx, win, title=name, query=query)
        activated = bring_to_front(win)
        note = "" if activated else " (couldn't confirm foreground)"
        stack_note = ""
        if sess.window_stack.depth() > 1:
            stack_note = f" Stack: {sess.window_stack.format_stack()}."
        return True, f"restored '{name}'{note}.{stack_note}", name

    ok, msg, name = await ctx._uia(_do)
    if ok and name:
        await ctx._emit_perception(
            "perception:focus_restored",
            **prec.focus_restored_activity_fields(title=name, query=query),
        )
    return ok, msg


async def refocus_locked_target(ctx) -> bool:
    """Re-activate the locked target, or re-find it if the lock was lost."""
    if ctx.session.target_window is not None:
        def _do():
            auto = ctx._load_uia()
            tgt = resolve_target(ctx, auto)
            if tgt is None:
                return False
            return bring_to_front(tgt)

        return bool(await ctx._uia(_do))

    ok, _msg = await refocus_locked_window(ctx)
    return ok


def find_window(ctx, auto, query):
    """Best top-level window whose title contains ``query``."""
    def match_text(value: Any) -> str:
        normalized = unicodedata.normalize("NFKC", str(value or ""))
        # UIA titles can contain zero-width direction/format marks and non-ASCII
        # spaces even when logs render them exactly like an ordinary title.
        visible = "".join(
            char for char in normalized
            if unicodedata.category(char) != "Cf"
        )
        return " ".join(visible.split()).casefold()

    q = match_text(query)
    cands = []
    try:
        children = auto.GetRootControl().GetChildren()
    except Exception:
        children = []
    for w in children:
        try:
            name = w.Name or ""
            cn = w.ClassName or ""
            pid = w.ProcessId
            ctype = w.ControlTypeName
            r = w.BoundingRectangle
        except Exception:
            continue
        if ctype not in ("WindowControl", "PaneControl"):
            continue
        if ctx._is_skippable(name, cn, pid):
            continue
        area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
        cands.append({"ctrl": w, "name": name, "area": area})
    titles = [c["name"] for c in cands if c["name"]]
    if not q:
        return None, titles
    matches = [c for c in cands if q in match_text(c["name"])]
    if not matches:
        return None, titles
    matches.sort(
        key=lambda c: (q == match_text(c["name"]), c["area"]),
        reverse=True,
    )
    return matches[0], titles


def find_window_by_hwnd(_ctx, auto, hwnd: int):
    """Resolve an exact HWND, including owned dialogs nested in the UIA tree."""
    try:
        target = int(hwnd or 0)
    except (TypeError, ValueError):
        target = 0
    if not target:
        return None
    # Native dialogs are top-level Win32 windows but can be children of their
    # owner in the UIA tree. Desktop-root enumeration alone misses them.
    # Keep this on the caller's UIA worker and verify the returned identity;
    # the fabric also fences the process identity before/after binding.
    from_handle = getattr(auto, "ControlFromHandle", None)
    if callable(from_handle):
        try:
            control = from_handle(target)
            if hwnd_for(control) == target:
                return control
        except Exception:
            pass
    try:
        children = auto.GetRootControl().GetChildren()
    except Exception:
        children = []
    for win in children:
        try:
            if int(getattr(win, "NativeWindowHandle", 0) or 0) == target:
                return win
        except Exception:
            continue
    return None


def _window_name(win, fallback: str = "") -> str:
    try:
        return (getattr(win, "Name", "") or fallback or "").strip()
    except Exception:
        return (fallback or "").strip()


def _matching_stack_context(
    session: DesktopSessionState,
    query: str,
) -> tuple[int | None, wctx.WindowContext | None]:
    """Return the most recent saved window whose identity matches ``query``."""
    q = (query or "").strip().lower()
    if not q:
        return None, None
    stack = session.window_stack.get_stack()
    for index in range(len(stack) - 1, -1, -1):
        item = stack[index]
        values = {
            (item.title or "").strip().lower(),
            (item.query or "").strip().lower(),
        }
        if any(q in value or value in q for value in values if value):
            return index, item
    return None, None


def bring_to_front(win):
    """Bring a window to the foreground reliably without resizing it."""
    hwnd = hwnd_for(win)
    if hwnd:
        try:
            import ctypes
            import time

            u32 = ctypes.windll.user32
            if int(u32.GetForegroundWindow() or 0) == hwnd:
                return True
            if u32.IsIconic(hwnd):
                u32.ShowWindowAsync(hwnd, 9)
            current_thread = int(ctypes.windll.kernel32.GetCurrentThreadId() or 0)
            foreground = int(u32.GetForegroundWindow() or 0)
            foreground_thread = int(
                u32.GetWindowThreadProcessId(foreground, None) or 0
            ) if foreground else 0
            target_thread = int(u32.GetWindowThreadProcessId(hwnd, None) or 0)
            attached: list[tuple[int, int]] = []
            try:
                for other_thread in (foreground_thread, target_thread):
                    if (
                        current_thread
                        and other_thread
                        and current_thread != other_thread
                        and u32.AttachThreadInput(
                            current_thread, other_thread, True
                        )
                    ):
                        attached.append((current_thread, other_thread))
                u32.ShowWindowAsync(hwnd, 5)
                u32.BringWindowToTop(hwnd)
                u32.SetForegroundWindow(hwnd)
                time.sleep(0.03)
            finally:
                for source_thread, other_thread in reversed(attached):
                    u32.AttachThreadInput(source_thread, other_thread, False)
            if int(u32.GetForegroundWindow() or 0) == hwnd:
                return True
        except Exception:
            pass
    try:
        if win.IsWindowPatternAvailable():
            wp = win.GetWindowPattern()
            if getattr(wp, "CurrentWindowVisualState", None) == 2:
                wp.SetWindowVisualState(0)
    except Exception:
        pass
    for attempt in ("SwitchToThisWindow", "SetActive", "Show"):
        try:
            getattr(win, attempt)()
            if hwnd:
                import ctypes
                import time

                time.sleep(0.03)
                if int(ctypes.windll.user32.GetForegroundWindow() or 0) == hwnd:
                    return True
        except Exception:
            continue
    return False


async def restore_previous_target(ctx, args):
    ctx._ensure_gates()
    if ctx.session.window_stack.depth() < 2:
        raise tools.ToolError("no previous bound window is available")

    def _do():
        auto = ctx._load_uia()
        stack = ctx.session.window_stack.get_stack()
        previous = stack[-2]
        query = (previous.query or previous.title or "").strip()
        win = find_window_by_hwnd(ctx, auto, previous.hwnd)
        titles = []
        if win is None:
            pick, titles = find_window(ctx, auto, query)
            win = pick["ctrl"] if pick else None
        if win is None:
            avail = ", ".join(sorted(set(titles))[:10]) or "(none)"
            raise tools.ToolError(f"previous window '{query}' not found (open: {avail})")
        name = _window_name(win, previous.title or query)
        popped, current = ctx.pop_window_context()
        if current is None:
            raise tools.ToolError("window history became empty while restoring")
        refocus_stack_top(ctx, win, title=name, query=query)
        activated = bring_to_front(win)
        title, controls = ctx._collect_controls(auto, max_controls=ctx.MAX_CONTROLS, top=win)
        return popped, name, activated, title, controls

    try:
        popped, name, activated, title, controls = await ctx._uia(_do)
    except tools.ToolError:
        ctx._note_result(False)
        raise
    except Exception as e:
        ctx._note_result(False)
        raise tools.ToolError(f"previous window focus failed: {e}")

    ctx._store_snapshot(title, controls)
    ctx._note_result(True)
    popped_name = (popped.title or popped.query or "(unknown)") if popped else ""
    note = "" if activated else " (couldn't confirm foreground)"
    body = (
        f"Restored '{name}' (popped '{popped_name}').{note}\n"
        f"Window history: {ctx.session.window_stack.format_stack()}\n\n"
        + ctx.format_controls(controls, title)
    )
    from . import modals as dmodals
    body += dmodals.modal_context_note(ctx)
    return body


async def focus_window(ctx, args):
    ctx._ensure_gates()
    args = args or {}
    query = (args.get("name") or args.get("title") or args.get("window") or "").strip()
    if not query:
        raise tools.ToolError(
            "window focus needs a name (part of the window title, "
            "e.g. 'Chrome', 'Notepad', 'Discord')"
        )

    def _do():
        auto = ctx._load_uia()
        pick = None
        titles = []
        history_index = None
        prefer_foreground = bool(args.get("prefer_foreground"))

        if prefer_foreground:
            try:
                foreground = auto.GetForegroundControl()
                foreground = foreground.GetTopLevelControl() if foreground else None
            except Exception:
                foreground = None
            foreground_name = _window_name(foreground)
            if foreground is not None and query.lower() in foreground_name.lower():
                pick = {"ctrl": foreground, "name": foreground_name}

        if pick is None and not prefer_foreground:
            saved_index, saved = _matching_stack_context(ctx.session, query)
            if saved is not None and saved.hwnd:
                saved_window = find_window_by_hwnd(ctx, auto, saved.hwnd)
                if saved_window is not None:
                    history_index = saved_index
                    pick = {
                        "ctrl": saved_window,
                        "name": _window_name(saved_window, saved.title or query),
                    }

        if pick is None:
            pick, titles = find_window(ctx, auto, query)
        waited = 0.0
        import time as _t
        # UIA can't see windows of ELEVATED processes at all (UIPI) — the window
        # may be right there on screen (e.g. Task Manager) while the UIA root
        # lists nothing for it. Waiting can't fix that, so check before looping.
        while not pick and waited < 6.0:
            if delev.find_elevated_window(query):
                break
            _t.sleep(0.8)
            waited += 0.8
            pick, titles = find_window(ctx, auto, query)
        if not pick:
            hit = delev.find_elevated_window(query)
            if hit:
                raise tools.ToolError(derr.tagged_message(
                    delev.uipi_guidance(hit["title"] or query),
                    derr.elevated_target_error(
                        window=hit["title"] or query,
                        pid=hit["pid"],
                        tool="computer",
                    ),
                ))
            avail = ", ".join(sorted(set(titles))[:14]) or "(no ordinary windows found)"
            raise tools.ToolError(
                f"no open window matches '{query}' (waited {waited:.0f}s for it to appear). "
                f"Open windows: {avail}. If the app is not running, launch it with "
                "ordinary Python or run_command, then focus its window."
            )
        win = pick["ctrl"]
        already_active = is_foreground_window(auto, win)
        if history_index is not None:
            ctx.promote_window_context(history_index)
            refocus_stack_top(ctx, win, title=pick["name"], query=query)
        else:
            current = ctx.session.window_stack.get_current()
            current_control = ctx.session.window_stack.get_current_control()
            same_target = bool(
                current
                and (
                    (current.hwnd and current.hwnd == hwnd_for(win))
                    or _same_window(current_control, win)
                )
            )
            if same_target:
                refocus_stack_top(ctx, win, title=pick["name"], query=query)
            else:
                # Preserve exact window identity automatically. The model does
                # not need to opt into bookkeeping before changing focus.
                ctx.set_target(
                    win,
                    title=pick["name"],
                    query=query,
                    push=bool(current),
                )
        activated = True if already_active else bring_to_front(win)
        title, controls = ctx._collect_controls(auto, max_controls=ctx.MAX_CONTROLS, top=win)
        if len(controls) < 5:
            _t.sleep(1.2)
            title, controls = ctx._collect_controls(auto, max_controls=ctx.MAX_CONTROLS, top=win)
        return pick["name"], activated, title, controls

    try:
        focused_name, activated, title, controls = await ctx._uia(_do)
    except tools.ToolError:
        ctx._note_result(False)
        raise
    except Exception as e:
        ctx._note_result(False)
        raise tools.ToolError(f"window focus failed: {e}")
    ctx._store_snapshot(title, controls)
    ctx._note_result(True)
    note = "" if activated else " (couldn't confirm it came to the front; check it's visible)"
    doc_note = _open_document_note(focused_name, query)
    stack_line = ""
    if ctx.session.window_stack.depth() > 1:
        stack_line = f"\nWindow history: {ctx.session.window_stack.format_stack()}"
    body = (f"Focused '{focused_name}'.{note}{doc_note}{stack_line}\n\n"
            + ctx.format_controls(controls, title))
    from . import modals as dmodals
    body += dmodals.modal_context_note(ctx)
    if any(c.get("role") == "Canvas" for c in controls):
        visual = await ctx._attach_visual_state(
            tool="computer", controls=len(controls),
        )
        if visual:
            body += f"\n\n{visual}"
    return body


# Document names that mean "fresh/blank" — no warning needed for these.
_BLANK_DOC_TITLES = frozenset({"untitled", "new tab", "new document", "home", "start page"})


def _open_document_note(window_title: str, query: str) -> str:
    """Return factual active-content context without prescribing a next step."""
    title = (window_title or "").strip().lstrip("*● ").strip()
    if " - " not in title:
        return ""
    doc = title.rsplit(" - ", 1)[0].strip().lstrip("*● ").strip()
    if not doc or doc.lower() in _BLANK_DOC_TITLES:
        return ""
    if query and (query.lower() in doc.lower() or doc.lower() in query.lower()):
        return ""
    return f"\nActive content: '{doc}'."
