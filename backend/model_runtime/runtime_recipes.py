"""Saved model launch recipes and supervised runtime lifecycle."""

from __future__ import annotations

import asyncio
from collections import deque
import os
import re
import shlex
import sys
import time
import uuid
from urllib.parse import urlparse

import httpx

from process_tree import (
    OwnedProcessTree,
    attach_process_and_reap,
    dispose_process_tree,
    settle_process,
)
from model_runtime.platform_store import AtomicJsonStore
from work_fabric.jobs import JobExecutionContext, JobResult
from work_fabric.scope import WorkScope


_NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0
_RUNTIME_DEFAULT_PORT = {"vllm": 8000, "sglang": 30000, "mlx": 8080}
RUNTIME_RECIPE_LAUNCH_JOB = "inference.runtime.recipe.launch.v1"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower()).strip("-")[:60]


def _port(value, default: int) -> int:
    try:
        return max(1024, min(65535, int(value)))
    except (TypeError, ValueError):
        return default


def _context(value) -> int:
    try:
        return max(2048, min(1_048_576, int(value)))
    except (TypeError, ValueError):
        return 32768


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _bind_host(value) -> str:
    host = str(value or "127.0.0.1").strip() or "127.0.0.1"
    if host not in _LOOPBACK_HOSTS:
        raise ValueError("recipe bind host must be loopback")
    return host


def _safe_args(value) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    skip_value = False
    for item in value[:100]:
        text = str(item).strip()
        if not text:
            continue
        if skip_value:
            skip_value = False
            continue
        lowered = text.lower()
        if lowered in {"--host", "--port"}:
            skip_value = True
            continue
        if lowered.startswith("--host=") or lowered.startswith("--port="):
            continue
        out.append(text[:500])
    return out


def _safe_env(value) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out = {}
    for key, raw in list(value.items())[:50]:
        name = str(key or "").strip()
        if name and name.replace("_", "A").isalnum():
            out[name[:120]] = str(raw or "")[:1000]
    return out


