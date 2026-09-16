"""Local engine/model management: model scanning, engine restarts, screen capture.

Owns the GGUF model scan (with mmproj projector pairing), the serialized
llama-server restart paths, and the RAM-only screen-capture helpers. Server
wrappers pass live dependencies (router, hub, status-message builder, config
setters) per call; this module never imports server.py.
"""

from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager
from contextvars import ContextVar


# Serialize engine (re)starts so they can't race — process-wide by design.
_ENGINE_LOCK = asyncio.Lock()
_LOCAL_LIFECYCLE_OWNER: ContextVar[tuple[int, asyncio.Task] | None] = ContextVar(
    "local_lifecycle_owner", default=None
)
_MISSING = object()


@asynccontextmanager
async def _local_lifecycle_slot(router):
    """Serialize process transitions with local token generation.

    ``llm_local_stream.call_local`` holds ``router._local_gate`` for the full
    response stream.  Engine stops and restarts must enter that same gate or a
    Settings change can terminate llama-server while response/tool deltas are
    still being consumed.  Routers used during early boot and narrow unit tests
    may not have a gate wired yet; ``_ENGINE_LOCK`` remains the fallback there.
    """
    owner = (id(router), asyncio.current_task())
    if _LOCAL_LIFECYCLE_OWNER.get() == owner:
        yield
        return
    gate_factory = getattr(router, "_local_gate", None)
    gate = gate_factory() if callable(gate_factory) else _null_lifecycle_slot()
    async with gate:
        token = _LOCAL_LIFECYCLE_OWNER.set(owner)
        try:
            yield
        finally:
            _LOCAL_LIFECYCLE_OWNER.reset(token)


@asynccontextmanager
async def _null_lifecycle_slot():
    yield


def _snapshot_model_selection(router) -> dict:
    cfg = getattr(router, "cfg", {})
    local = cfg.get("local", {}) if isinstance(cfg, dict) else {}
    local = local if isinstance(local, dict) else {}
    return {
        "model": local.get("model", _MISSING),
        "mmproj": local.get("mmproj", _MISSING),
    }


def _restore_model_selection(
    router,
    snapshot: dict,
    *,
    strict: bool = False,
) -> None:
    """Restore only model-owned config fields, preserving concurrent settings."""
    cfg = getattr(router, "cfg", None)
    if not isinstance(cfg, dict):
        return
    local = cfg.setdefault("local", {})
    if not isinstance(local, dict):
        local = {}
        cfg["local"] = local
    for mapping, key in ((local, "model"), (local, "mmproj")):
        old_value = snapshot[key]
        if old_value is _MISSING:
            mapping.pop(key, None)
        else:
            mapping[key] = old_value
    save = getattr(router, "save_config", None)
    if callable(save):
        save(strict=strict)


async def _restore_runtime(engine, *, was_ready: bool, model: str, mmproj: str) -> bool:
    """Best-effort rollback after a candidate model failed to become healthy."""
    if was_ready and model:
        try:
            await engine.restart(model, mmproj)
            return bool(getattr(engine, "ready", True))
        except Exception as exc:
            print(f"[variant1-backend] previous model rollback failed: {exc}", flush=True)
            return False

    # There was no healthy runtime to relaunch.  Clean up any partial candidate
    # process and restore the engine's selected paths so a later start follows
    # the still-valid configuration rather than retrying the broken target.
    stop = getattr(engine, "stop", None)
    if callable(stop):
        try:
            await stop()
        except Exception:
            pass
    if hasattr(engine, "model"):
        engine.model = model
    if hasattr(engine, "mmproj"):
        engine.mmproj = mmproj
    return False


async def reconcile_local_engine(router) -> str:
    """Make the live llama engine match the router's current routing policy.

    Returns ``started``, ``stopped``, or ``unchanged`` for logs/tests.  The
    process-wide engine lock also serializes model reloads and rapid Settings
    changes so a stop cannot race a start.
    """
    async with _local_lifecycle_slot(router):
        async with _ENGINE_LOCK:
            wanted = router.wants_local_engine()
            engine = router.engine
            poll = getattr(engine, "poll_process", None)
            if callable(poll):
                poll()
            running = bool(engine.ready or getattr(engine, "proc", None) is not None)
            if wanted and not engine.ready:
                await router.start_local()
                return "started"
            if not wanted and running:
                await engine.stop()
                return "stopped"
        return "unchanged"


async def ensure_active_runtime(router) -> str:
    """Start the currently selected runtime under the lifecycle gates.

    The loopback OpenAI gateway is an explicit local-inference caller even when
    the conversation route is cloud, so it cannot use ``wants_local_engine`` as
    its policy.  It still shares the same locks as route/model switches.
    """
    async with _local_lifecycle_slot(router):
        async with _ENGINE_LOCK:
            poll = getattr(router.engine, "poll_process", None)
            if callable(poll):
                poll()
            if router.engine_ready:
                return "already_ready"
            await router.engine.start()
            return "started"


