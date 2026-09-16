"""Platform-aware discovery and managed installation for inference runtimes.

The primary desktop path downloads and verifies llama.cpp independently of
model weights. Optional Python stacks retain their isolated environment path.
Every operation is a cancellable Work job reported to the Main Deck.

The manager never mutates a system Python installation.  Native installs live
under VARIANT-1's writable data directory; Windows GPU installs live in a bounded
WSL home-directory path because vLLM and SGLang are Linux runtimes.
"""

from __future__ import annotations

import asyncio
from collections import deque
import importlib.util
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any, Awaitable, Callable

from process_tree import (
    OwnedProcessTree,
    attach_process_and_reap,
    dispose_process_tree,
    settle_process,
)
from model_runtime.runtime_catalog import platform_id
from work_fabric.jobs import JobExecutionContext, JobResult
from work_fabric.scope import WorkScope

Broadcast = Callable[[dict], Awaitable[None]]
EventSink = Callable[[str, str, str, dict | None], None]

_NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0
_SUPPORTED = {
    "vllm": {
        "package": "vllm",
        "module": "vllm",
        "platform": "linux",
        "default_port": 8000,
    },
    "sglang": {
        "package": "sglang[all]",
        "module": "sglang",
        "platform": "linux",
        "default_port": 30000,
    },
    "mlx": {
        "package": "mlx-lm",
        "module": "mlx_lm",
        "platform": "macos-arm",
        "default_port": 8080,
    },
}
RUNTIME_INSTALL_JOB = "inference.runtime.install.v1"


def _run_text(args: list[str], timeout: float = 4.0) -> tuple[int, str]:
    """Bounded discovery command. Failures are represented, never raised."""
    try:
        completed = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_NO_WINDOW,
            check=False,
        )
        return int(completed.returncode or 0), (completed.stdout or "").strip()
    except Exception as exc:
        return 1, str(exc)


def _python_in_venv(folder: str) -> str:
    if sys.platform.startswith("win"):
        return os.path.join(folder, "Scripts", "python.exe")
    return os.path.join(folder, "bin", "python")


def _module_available(python: str, module: str) -> bool:
    if not python or not os.path.isfile(python):
        return False
    code, _ = _run_text([
        python,
        "-c",
        f"import importlib.util; raise SystemExit(0 if importlib.util.find_spec({module!r}) else 1)",
    ])
    return code == 0


def _clean_distro(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._ -]", "", str(value or "")).strip()[:100]