class RuntimeRecipeManager:
    """One-active-model recipe store with observed process ownership."""

    def __init__(
        self,
        data_dir: str,
        installer,
        remote_nodes,
        broadcast,
        *,
        event_sink=None,
    ) -> None:
        path = os.path.join(data_dir, "data", "inference", "recipes.json")
        self.store = AtomicJsonStore(path, {"version": 1, "items": []})
        loaded = self.store.load()
        rows = loaded.get("items") if isinstance(loaded, dict) else []
        self.recipes: list[dict] = [row for row in (rows or []) if isinstance(row, dict)]
        self.installer = installer
        self.remote_nodes = remote_nodes
        self.broadcast = broadcast
        self.event_sink = event_sink
        self.host = None
        self.process: asyncio.subprocess.Process | None = None
        self._process_job: OwnedProcessTree | None = None
        self.active_recipe_id = ""
        self.launching_recipe_id = ""
        self.last_error = ""
        self.started_at = 0.0
        self.logs: deque[dict] = deque(maxlen=500)
        self._reader_task: asyncio.Task | None = None
        self._watcher_task: asyncio.Task | None = None
        self._work = None
        self._launch_job_id = ""
        self._lock = asyncio.Lock()
        self._failures: dict[str, deque[float]] = {}

    def bind_host(self, host) -> None:
        self.host = host

    def bind_work(self, work) -> None:
        if self._work is work:
            return
        if self._work is not None:
            raise RuntimeError("RuntimeRecipeManager is already bound to Work Fabric")
        self._work = work
        work.register_job_handler(
            RUNTIME_RECIPE_LAUNCH_JOB,
            self._work_launch,
            max_concurrency=1,
        )

    def get(self, recipe_id: str) -> dict | None:
        return next((row for row in self.recipes if row.get("id") == recipe_id), None)

    def list(self) -> dict:
        self._poll_process()
        return {
            "type": "inference:recipes",
            "active_recipe_id": self.active_recipe_id,
            "launching_recipe_id": self.launching_recipe_id,
            "launch_job_id": self._launch_job_id,
            "process": self.process_status(),
            "items": [
                {
                    **row,
                    "status": (
                        "starting" if row.get("id") == self.launching_recipe_id
                        else "running" if row.get("id") == self.active_recipe_id and self.process_status().get("ready")
                        else "error" if self._blocked(str(row.get("id") or ""))
                        else "stopped"
                    ),
                    "crash_loop": self._failure_status(str(row.get("id") or "")),
                }
                for row in self.recipes
            ],
        }

    def save(self, value: dict) -> dict:
        if not isinstance(value, dict):
            raise ValueError("recipe must be an object")
        recipe_id = str(value.get("id") or "").strip()
        existing = self.get(recipe_id) if recipe_id else None
        runtime_id = str(value.get("runtime_id") or (existing or {}).get("runtime_id") or "llamacpp").strip().lower()
        if runtime_id not in {"llamacpp", "vllm", "sglang", "mlx", "openai_compatible"}:
            raise ValueError("unsupported recipe runtime")
        model = str(value.get("model") or (existing or {}).get("model") or "").strip()[:500]
        model_path = str(value.get("model_path") or (existing or {}).get("model_path") or "").strip()[:1000]
        mmproj_path = str(
            value.get("mmproj_path")
            or (existing or {}).get("mmproj_path")
            or ""
        ).strip()[:1000]
        if not model and not model_path:
            raise ValueError("recipe needs a model ID or local model path")
        name = str(value.get("name") or (existing or {}).get("name") or model or os.path.basename(model_path)).strip()[:120]
        port = _port(value.get("port", (existing or {}).get("port")), _RUNTIME_DEFAULT_PORT.get(runtime_id, 8000))
        endpoint_value = str(value.get("endpoint") or (existing or {}).get("endpoint") or "").strip().rstrip("/")
        endpoint = endpoint_value or f"http://127.0.0.1:{port}"
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("recipe endpoint must be an http:// or https:// URL")
        if value.get("endpoint") and "port" not in value and parsed.port:
            port = _port(parsed.port, port)
        host_raw = str(value.get("host") or (existing or {}).get("host") or parsed.hostname or "127.0.0.1")[:200]
        host = _bind_host(host_raw) if runtime_id in {"vllm", "sglang", "mlx"} else host_raw
        now = time.time()
        row = {
            "id": recipe_id or f"{_slug(name) or 'recipe'}-{uuid.uuid4().hex[:8]}",
            "name": name,
            "runtime_id": runtime_id,
            "model": model,
            "model_path": model_path,
            "mmproj_path": mmproj_path,
            "model_format": str(value.get("model_format") or (existing or {}).get("model_format") or "")[:80],
            "source_repo": str(value.get("source_repo") or (existing or {}).get("source_repo") or "")[:300],
            "target_id": str(value.get("target_id") or (existing or {}).get("target_id") or "")[:240],
            "node_id": str(value.get("node_id") or (existing or {}).get("node_id") or "")[:120],
            "remote_recipe_id": str(value.get("remote_recipe_id") or (existing or {}).get("remote_recipe_id") or "")[:240],
            "endpoint": endpoint,
            "host": host,
            "port": port,
            "context_size": _context(value.get("context_size", (existing or {}).get("context_size"))),
            "gpu_ids": [int(item) for item in (value.get("gpu_ids") or (existing or {}).get("gpu_ids") or []) if str(item).isdigit()][:32],
            "arguments": _safe_args(value.get("arguments", (existing or {}).get("arguments"))),
            "environment": _safe_env(value.get("environment", (existing or {}).get("environment"))),
            "autostart": bool(value.get("autostart", (existing or {}).get("autostart", False))),
            "restart_on_crash": bool(value.get("restart_on_crash", (existing or {}).get("restart_on_crash", True))),
            "created_at": float((existing or {}).get("created_at") or now),
            "updated_at": now,
        }
        if existing:
            candidate = [row if item.get("id") == row["id"] else item for item in self.recipes]
        else:
            candidate = [*self.recipes, row]
        self.store.save({"version": 1, "items": candidate})
        self.recipes = candidate
        self._event("info", f"Launch recipe saved: {name}", {"recipe_id": row["id"]})
        return row

    def remove(self, recipe_id: str) -> bool:
        if recipe_id in {self.active_recipe_id, self.launching_recipe_id}:
            raise ValueError("stop the active recipe before removing it")
        before = len(self.recipes)
        candidate = [row for row in self.recipes if row.get("id") != recipe_id]
        changed = before != len(candidate)
        if changed:
            self.store.save({"version": 1, "items": candidate})
            self.recipes = candidate
        return changed

    def start_launch(self, recipe_id: str) -> dict:
        recipe = self.get(recipe_id)
        if not recipe:
            raise ValueError("recipe not found")
        if self._blocked(recipe_id):
            raise RuntimeError("recipe launch is temporarily blocked after repeated failures")
        if self._work is None:
            raise RuntimeError("runtime launch requires Work Fabric")
        active = self._work.jobs.list(
            kind=RUNTIME_RECIPE_LAUNCH_JOB,
            statuses=("queued", "leased", "running", "waiting"),
            limit=10,
        )
        if active:
            raise RuntimeError("another recipe launch is already in progress")
        self.launching_recipe_id = recipe_id
        self.last_error = ""
        job = self._create_launch_job(recipe)
        self._launch_job_id = job.job_id
        return {**self.process_status(), "job_id": job.job_id}

    def _create_launch_job(
        self,
        recipe: dict,
        *,
        available_at: float | None = None,
        source: str = "user",
    ):
        if self._work is None:
            raise RuntimeError("runtime launch requires Work Fabric")
        return self._work.jobs.create(
            RUNTIME_RECIPE_LAUNCH_JOB,
            owner_kind="inference_recipe",
            owner_id=str(recipe.get("id") or ""),
            scope=WorkScope(),
            input_manifest={"recipe": dict(recipe), "source": str(source or "user")},
            max_attempts=3,
            retry_policy={
                "on_lease_expiry": "retry",
                "base_delay_s": 2.0,
                "max_delay_s": 30.0,
            },
            available_at=available_at,
        )

    async def cancel_launch(self) -> bool:
        if self._work is None:
            return False
        job = self._work.jobs.get(self._launch_job_id) if self._launch_job_id else None
        if job is None or job.terminal:
            rows = self._work.jobs.list(
                kind=RUNTIME_RECIPE_LAUNCH_JOB,
                statuses=("queued", "leased", "running", "waiting"),
                limit=1,
            )
            job = rows[0] if rows else None
        if job is None:
            return False
        self._work.jobs.cancel(job.job_id, reason="cancelled by user")
        self._work.scheduler.cancel_active(job.job_id)
        return True

    async def _work_launch(self, execution: JobExecutionContext) -> JobResult:
        recipe = dict((execution.job.input_manifest or {}).get("recipe") or {})
        recipe_id = str(recipe.get("id") or execution.job.owner_id)
        if not recipe_id:
            raise RuntimeError("runtime launch job has no recipe")
        self.launching_recipe_id = recipe_id
        self._launch_job_id = execution.job.job_id
        await asyncio.to_thread(execution.progress, {
            "phase": "launching",
            "recipe_id": recipe_id,
        })
        await self._launch(recipe)
        return JobResult(progress={
            "phase": "ready",
            "recipe_id": recipe_id,
            "endpoint": str(recipe.get("endpoint") or ""),
        })

    async def _launch(self, recipe: dict) -> None:
        recipe_id = str(recipe["id"])
        self._event("info", f"Launching {recipe['name']}", {"recipe_id": recipe_id})
        await self._broadcast_state()
        try:
            async with self._lock:
                await self._evict_unlocked(clear_adapter=False)
                if recipe.get("node_id"):
                    await self.remote_nodes.launch(str(recipe["node_id"]), str(recipe.get("remote_recipe_id") or recipe_id))
                    node = self.remote_nodes.get(str(recipe["node_id"]))
                    if not node:
                        raise RuntimeError("remote node disappeared during launch")
                    recipe = {**recipe, "endpoint": node["base_url"]}
                    if not await self._wait_ready(recipe["endpoint"], timeout=180.0):
                        raise RuntimeError(f"remote runtime did not become ready at {recipe['endpoint']}")
                    await self._attach_recipe(recipe)
                    self.active_recipe_id = recipe_id
                    self.started_at = time.time()
                    self._event("ok", f"Remote runtime ready: {recipe['name']}", {"recipe_id": recipe_id})
                    return
                elif recipe["runtime_id"] == "llamacpp":
                    await self._launch_llamacpp(recipe)
                    self.active_recipe_id = recipe_id
                    self.started_at = time.time()
                    self._event("ok", f"Runtime ready: {recipe['name']}", {"recipe_id": recipe_id})
                    return
                elif recipe["runtime_id"] == "openai_compatible":
                    await self._attach_recipe(recipe)
                    self.active_recipe_id = recipe_id
                    self.started_at = time.time()
                    self._event("ok", f"Runtime attached: {recipe['name']}", {"recipe_id": recipe_id})
                    return
                else:
                    target = self._resolve_target(recipe)
                    command, env = self._command(recipe, target)
                    self._log("launch", "$ " + " ".join(shlex.quote(part) for part in command))
                    self.process = await asyncio.create_subprocess_exec(
                        *command,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        env=env,
                        creationflags=_NO_WINDOW,
                        start_new_session=os.name != "nt",
                    )
                    try:
                        self._process_job = await attach_process_and_reap(
                            self._process_job, self.process
                        )
                    except BaseException:
                        self.process = None
                        raise
                    self._reader_task = asyncio.create_task(self._read_logs(self.process, recipe_id))
                    self._watcher_task = asyncio.create_task(
                        self._watch_process(self.process, recipe_id),
                        name=f"inference-watch:{recipe_id}",
                    )
                    if not await self._wait_ready(recipe["endpoint"], timeout=300.0):
                        code = self.process.returncode if self.process else None
                        raise RuntimeError(f"runtime did not become ready at {recipe['endpoint']} (exit={code})")
                    await self._attach_recipe(recipe)
                    self.active_recipe_id = recipe_id
                    self.started_at = time.time()
                    self._event("ok", f"Runtime ready: {recipe['name']}", {"recipe_id": recipe_id})
        except asyncio.CancelledError:
            self._event("info", f"Launch cancelled: {recipe['name']}", {"recipe_id": recipe_id})
            async with self._lock:
                await self._evict_unlocked(clear_adapter=True)
            raise
        except Exception as exc:
            self.last_error = str(exc)[:500]
            self._record_failure(recipe_id)
            self._log("error", self.last_error)
            self._event("error", f"Launch failed for {recipe['name']}: {exc}", {"recipe_id": recipe_id})
            async with self._lock:
                await self._evict_unlocked(clear_adapter=True)
            raise
        finally:
            self.launching_recipe_id = ""
            self._launch_job_id = ""
            await self._broadcast_state()

    async def _launch_llamacpp(self, recipe: dict) -> None:
        if self.host is None:
            raise RuntimeError("recipe manager is not bound to the application host")
        from model_runtime import engine_manager
        if self.host.router.inference_runtime_id != "llamacpp":
            await engine_manager.switch_inference_runtime(self.host.router, "llamacpp")
        path = str(recipe.get("model_path") or "")
        if not path or not os.path.isfile(path):
            raise RuntimeError("GGUF recipe model file is not installed")
        mmproj_path = str(recipe.get("mmproj_path") or "").strip()
        if mmproj_path and not os.path.isfile(mmproj_path):
            raise RuntimeError("GGUF recipe vision projector is not installed")
        if not mmproj_path:
            projector = engine_manager.find_mmproj(
                os.path.dirname(path), os.path.basename(path)
            )
            if projector:
                mmproj_path = os.path.join(os.path.dirname(path), projector)
        await self.host.require_runtime().models.restart_engine(path, mmproj_path)

    async def _attach_recipe(self, recipe: dict) -> None:
        if self.host is None:
            raise RuntimeError("recipe manager is not bound to the application host")
        runtime_id = str(recipe["runtime_id"])
        self.host.router.configure_inference_runtime(runtime_id, {
            "endpoint": recipe["endpoint"],
            "model": recipe.get("model") or recipe.get("source_repo"),
            "context_size": recipe.get("context_size") or 32768,
        })
        from model_runtime import engine_manager
        await engine_manager.switch_inference_runtime(self.host.router, runtime_id)

    def _resolve_target(self, recipe: dict) -> dict:
        target_id = str(recipe.get("target_id") or "")
        target = self.installer.target(target_id) if target_id else self.installer.recommended_target(str(recipe["runtime_id"]))
        if not target or not target.get("available"):
            raise RuntimeError(f"no available target for {recipe['runtime_id']}")
        if not target.get("installed"):
            raise RuntimeError(f"{target['label']} is not installed; install it in Settings first")
        if target.get("kind") == "container":
            raise RuntimeError("container targets need a user-selected image/GPU policy; attach its endpoint or choose another target")
        return target

    def _command(self, recipe: dict, target: dict) -> tuple[list[str], dict[str, str]]:
        runtime_id = str(recipe["runtime_id"])
        model = str(recipe.get("model_path") or recipe.get("model") or recipe.get("source_repo") or "")
        host = _bind_host(recipe.get("host"))
        port = int(recipe["port"])
        extras = _safe_args(recipe.get("arguments"))
        if runtime_id == "vllm":
            inner = [target["python"], "-m", "vllm.entrypoints.openai.api_server", "--model", model, "--host", host, "--port", str(port)]
        elif runtime_id == "sglang":
            inner = [target["python"], "-m", "sglang.launch_server", "--model-path", model, "--host", host, "--port", str(port)]
        elif runtime_id == "mlx":
            inner = [target["python"], "-m", "mlx_lm.server", "--model", model, "--host", host, "--port", str(port)]
        else:
            raise RuntimeError(f"runtime {runtime_id} is attach-only")
        inner.extend(extras)
        environment = {**os.environ, **_safe_env(recipe.get("environment"))}
        gpu_ids = recipe.get("gpu_ids") or []
        if gpu_ids:
            environment["CUDA_VISIBLE_DEVICES"] = ",".join(str(item) for item in gpu_ids)
        if target["kind"] == "managed_wsl":
            exports = []
            for key, value in _safe_env(recipe.get("environment")).items():
                exports.append(f"export {key}={shlex.quote(value)}")
            if gpu_ids:
                exports.append(f"export CUDA_VISIBLE_DEVICES={shlex.quote(','.join(str(item) for item in gpu_ids))}")
            script = "; ".join([*exports, shlex.join(inner)])
            return [target["executable"], "-d", target["distro"], "--", "bash", "-lc", script], environment
        return inner, environment

    async def _read_logs(self, process: asyncio.subprocess.Process, recipe_id: str) -> None:
        if process.stdout is None:
            return
        try:
            async for raw in process.stdout:
                self._log("runtime", raw.decode("utf-8", errors="replace").rstrip(), recipe_id=recipe_id)
                await self._broadcast_logs()
        except asyncio.CancelledError:
            return

    async def _watch_process(self, process: asyncio.subprocess.Process, recipe_id: str) -> None:
        try:
            code = await process.wait()
        except asyncio.CancelledError:
            return
        async with self._lock:
            if self.process is not process:
                return
            # A process that exits before readiness is still owned by _launch.
            # Let that path clean it up and count the failure exactly once.
            if self.active_recipe_id != recipe_id:
                return
            owner, self._process_job = self._process_job, None
            if owner is not None:
                dispose_process_tree(owner, terminate=False)
            self.process = None
            self.active_recipe_id = ""
            self.started_at = 0.0
            self.last_error = f"runtime process exited with code {code}"
            self._record_failure(recipe_id)
            self._log("error", self.last_error, recipe_id=recipe_id)
            self._event("error", self.last_error, {"recipe_id": recipe_id})
            self._schedule_restart(recipe_id)
        await self._broadcast_state()

    def _schedule_restart(self, recipe_id: str) -> None:
        recipe = self.get(recipe_id)
        if not recipe or not recipe.get("restart_on_crash", True) or self._blocked(recipe_id):
            if self._blocked(recipe_id):
                self._event("error", "Automatic restart stopped by the crash-loop gate", {"recipe_id": recipe_id})
            return
        if self._work is None:
            return
        active = self._work.jobs.list(
            kind=RUNTIME_RECIPE_LAUNCH_JOB,
            statuses=("queued", "leased", "running", "waiting"),
            limit=10,
        )
        if active:
            return
        failures = self._failure_status(recipe_id)["failures"]
        delay = min(8.0, float(2 ** max(0, failures - 1)))
        self._log(
            "supervisor",
            f"Restarting after crash in {delay:.0f}s",
            recipe_id=recipe_id,
        )
        self._create_launch_job(
            recipe,
            available_at=time.time() + delay,
            source="crash_restart",
        )

    async def _wait_ready(self, endpoint: str, timeout: float) -> bool:
        deadline = asyncio.get_event_loop().time() + timeout
        async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
            while asyncio.get_event_loop().time() < deadline:
                if self.process is not None and self.process.returncode is not None:
                    return False
                try:
                    response = await client.get(f"{endpoint.rstrip('/')}/v1/models")
                    if response.status_code == 200:
                        return True
                except Exception:
                    pass
                await asyncio.sleep(.5)
        return False

    async def evict(self) -> None:
        await self.cancel_launch()
        async with self._lock:
            await self._evict_unlocked(clear_adapter=True)
        await self._broadcast_state()

    async def restart(self) -> dict:
        recipe_id = self.active_recipe_id
        if not recipe_id:
            raise RuntimeError("no active recipe")
        await self.evict()
        return self.start_launch(recipe_id)

    async def _evict_unlocked(self, *, clear_adapter: bool) -> None:
        recipe_id = self.active_recipe_id
        recipe = self.get(recipe_id) if recipe_id else None
        if recipe and recipe.get("node_id"):
            try:
                await self.remote_nodes.evict(str(recipe["node_id"]))
            except Exception as exc:
                self._log("error", f"remote eviction warning: {exc}")
        process = self.process
        self.process = None
        owner, self._process_job = self._process_job, None
        if process is not None and process.returncode is None:
            if owner is not None:
                dispose_process_tree(owner, terminate=True)
            else:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            if not await settle_process(process, timeout_s=10.0):
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await settle_process(process, timeout_s=5.0)
        elif owner is not None:
            dispose_process_tree(owner, terminate=False)
        if self._reader_task:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None
        watcher = self._watcher_task
        self._watcher_task = None
        if watcher and watcher is not asyncio.current_task() and not watcher.done():
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        if clear_adapter and self.host is not None and recipe_id:
            engine = self.host.router.engine
            if self.host.router.inference_runtime_id != "llamacpp":
                try:
                    await engine.stop()
                except Exception:
                    pass
        if recipe_id:
            self._event("info", "Runtime evicted", {"recipe_id": recipe_id})
        self.active_recipe_id = ""
        self.started_at = 0.0

    async def shutdown(self) -> None:
        await self.cancel_launch()
        await self.evict()

    def _poll_process(self) -> None:
        if self.process is not None and self.process.returncode is not None:
            # During readiness probing _launch owns this process. Clearing it
            # here would hide the exit code and make the probe wait to timeout.
            if not self.active_recipe_id:
                return
            code = self.process.returncode
            recipe_id = self.active_recipe_id
            owner, self._process_job = self._process_job, None
            if owner is not None:
                dispose_process_tree(owner, terminate=False)
            self.process = None
            self.active_recipe_id = ""
            self.started_at = 0.0
            self.last_error = f"runtime process exited with code {code}"
            if recipe_id:
                self._record_failure(recipe_id)
                self._event("error", self.last_error, {"recipe_id": recipe_id})
                self._schedule_restart(recipe_id)

    def process_status(self) -> dict:
        self._poll_process()
        process = self.process
        recipe = self.get(self.active_recipe_id) if self.active_recipe_id else None
        return {
            "ready": bool(self.active_recipe_id and (process is None or process.returncode is None)),
            "pid": process.pid if process is not None else None,
            "recipe_id": self.active_recipe_id,
            "recipe_name": recipe.get("name", "") if recipe else "",
            "runtime_id": recipe.get("runtime_id", "") if recipe else "",
            "model": (recipe.get("model") or recipe.get("model_path") or "") if recipe else "",
            "endpoint": recipe.get("endpoint", "") if recipe else "",
            "started_at": self.started_at,
            "uptime_s": round(max(0.0, time.time() - self.started_at), 1) if self.started_at else 0.0,
            "launching_recipe_id": self.launching_recipe_id,
            "last_error": self.last_error,
        }

    def logs_snapshot(self, limit: int = 200) -> dict:
        return {"type": "inference:logs", "items": list(self.logs)[-max(1, min(500, int(limit))):]}

    def _log(self, source: str, line: str, *, recipe_id: str = "") -> None:
        text = str(line or "").rstrip()
        if not text:
            return
        self.logs.append({"ts": time.time(), "source": source, "recipe_id": recipe_id, "line": text[:1600]})

    async def _broadcast_logs(self) -> None:
        try:
            await self.broadcast(self.logs_snapshot(80))
        except Exception:
            pass

    async def _broadcast_state(self) -> None:
        try:
            await self.broadcast(self.list())
        except Exception:
            pass

    def _record_failure(self, recipe_id: str) -> None:
        now = time.time()
        failures = self._failures.setdefault(recipe_id, deque(maxlen=8))
        failures.append(now)
        while failures and failures[0] < now - 300:
            failures.popleft()

    def _blocked(self, recipe_id: str) -> bool:
        failures = self._failures.get(recipe_id) or ()
        return len([stamp for stamp in failures if stamp >= time.time() - 300]) >= 3

    def _failure_status(self, recipe_id: str) -> dict:
        failures = [stamp for stamp in (self._failures.get(recipe_id) or ()) if stamp >= time.time() - 300]
        return {"failures": len(failures), "blocked": len(failures) >= 3, "window_s": 300}

    def _event(self, level: str, message: str, metadata: dict | None = None) -> None:
        if self.event_sink:
            self.event_sink("runtime", level, message, metadata)


__all__ = ["RUNTIME_RECIPE_LAUNCH_JOB", "RuntimeRecipeManager"]