async def ensure_local_engine(router) -> None:
    """Ensure a wanted local engine is up (restart if the process died).

    Serialized through the same lock as reconcile/model switch so concurrent
    chat turns do not double-spawn llama-server.
    """
    async with _local_lifecycle_slot(router):
        if not router.wants_local_engine():
            raise RuntimeError("local engine not wanted by current routing policy")
        async with _ENGINE_LOCK:
            engine = router.engine
            poll = getattr(engine, "poll_process", None)
            if callable(poll):
                poll()
            if engine.ready:
                return
            name = getattr(engine, "display_name", "local inference")
            print(f"[variant1-backend] local engine down; attaching {name}", flush=True)
            await router.start_local()


async def switch_inference_runtime(router, runtime_id: str) -> str:
    """Atomically replace the local-route engine after the candidate is ready.

    External runtimes are probed before the current engine is detached.  The
    shared local gate prevents a Settings switch from replacing the adapter in
    the middle of a streamed response.
    """
    async with _local_lifecycle_slot(router):
        async with _ENGINE_LOCK:
            candidate = router.build_inference_runtime(runtime_id)
            old = router.engine
            same_instance_kind = (
                getattr(old, "runtime_id", "llamacpp")
                == getattr(candidate, "runtime_id", "llamacpp")
            )
            if router.wants_local_engine():
                await candidate.start()
            if old is not candidate:
                old_running = bool(
                    getattr(old, "ready", False)
                    or getattr(old, "proc", None) is not None)
                if old_running:
                    await old.stop()
            router.commit_inference_runtime(runtime_id, candidate)
            return "reconfigured" if same_instance_kind else "switched"


def find_mmproj(directory, model_name):
    """Find a vision projector (mmproj) for a model in the same folder."""
    try:
        cands = [g for g in os.listdir(directory)
                 if g.lower().endswith(".gguf") and ("mmproj" in g.lower() or "mproj" in g.lower())]
    except Exception:
        return ""
    if not cands:
        return ""

    stem = os.path.splitext(model_name)[0].lower()

    def _toks(s: str) -> list:
        return [t for t in re.split(r"[^a-z0-9]+", s) if t]

    # Match on LEADING NAME TOKENS, not a raw character prefix. A character prefix
    # is dangerously loose: "gemma-4-e4b-it…" and "gemma-4-12b-it…" share the
    # 8-char prefix "gemma-4-", so an E4B model would grab a 12B projector — which
    # makes llama-server fail to load. Comparing tokens makes the size/variant token
    # (e4b vs 12b) decisive: it must agree before we pair.
    mtok = _toks(stem)
    best, best_score = "", 0
    for g in cands:
        gstem = re.sub(r"^m?mproj[-_]?", "", os.path.splitext(g)[0].lower())
        gtok = _toks(gstem)
        score = 0
        for a, b in zip(mtok, gtok):
            if a != b:
                break
            score += 1
        if score > best_score:
            best, best_score = g, score
    # Require family + major-version + size to agree (≥3 leading tokens, e.g.
    # "gemma","4","12b"), or — for short names — every token of the model. There is
    # deliberately NO "sole candidate" fallback: attaching a wrong projector kills
    # the engine, whereas no projector just means text-only (vision off), which is
    # the safe degradation.
    need = min(3, len(mtok))
    return best if best_score >= need and best_score >= 2 else ""


def scan_models(data_dir: str):
    """List user-provided GGUF models below the writable ``models/user`` tree."""
    out, seen = [], set()
    sub = "models/user"
    d = os.path.join(data_dir, sub)
    if not os.path.isdir(d):
        return out
    resolved_root = os.path.realpath(d)
    for folder, dirs, files in os.walk(d, followlinks=False):
        # Avoid following links outside the model tree and keep traversal
        # deterministic across platforms/filesystems.
        dirs[:] = sorted(
            name for name in dirs
            if not os.path.islink(os.path.join(folder, name)))
        for filename in sorted(files):
            low = filename.lower()
            if (
                not low.endswith(".gguf")
                or "mmproj" in low
                or "mproj" in low
                or (re.search(r"-(\d{5})-of-\d{5}\.gguf$", low)
                    and not re.search(r"-00001-of-\d{5}\.gguf$", low))
            ):
                continue
            path = os.path.abspath(os.path.join(folder, filename))
            if os.path.islink(path):
                continue
            try:
                contained = os.path.commonpath(
                    (resolved_root, os.path.realpath(path)))
            except ValueError:
                continue
            if os.path.normcase(contained) != os.path.normcase(resolved_root):
                continue
            relative = os.path.relpath(path, d)
            identity = os.path.normcase(os.path.normpath(
                os.path.join(sub, relative)))
            if identity in seen:
                continue
            seen.add(identity)
            mmproj = find_mmproj(folder, filename)
            mmproj_path = os.path.join(folder, mmproj) if mmproj else ""
            if mmproj_path and os.path.islink(mmproj_path):
                mmproj_path = ""
            try:
                size_bytes = os.path.getsize(path)
            except OSError:
                continue
            out.append({
                "path": path,
                # Nested entries include their readable repository scope so
                # identical GGUF basenames remain distinguishable.
                "name": relative.replace(os.sep, "/"),
                "mmproj": mmproj_path,
                "vision": bool(mmproj_path),
                "size_bytes": size_bytes,
            })
    return out