class RuntimeInstaller:
    """Discovers install targets and owns cancellable installation jobs."""

    def __init__(
        self,
        data_dir: str,
        broadcast: Broadcast,
        *,
        event_sink: EventSink | None = None,
        router: Any = None,
    ) -> None:
        self.data_dir = os.path.abspath(data_dir)
        self.root = os.path.join(self.data_dir, "runtime")
        self.venvs_root = os.path.join(self.root, "venvs")
        self.broadcast = broadcast
        self.event_sink = event_sink
        self.router = router
        self._work = None
        self._executions: dict[str, JobExecutionContext] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._process_jobs: dict[str, OwnedProcessTree] = {}
        self._target_cache: tuple[float, list[dict]] = (0.0, [])

    def bind_work(self, work) -> None:
        if self._work is work:
            return
        if self._work is not None:
            raise RuntimeError("RuntimeInstaller is already bound to Work Fabric")
        self._work = work
        work.register_job_handler(
            RUNTIME_INSTALL_JOB,
            self._work_job,
            max_concurrency=1,
        )

    # ------------------------------------------------------------------
    # Discovery / doctor
    # ------------------------------------------------------------------
    def managed_env(self, runtime_id: str) -> str:
        return os.path.join(self.venvs_root, f"{runtime_id}-latest")

    def managed_python(self, runtime_id: str) -> str:
        return _python_in_venv(self.managed_env(runtime_id))

    def _native_python(self) -> str:
        names = ("python3", "python") if not sys.platform.startswith("win") else ("python", "py")
        for name in names:
            candidate = shutil.which(name)
            if candidate:
                code, _ = _run_text([candidate, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"])
                if code == 0:
                    return candidate
        # Development venvs are acceptable targets; frozen executables are not.
        if not getattr(sys, "frozen", False) and os.path.isfile(sys.executable):
            return sys.executable
        return ""

    def _wsl_distros(self) -> list[str]:
        wsl = shutil.which("wsl.exe") or shutil.which("wsl")
        if not wsl:
            return []
        code, out = _run_text([wsl, "--list", "--quiet"], timeout=8.0)
        if code != 0:
            return []
        # Windows PowerShell can surface UTF-16 NULs even when subprocess text
        # decoding selected UTF-8. Removing them is safe for distro names.
        return [
            clean for clean in (_clean_distro(line.replace("\x00", "")) for line in out.splitlines())
            if clean
        ]

    def discover_targets(self, *, force: bool = False) -> list[dict]:
        now = time.monotonic()
        cached_at, cached = self._target_cache
        if not force and cached and now - cached_at < 5.0:
            return [dict(item) for item in cached]

        current_platform = platform_id()
        machine = platform.machine().lower()
        python = self._native_python()
        uv = shutil.which("uv") or ""
        docker = shutil.which("docker") or shutil.which("podman") or ""
        configured_binary = str(
            getattr(getattr(self.router, "engine", None), "binary", "") or ""
        )
        llama = self.local_runtime_status()
        targets: list[dict] = [{
            "id": "managed-binary:llamacpp",
            "kind": "managed_binary",
            "runtime_id": "llamacpp",
            "label": "VARIANT-1-managed llama.cpp",
            "detail": llama["binary"] or llama["runtime_root"],
            "available": bool(llama["supported"]),
            # This row describes the managed installation only.  A live
            # packaged fallback is projected by the separate bundled row and
            # by local_runtime_status; it must not make this target pretend a
            # managed build already exists.
            "installed": bool(llama["managed_installed"]),
            "manageable": bool(llama["supported"]),
            "recommended": True,
            "executable": llama["binary"],
            "python": "",
            "installer": "verified upstream release",
            "tag": llama["tag"],
            "backend": llama["backend"] or llama["recommended_backend"],
            "version": llama["version"],
            "active": llama["managed_active"],
            "update_available": llama["update_available"],
            "reason": "" if llama["supported"] else "Managed download requires Windows.",
        }]
        if configured_binary and not llama["managed_active"]:
            targets.append({
                "id": "bundled-llamacpp" if llama["bundled_active"] else "custom-llamacpp",
                "kind": "bundled" if llama["bundled_active"] else "custom",
                "runtime_id": "llamacpp",
                "label": "Packaged llama.cpp fallback" if llama["bundled_active"] else "Custom llama.cpp",
                "detail": configured_binary,
                "available": os.path.isfile(configured_binary),
                "installed": os.path.isfile(configured_binary),
                "manageable": False,
                "recommended": False,
                "executable": configured_binary,
                "python": "",
            })

        for runtime_id, spec in _SUPPORTED.items():
            managed_python = self.managed_python(runtime_id)
            installed = _module_available(managed_python, spec["module"])
            native_supported = (
                (spec["platform"] == "linux" and current_platform == "linux")
                or (
                    spec["platform"] == "macos-arm"
                    and current_platform == "macos"
                    and machine in {"arm64", "aarch64"}
                )
            )
            targets.append({
                "id": f"managed-native:{runtime_id}",
                "kind": "managed_native",
                "runtime_id": runtime_id,
                "label": f"VARIANT-1-managed {runtime_id}",
                "detail": self.managed_env(runtime_id),
                "available": native_supported and bool(python),
                "installed": installed,
                "manageable": native_supported and bool(python),
                "recommended": native_supported,
                "executable": managed_python if installed else "",
                "python": managed_python if installed else python,
                "installer": "uv" if uv else "venv + pip",
                "reason": "" if native_supported else (
                    "Requires Linux" if spec["platform"] == "linux"
                    else "Requires Apple Silicon macOS"
                ),
            })

            if python and _module_available(python, spec["module"]):
                targets.append({
                    "id": f"system-python:{runtime_id}",
                    "kind": "system_python",
                    "runtime_id": runtime_id,
                    "label": f"System {runtime_id}",
                    "detail": python,
                    "available": native_supported,
                    "installed": True,
                    "manageable": False,
                    "recommended": False,
                    "executable": python,
                    "python": python,
                })

        if current_platform == "windows":
            wsl = shutil.which("wsl.exe") or shutil.which("wsl") or ""
            for distro_index, distro in enumerate(self._wsl_distros()):
                home_code, home = _run_text(
                    [wsl, "-d", distro, "--", "bash", "-lc", "printf %s \"$HOME\""],
                    timeout=8.0,
                )
                home = home.strip() if home_code == 0 and home.strip().startswith("/") else ""
                for runtime_id in ("vllm", "sglang"):
                    module = _SUPPORTED[runtime_id]["module"]
                    env = (
                        f"{home}/.local/share/variant1/runtime/venvs/{runtime_id}-latest"
                        if home else
                        f"$HOME/.local/share/variant1/runtime/venvs/{runtime_id}-latest"
                    )
                    shell_env = shlex.quote(env) if home else env
                    command = (
                        f"test -x {shell_env}/bin/python && "
                        f"{shell_env}/bin/python -c {shlex.quote(f'import {module}')}"
                    )
                    code, _ = _run_text([wsl, "-d", distro, "--", "bash", "-lc", command], timeout=8.0)
                    targets.append({
                        "id": f"managed-wsl:{runtime_id}:{distro}",
                        "kind": "managed_wsl",
                        "runtime_id": runtime_id,
                        "label": f"{runtime_id} in WSL · {distro}",
                        "detail": env,
                        "available": bool(home),
                        "installed": code == 0,
                        "manageable": True,
                        "recommended": distro_index == 0,
                        "executable": wsl,
                        "python": f"{env}/bin/python",
                        "distro": distro,
                        "home": home,
                        "installer": "venv + pip in WSL",
                        "reason": "" if home else "Could not resolve the WSL home directory.",
                    })

        if docker:
            for runtime_id in ("vllm", "sglang"):
                targets.append({
                    "id": f"container:{runtime_id}",
                    "kind": "container",
                    "runtime_id": runtime_id,
                    "label": f"{runtime_id} container target",
                    "detail": docker,
                    "available": True,
                    "installed": True,
                    "manageable": False,
                    "recommended": False,
                    "executable": docker,
                    "python": "",
                    "reason": "Container launch requires a user-selected image and GPU policy.",
                })

        self._target_cache = (now, [dict(item) for item in targets])
        return targets

    def target(self, target_id: str) -> dict | None:
        return next((row for row in self.discover_targets(force=True) if row["id"] == target_id), None)

    def recommended_target(self, runtime_id: str, *, install: bool = False) -> dict | None:
        candidates = [
            item for item in self.discover_targets(force=True)
            if item["runtime_id"] == runtime_id and item.get("available")
        ]
        if install:
            candidates = [item for item in candidates if item.get("manageable")]
        else:
            runnable = [item for item in candidates if item.get("kind") != "container"]
            if runnable:
                candidates = runnable
        candidates.sort(key=lambda item: (
            not bool(item.get("installed")),
            not bool(item.get("recommended")),
            item.get("kind") == "container",
        ))
        return candidates[0] if candidates else None

    def doctor(self, runtime_id: str = "") -> dict:
        runtime_id = str(runtime_id or "").strip().lower()
        targets = self.discover_targets(force=True)
        findings: list[dict] = []

        def finding(level: str, check: str, detail: str, fix: str = "") -> None:
            findings.append({"level": level, "check": check, "detail": detail, "fix": fix})

        finding("ok", "Platform", f"{platform.system()} {platform.release()} · {platform.machine()}")
        gpu_tool = shutil.which("nvidia-smi")
        finding(
            "ok" if gpu_tool else "info",
            "NVIDIA runtime",
            gpu_tool or "nvidia-smi was not found",
            "Install an NVIDIA driver when using vLLM or SGLang on CUDA hardware." if not gpu_tool else "",
        )
        python = self._native_python()
        finding(
            "ok" if python else "warn",
            "Python 3.10+",
            python or "No compatible external Python was found",
            "Install Python 3.10+ or use WSL/Docker." if not python else "",
        )
        uv = shutil.which("uv")
        finding("ok" if uv else "info", "uv installer", uv or "Not found; pip fallback will be used")
        wsl = shutil.which("wsl.exe") or shutil.which("wsl")
        if platform_id() == "windows":
            distros = self._wsl_distros()
            finding(
                "ok" if wsl and distros else "warn",
                "WSL2",
                f"{len(distros)} distribution(s): {', '.join(distros)}" if distros else "No usable WSL distribution found",
                "Install WSL2 with a Linux distribution for managed vLLM/SGLang." if not distros else "",
            )

        if runtime_id:
            if runtime_id not in _SUPPORTED and runtime_id != "llamacpp":
                raise ValueError(f"runtime doctor does not know {runtime_id}")
            matches = [item for item in targets if item["runtime_id"] == runtime_id]
            usable = [item for item in matches if item.get("available")]
            installed = [item for item in usable if item.get("installed")]
            finding(
                "ok" if installed else ("info" if usable else "warn"),
                f"{runtime_id} target",
                (
                    f"Installed: {', '.join(item['label'] for item in installed)}"
                    if installed else
                    f"Installable: {', '.join(item['label'] for item in usable)}"
                    if usable else "No compatible target was discovered"
                ),
                "Choose a remote endpoint in Settings." if not usable else "",
            )

        rank = {"warn": 0, "info": 1, "ok": 2}
        summary = min((row["level"] for row in findings), key=lambda level: rank[level], default="ok")
        return {
            "type": "inference:doctor",
            "runtime_id": runtime_id,
            "summary": summary,
            "findings": findings,
            "targets": targets,
            "checked_at": time.time(),
        }

    def local_runtime_status(self) -> dict:
        from model_runtime.llama_runtime import status as llama_status

        engine = getattr(self.router, "engine", None)
        configured_binary = str(getattr(engine, "binary", "") or "")
        app_root = str(getattr(self.router, "app_root", "") or getattr(engine, "app_root", ""))
        return llama_status(
            self.data_dir,
            configured_binary=configured_binary,
            bundled_binary=(os.path.join(app_root, "bin", "llama-server.exe" if sys.platform.startswith("win") else "llama-server") if app_root else ""),
            running_binary=str(getattr(engine, "running_binary", "") or ""),
        )

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------
    def jobs_snapshot(self) -> dict:
        if self._work is None:
            return {"type": "inference:install:jobs", "items": []}
        rows = self._work.jobs.list(kind=RUNTIME_INSTALL_JOB, limit=30)
        return {
            "type": "inference:install:jobs",
            "items": [self._job_projection(row) for row in rows],
        }

    @staticmethod
    def _job_projection(record) -> dict:
        manifest = dict(record.input_manifest or {})
        progress = dict(record.progress or {})
        status = {
            "succeeded": "done",
            "failed": "error",
            "unknown_effect": "error",
        }.get(record.status, record.status)
        return {
            "id": record.job_id,
            "runtime_id": str(manifest.get("runtime_id") or ""),
            "operation": str(manifest.get("operation") or "install"),
            "target_id": str(manifest.get("target_id") or ""),
            "target_label": str(manifest.get("target_label") or ""),
            "status": status,
            "progress": int(progress.get("progress") or (100 if status == "done" else 0)),
            "step": str(progress.get("step") or status.title()),
            "logs": list(progress.get("logs") or ()),
            "error": str(record.error or progress.get("error") or ""),
            "backend": str(progress.get("backend") or manifest.get("backend") or ""),
            "tag": str(progress.get("tag") or manifest.get("tag") or ""),
            "done_bytes": int(progress.get("done_bytes") or 0),
            "total_bytes": int(progress.get("total_bytes") or 0),
            "created_at": record.created_at,
            "started_at": record.started_at or 0.0,
            "finished_at": record.completed_at or 0.0,
        }

    @staticmethod
    def _public_job(job: dict) -> dict:
        return {
            key: value for key, value in job.items()
            if key not in {"task", "process"}
        }

    async def _emit_job(self, job: dict) -> None:
        execution = self._executions.get(str(job.get("id") or ""))
        if execution is not None:
            await asyncio.to_thread(execution.progress, self._public_job(job))
        payload = {"type": "inference:install:job", **self._public_job(job)}
        try:
            await self.broadcast(payload)
        except Exception:
            pass

    def _event(self, level: str, message: str, metadata: dict | None = None) -> None:
        if self.event_sink:
            self.event_sink("installer", level, message, metadata)

    def start(
        self,
        runtime_id: str,
        *,
        operation: str = "install",
        target_id: str = "",
        backend: str = "auto",
    ) -> dict:
        if self._work is None:
            raise RuntimeError("runtime installation requires Work Fabric")
        runtime_id = str(runtime_id or "").strip().lower()
        operation = str(operation or "install").strip().lower()
        if runtime_id not in {*_SUPPORTED, "llamacpp"}:
            raise ValueError("unknown managed inference runtime")
        if operation not in {"install", "repair", "update", "uninstall"}:
            raise ValueError("operation must be install, repair, update, or uninstall")
        if runtime_id == "llamacpp" and operation == "uninstall":
            raise ValueError("the llama.cpp runtime can be replaced or updated, not removed here")
        target = self.target(target_id) if target_id else self.recommended_target(runtime_id, install=True)
        if not target or target.get("runtime_id") != runtime_id:
            raise ValueError(f"no compatible managed installation target for {runtime_id}")
        if not target.get("manageable"):
            raise ValueError("the selected target is discoverable but not managed by VARIANT-1")
        active = self._work.jobs.list(
            kind=RUNTIME_INSTALL_JOB,
            statuses=("queued", "leased", "running", "waiting"),
            limit=100,
        )
        if any(
            str((row.input_manifest or {}).get("target_id") or "") == target["id"]
            for row in active
        ):
            raise RuntimeError("an installation job is already using this target")

        record = self._work.jobs.create(
            RUNTIME_INSTALL_JOB,
            owner_kind="inference_runtime",
            owner_id=str(target["id"]),
            scope=WorkScope(),
            input_manifest={
            "runtime_id": runtime_id,
            "operation": operation,
            "target_id": target["id"],
            "target_label": target["label"],
            "backend": str(backend or "auto"),
            "tag": str(target.get("tag") or ""),
            },
            max_attempts=2,
            retry_policy={
                "on_lease_expiry": "retry",
                "base_delay_s": 2.0,
                "max_delay_s": 10.0,
            },
        )
        return self._job_projection(record)

    async def cancel(self, job_id: str) -> bool:
        job_id = str(job_id or "")
        if self._work is None:
            return False
        record = self._work.jobs.get(job_id)
        if record is None or record.kind != RUNTIME_INSTALL_JOB or record.terminal:
            return False
        self._work.jobs.cancel(job_id, reason="cancelled by user")
        process = self._processes.get(job_id)
        if process is not None and process.returncode is None:
            owner = self._process_jobs.pop(job_id, None)
            if owner is not None:
                dispose_process_tree(owner, terminate=True)
            else:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
        self._work.scheduler.cancel_active(job_id)
        return True

    async def shutdown(self) -> None:
        for job_id, process in list(self._processes.items()):
            if process.returncode is None:
                owner = self._process_jobs.pop(job_id, None)
                if owner is not None:
                    dispose_process_tree(owner, terminate=True)
                else:
                    process.terminate()

    async def _work_job(self, execution: JobExecutionContext) -> JobResult:
        manifest = dict(execution.job.input_manifest or {})
        target = self.target(str(manifest.get("target_id") or ""))
        if target is None:
            raise RuntimeError("installation target is no longer available")
        job = self._job_projection(execution.job)
        job["status"] = "running"
        job["started_at"] = time.time()
        self._executions[execution.job.job_id] = execution
        try:
            await self._run_job(job, target)
        finally:
            self._executions.pop(execution.job.job_id, None)
        return JobResult(progress=self._public_job(job))

    async def _run_job(self, job: dict, target: dict) -> None:
        job["status"] = "running"
        job["started_at"] = time.time()
        await self._emit_job(job)
        self._event("info", f"{job['operation']} started for {job['runtime_id']}", {"job_id": job["id"]})
        try:
            if job["runtime_id"] == "llamacpp":
                await self._run_llama_job(job)
            elif job["operation"] == "uninstall":
                await self._uninstall(job, target)
            else:
                commands = self._install_commands(job["runtime_id"], target, upgrade=job["operation"] in {"repair", "update"})
                total = max(1, len(commands))
                for index, (label, args) in enumerate(commands):
                    job["step"] = label
                    job["progress"] = int((index / total) * 90)
                    await self._emit_job(job)
                    await self._run_command(job, args)
                job["step"] = "Refreshing runtime inventory"
                job["progress"] = 95
                self._target_cache = (0.0, [])
                target_now = self.target(target["id"])
                if not target_now or not target_now.get("installed"):
                    raise RuntimeError("installation command completed but the runtime import check failed")
            job["status"] = "done"
            job["progress"] = 100
            job["step"] = "Complete"
            self._event("ok", f"{job['runtime_id']} {job['operation']} completed", {"job_id": job["id"]})
        except asyncio.CancelledError:
            job["status"] = "cancelled"
            job["step"] = "Cancelled"
            self._event("info", f"{job['runtime_id']} installation cancelled", {"job_id": job["id"]})
            raise
        except Exception as exc:
            job["status"] = "error"
            job["step"] = "Failed"
            job["error"] = str(exc)[:500]
            self._append_log(job, f"ERROR: {exc}")
            self._event("error", f"{job['runtime_id']} {job['operation']} failed: {exc}", {"job_id": job["id"]})
            raise
        finally:
            job["finished_at"] = time.time()
            self._processes.pop(job["id"], None)
            await self._emit_job(job)

    async def _run_llama_job(self, job: dict) -> None:
        from model_runtime.llama_runtime import install_runtime

        execution = self._executions.get(job["id"])
        loop = asyncio.get_running_loop()
        last_emit = 0.0
        transfer = {"label": None, "banked": 0, "asset_total": 0}

        def cancelled() -> bool:
            try:
                return bool(execution and execution.cancellation_requested())
            except Exception:
                return False

        def progress(stage: str, done: int, total: int, label: str) -> None:
            nonlocal last_emit
            match = re.fullmatch(r"(\d+)/(\d+)", label or "")
            index, count = (int(match[1]), max(1, int(match[2]))) if match else (1, 1)
            base = 94 * (index - 1) / count
            if stage == "download":
                if label != transfer["label"]:
                    transfer["banked"] += int(transfer["asset_total"] or 0)
                    transfer["label"] = label
                transfer["asset_total"] = int(total or done)
                plan_done = int(transfer["banked"] + done)
                plan_total = int(transfer["banked"] + (total or 0))
                pct = int(done * 80 / total) if total else 5
                job["step"] = f"Downloading llama.cpp{f' ({label})' if label else ''}"
                job["progress"] = max(int(job.get("progress") or 0), int(base + max(1, min(80, pct)) / count))
                job["done_bytes"] = plan_done
                job["total_bytes"] = plan_total
            elif stage == "extract":
                pct = int(done * 14 / total) if total else 0
                job["step"] = f"Unpacking runtime{f' ({label})' if label else ''}"
                job["progress"] = max(int(job.get("progress") or 0), int(base + min(94, 80 + pct) / count))
            else:
                job["step"] = "Verifying llama-server"
                job["progress"] = max(int(job.get("progress") or 0), int(base + 80 / count) if label else 96)
            now = time.monotonic()
            if execution is not None and (now - last_emit >= 0.25 or (total and done >= total)):
                last_emit = now
                try:
                    execution.progress(self._public_job(job))
                except Exception:
                    pass
                asyncio.run_coroutine_threadsafe(
                    self.broadcast({"type": "inference:install:job", **self._public_job(job)}),
                    loop,
                )

        backend = str(job.get("backend") or "auto")
        result = await asyncio.to_thread(
            install_runtime,
            self.data_dir,
            backend=backend,
            progress=progress,
            cancelled=cancelled,
        )
        job["backend"] = str(result.get("backend") or backend)
        job["tag"] = str(result.get("tag") or "")
        job["step"] = "Activating managed runtime"
        job["progress"] = 98
        if self.router is not None:
            from model_runtime.engine_manager import activate_llama_binary
            await activate_llama_binary(self.router, binary=str(result["binary"]),
                backend=str(result.get("backend") or "auto"), tag=str(result.get("tag") or ""))
        self._target_cache = (0.0, [])

    def _install_commands(self, runtime_id: str, target: dict, *, upgrade: bool) -> list[tuple[str, list[str]]]:
        spec = _SUPPORTED[runtime_id]
        package = spec["package"]
        module = spec["module"]
        if target["kind"] == "managed_native":
            env = self.managed_env(runtime_id)
            os.makedirs(self.venvs_root, exist_ok=True)
            python = self._native_python()
            if not python:
                raise RuntimeError("Python 3.10+ is not available")
            env_python = _python_in_venv(env)
            uv = shutil.which("uv")
            if uv:
                commands = [
                    ("Creating isolated environment", [uv, "venv", env, "--python", python, "--allow-existing"]),
                    ("Installing runtime packages", [uv, "pip", "install", "--python", env_python, *( ["--upgrade"] if upgrade else [] ), package]),
                ]
            else:
                commands = [
                    ("Creating isolated environment", [python, "-m", "venv", env]),
                    ("Updating package installer", [env_python, "-m", "pip", "install", "--upgrade", "pip"]),
                    ("Installing runtime packages", [env_python, "-m", "pip", "install", *( ["--upgrade"] if upgrade else [] ), package]),
                ]
            commands.append(("Verifying runtime import", [env_python, "-c", f"import {module}; print('runtime import ok')"]))
            return commands

        if target["kind"] == "managed_wsl":
            wsl = target["executable"]
            distro = target["distro"]
            env = f"$HOME/.local/share/variant1/runtime/venvs/{runtime_id}-latest"
            install = f"{env}/bin/python -m pip install {'--upgrade ' if upgrade else ''}{shlex.quote(package)}"
            scripts = [
                ("Checking WSL Python", "python3 -c 'import sys; assert sys.version_info >= (3,10)'"),
                ("Creating isolated WSL environment", f"python3 -m venv {env}"),
                ("Updating WSL package installer", f"{env}/bin/python -m pip install --upgrade pip"),
                ("Installing runtime packages", install),
                ("Verifying runtime import", f"{env}/bin/python -c {shlex.quote(f'import {module}; print(\"runtime import ok\")')}")
            ]
            return [(label, [wsl, "-d", distro, "--", "bash", "-lc", script]) for label, script in scripts]

        raise RuntimeError(f"target kind {target['kind']} cannot be installed")

    async def _uninstall(self, job: dict, target: dict) -> None:
        job["step"] = "Removing managed environment"
        job["progress"] = 20
        await self._emit_job(job)
        if target["kind"] == "managed_native":
            root = os.path.realpath(self.venvs_root)
            env = os.path.realpath(self.managed_env(job["runtime_id"]))
            if os.path.commonpath([root, env]) != root or env == root:
                raise RuntimeError("refusing to remove a path outside VARIANT-1's runtime directory")
            if os.path.isdir(env):
                await asyncio.to_thread(shutil.rmtree, env)
        elif target["kind"] == "managed_wsl":
            runtime_id = job["runtime_id"]
            exact = f"$HOME/.local/share/variant1/runtime/venvs/{runtime_id}-latest"
            await self._run_command(job, [
                target["executable"], "-d", target["distro"], "--", "bash", "-lc",
                f"test -d {exact} && rm -rf -- {exact} || true",
            ])
        else:
            raise RuntimeError("only VARIANT-1-managed environments can be removed")
        self._target_cache = (0.0, [])

    def _append_log(self, job: dict, line: str) -> None:
        text = str(line or "").rstrip()
        if not text:
            return
        logs = deque(job.get("logs") or [], maxlen=200)
        logs.append({"ts": time.time(), "line": text[:1200]})
        job["logs"] = list(logs)

    async def _run_command(self, job: dict, args: list[str]) -> None:
        self._append_log(job, "$ " + " ".join(shlex.quote(str(part)) for part in args))
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                creationflags=_NO_WINDOW,
                start_new_session=os.name != "nt",
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"command not found: {args[0]}") from exc
        owner = await attach_process_and_reap(None, process)
        assert owner is not None
        self._processes[job["id"]] = process
        self._process_jobs[job["id"]] = owner
        try:
            assert process.stdout is not None
            async for raw in process.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    self._append_log(job, line)
                    await self._emit_job(job)
            code = await process.wait()
        finally:
            self._processes.pop(job["id"], None)
            owned = self._process_jobs.pop(job["id"], None)
            if owned is not None:
                dispose_process_tree(
                    owned, terminate=process.returncode is None
                )
            if process.returncode is None:
                await settle_process(process, timeout_s=5.0)
        if code != 0:
            raise RuntimeError(f"{os.path.basename(args[0])} exited with code {code}")


__all__ = ["RUNTIME_INSTALL_JOB", "RuntimeInstaller"]