@asynccontextmanager
async def local_model_files_slot(router):
    """Serialize installed-model file changes with inference and model switches."""
    async with _local_lifecycle_slot(router):
        async with _ENGINE_LOCK:
            yield


async def eject_local_model(router) -> dict:
    """Release the active managed process while retaining its selected files."""
    async with local_model_files_slot(router):
        if str(getattr(router.engine, 'runtime_id', 'llamacpp')) != 'llamacpp':
            raise ValueError('Eject applies to the managed llama.cpp runtime.')
        await router.engine.stop()
        return {'status': 'ejected', 'model': str(getattr(router.engine, 'model', '') or '')}


async def remove_local_model_directory(router, directory: str, *, library_root: str, staging_root: str) -> None:
    """Withdraw an idle owned download atomically before clearing its selection."""
    from pathlib import Path
    import shutil
    import uuid
    target, root = Path(directory).resolve(), Path(library_root).resolve()
    staging = Path(staging_root).resolve()
    if target == root or not target.is_relative_to(root):
        raise ValueError('Model deletion path changed.')
    async with local_model_files_slot(router):
        engine = router.engine
        old_model, old_projector = str(getattr(engine, 'model', '') or ''), str(getattr(engine, 'mmproj', '') or '')
        selected = any(value and Path(value).resolve().is_relative_to(target) for value in (old_model, old_projector))
        if selected and (getattr(engine, 'ready', False) or getattr(engine, 'proc', None) is not None):
            raise ValueError('Eject the running model before deleting its files.')
        snapshot = _snapshot_model_selection(router)
        staging.mkdir(parents=True, exist_ok=True)
        withdrawn = staging / ('delete_' + uuid.uuid4().hex)
        target.rename(withdrawn)
        try:
            if selected:
                router.set_local_model('', '', strict=True)
                engine.model, engine.mmproj = '', ''
        except BaseException:
            withdrawn.rename(target)
            _restore_model_selection(router, snapshot)
            engine.model, engine.mmproj = old_model, old_projector
            raise
        if not withdrawn.resolve().is_relative_to(staging):
            raise ValueError('Model cleanup path changed.')
        shutil.rmtree(withdrawn)


async def restart_engine(
    router,
    hub,
    status_msg,
    model_path,
    mmproj_path="",
    *,
    should_apply=None,
) -> str:
    """Transactionally switch the managed local model.

    The candidate is launched before its paths are persisted.  If launch (or
    the config commit) fails, the previous selection is restored and a
    previously healthy runtime is relaunched.  Returns ``switched``,
    ``rolled_back``, ``failed``, or ``superseded`` for callers/tests.  The
    optional guard is evaluated only after both transition gates are held, so a
    picker request that became stale while waiting never launches its model.
    """
    async with _local_lifecycle_slot(router):
        async with _ENGINE_LOCK:
            if callable(should_apply) and not should_apply():
                return "superseded"
            engine = router.engine
            poll = getattr(engine, "poll_process", None)
            if callable(poll):
                poll()
            old_selection = _snapshot_model_selection(router)
            configured_model = old_selection["model"]
            configured_mmproj = old_selection["mmproj"]
            old_model = str(getattr(engine, "model", "") or
                            ("" if configured_model is _MISSING else configured_model))
            old_mmproj = str(getattr(engine, "mmproj", "") or
                             ("" if configured_mmproj is _MISSING else configured_mmproj))
            old_ready = bool(getattr(engine, "ready", False))

            validate_selection = getattr(engine, "validate_selection", None)
            if callable(validate_selection):
                validate_selection(model_path, mmproj_path)

            # The picker carries the pending target independently, so this
            # loading broadcast can truthfully keep showing the last committed
            # model until the new runtime is healthy.
            await hub.broadcast(status_msg())
            try:
                await engine.restart(model_path, mmproj_path)
                # llama-server may reject an incompatible projector and recover
                # text-only. Persist the effective projector, not the rejected
                # requested path.
                effective_mmproj = str(getattr(engine, "mmproj", mmproj_path) or "")
                router.set_local_model(
                    model_path,
                    effective_mmproj,
                    strict=True,
                )
            except Exception as exc:
                config_restore_error: BaseException | None = None
                try:
                    _restore_model_selection(
                        router,
                        old_selection,
                        strict=True,
                    )
                except BaseException as restore_exc:
                    # Runtime rollback is independent of config persistence. A
                    # full disk or denied replace must not leave the candidate
                    # model running merely because saving the old selection also
                    # failed.
                    config_restore_error = restore_exc
                rolled_back = await _restore_runtime(
                    engine,
                    was_ready=old_ready,
                    model=old_model,
                    mmproj=old_mmproj,
                )
                state = (
                    "rolled_back"
                    if rolled_back and config_restore_error is None
                    else "failed"
                )
                restore_detail = (
                    f"; config restore failed: {config_restore_error}"
                    if config_restore_error is not None else ""
                )
                print(
                    f"[variant1-backend] model switch failed: {exc}"
                    f"{restore_detail}; state={state}",
                    flush=True,
                )
                await hub.broadcast(status_msg())
                return state

            print(
                f"[variant1-backend] switched model -> {os.path.basename(model_path)}"
                f"{' (vision)' if effective_mmproj else ''}",
                flush=True,
            )
            await hub.broadcast(status_msg())
            return "switched"


async def activate_llama_binary(router, *, binary: str, backend: str, tag: str) -> None:
    """Publish a verified executable only after the live engine follows it."""
    async with _local_lifecycle_slot(router):
        async with _ENGINE_LOCK:
            engine = router.engine
            local = router.cfg.setdefault("local", {})
            keys = ("binary", "backend", "runtime_tag")
            previous = {key: local.get(key, _MISSING) for key in keys}
            native = getattr(engine, "runtime_id", "") == "llamacpp"
            attrs = {key: getattr(engine, key, _MISSING) for key in (
                "binary", "backend", "_runtime_probe_complete", "_runtime_flags")}
            was_ready = bool(getattr(engine, "ready", False))
            running = native and bool(was_ready or getattr(engine, "proc", None) is not None)
            model, mmproj = getattr(engine, "model", ""), getattr(engine, "mmproj", "")
            try:
                if native:
                    engine.binary, engine.backend = binary, backend
                    engine._runtime_probe_complete, engine._runtime_flags = False, None
                    if running:
                        await engine.restart(model, mmproj)
                local.update(binary=binary, backend=backend, runtime_tag=tag)
                router.save_config(strict=True)
            except BaseException as error:
                for key, value in previous.items():
                    if value is _MISSING:
                        local.pop(key, None)
                    else:
                        local[key] = value
                if native:
                    for key, value in attrs.items():
                        if value is not _MISSING:
                            setattr(engine, key, value)
                    if running:
                        rollback = asyncio.create_task(_restore_runtime(
                            engine, was_ready=was_ready, model=model, mmproj=mmproj))
                        while not rollback.done():
                            try:
                                await asyncio.shield(rollback)
                            except asyncio.CancelledError:
                                continue
                        if not rollback.result() and was_ready:
                            error.add_note("The previous llama runtime could not be restored.")
                try:
                    router.save_config(strict=True)
                except Exception as failure:
                    error.add_note(f"Previous runtime configuration could not be saved: {failure}")
                raise


async def restart_engine_keep_model(router, hub, status_msg):
    """Relaunch llama-server with the current model to apply launch-only flags
    (e.g. --reasoning-budget, which this build won't change per-request).
    Serialized + coalesced so rapid toggles don't race or reload needlessly."""
    async with _local_lifecycle_slot(router):
        async with _ENGINE_LOCK:
            eng = router.engine
            # Already running with the desired thinking state? Nothing to do.
            if eng.ready and getattr(eng, "_running_reasoning_budget", None) == eng.reasoning_budget:
                await hub.broadcast(status_msg())
                return
            loc = router.cfg.get("local", {}) or {}
            model_path = loc.get("model", "") or None
            mmproj_path = loc.get("mmproj", "") or None
            await hub.broadcast(status_msg())     # shows "loading" while it reloads
            try:
                await eng.restart(model_path, mmproj_path)
                print(f"[variant1-backend] engine reloaded (reasoning="
                      f"{'on' if router.reasoning else 'off'})", flush=True)
            except Exception as e:
                print(f"[variant1-backend] reasoning restart failed: {e}", flush=True)
            await hub.broadcast(status_msg())
