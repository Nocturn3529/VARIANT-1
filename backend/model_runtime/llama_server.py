"""Local llama-server process management."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys

import httpx

from process_tree import (
    OwnedProcessTree,
    attach_process_and_reap,
    dispose_process_tree,
    find_free_tcp_port,
    tcp_port_is_free,
)

_NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0

def _default_llama_relpath() -> str:
    """Packaged/default llama-server path for the current OS."""
    name = "llama-server.exe" if sys.platform.startswith("win") else "llama-server"
    return "bin/" + name


def resolve_llama_binary_relpath(configured: object | None = None) -> str:
    """Map a packaged/default binary pin to the host OS.

    Accepts ``bin/llama-server``, ``bin/llama-server.exe``, or empty.
    Absolute paths and unrelated names are returned unchanged (normpath).
    """
    raw = str(configured or "").strip()
    if not raw:
        return _default_llama_relpath()
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    norm = raw.replace("\\", "/")
    base = norm.split("/")[-1].lower()
    if base in {"llama-server", "llama-server.exe"}:
        return _default_llama_relpath()
    return norm

# LLMScheduler currently admits one local generation at a time. Keep the
# server's KV allocation aligned instead of accepting its auto slot count.
_LOCAL_PARALLEL_SLOTS = 1
_FLAG_RE = re.compile(r"(?<![\w-])(--[a-z0-9][a-z0-9-]*|-[a-z][a-z0-9]*)", re.I)
_RUNTIME_FLAG_CACHE: dict[tuple[str, int, int], frozenset[str] | None] = {}


class LocalEngineError(RuntimeError):
    """Raised when the managed local llama.cpp runtime is unavailable."""


def _abs(app_root: str, p: str) -> str:
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(app_root, p))


def resolve_user_model_path(data_dir: str, value: str) -> str:
    """Resolve relative GGUF/projector pins inside the writable user model root."""
    path = str(value or "").strip()
    if not path:
        return ""
    if os.path.isabs(path):
        return os.path.normpath(path)
    normalized = path.replace("\\", "/").lstrip("/")
    lowered = normalized.lower()
    if lowered.startswith("models/user/"):
        normalized = normalized[len("models/user/"):]
    elif lowered.startswith("models/"):
        normalized = normalized[len("models/"):]
    root = os.path.realpath(os.path.join(data_dir, "models", "user"))
    candidate = os.path.realpath(os.path.join(root, normalized))
    try:
        if os.path.commonpath([root, candidate]) != root:
            raise ValueError("relative model path escapes models/user")
    except ValueError as exc:
        raise ValueError("relative model path escapes models/user") from exc
    return candidate


def _flash_attn_mode(value) -> str:
    """Normalize legacy booleans and llama.cpp's current tri-state option."""
    if isinstance(value, bool):
        return "on" if value else "off"
    mode = str(value or "auto").strip().lower()
    return mode if mode in {"on", "off", "auto"} else "auto"


class LlamaServer:
    """Owns the llama-server subprocess and its HTTP endpoint."""

    runtime_id = "llamacpp"
    display_name = "llama.cpp"
    managed = True
    supports_llama_extensions = True
    api_model = ""


    def __init__(self, cfg: dict, app_root: str, data_dir: str | None = None):
        self.app_root = app_root
        self.data_dir = data_dir or app_root
        self.host = cfg.get("host", "127.0.0.1")
        self.autostart = cfg.get("autostart", True)
        self.binary = _abs(app_root, resolve_llama_binary_relpath(cfg.get("binary")))
        _m = cfg.get("model", "")
        self.model = resolve_user_model_path(self.data_dir, _m) if _m else ""
        _mp = cfg.get("mmproj", "")
        self.mmproj = resolve_user_model_path(self.data_dir, _mp) if _mp else ""
        raw_ctx = cfg.get("ctx_size", 8192)
        self.ctx_auto = str(raw_ctx or "").strip().lower() == "auto" or raw_ctx == 0
        self.requested_ctx_size = 0 if self.ctx_auto else max(2048, int(raw_ctx))
        self.ctx_size = self.requested_ctx_size or 8192
        self.context_size_source = (
            "configured" if not self.ctx_auto else "unverified_auto_default"
        )
        raw_ngl = cfg.get("n_gpu_layers", "auto")
        self.gpu_layers_auto = str(raw_ngl or "").strip().lower() == "auto"
        self.n_gpu_layers = 0 if self.gpu_layers_auto else int(raw_ngl)
        self.threads = int(cfg.get("threads", 0))
        self.backend = str(cfg.get("backend", "auto") or "auto").strip().lower()
        self.device = str(cfg.get("device", "") or "").strip()
        # VRAM/throughput tuning. The exact binary's help surface is probed at
        # startup, so unsupported options are omitted independently.
        #  - flash_attn: FlashAttention — less KV-cache VRAM + faster prefill on
        #    Ampere+ (RTX 3060 qualifies). Required for V-cache quantization below.
        #  - cache_type_k/v: quantize the KV cache (q8_0 ≈ half the f16 footprint),
        #    which is what lets a 12GB card hold a larger context. Omitted only when
        #    Flash Attention is explicitly off (quantized V-cache requires it).
        #  - ubatch: physical batch for prompt processing; lower trims the compute
        #    buffer's VRAM on a tight card (0 = leave the llama.cpp default).
        self.parallel = _LOCAL_PARALLEL_SLOTS
        self.requested_parallel = max(
            1, int(cfg.get("parallel", _LOCAL_PARALLEL_SLOTS)))
        self.flash_attn = _flash_attn_mode(cfg.get("flash_attn", "auto"))
        self.cache_type_k = str(cfg.get("cache_type_k", "q8_0") or "")
        self.cache_type_v = str(cfg.get("cache_type_v", "q8_0") or "")
        self.ubatch = int(cfg.get("ubatch", 0))
        self.cache_prompt = bool(cfg.get("cache_prompt", True))
        self.cache_reuse = max(0, int(cfg.get("cache_reuse", 256)))
        self.cache_ram_mb = max(0, int(cfg.get("cache_ram_mb", 512)))
        self.ctx_checkpoints = max(0, int(cfg.get("ctx_checkpoints", 8)))
        self.sleep_idle_seconds = max(0, int(cfg.get("sleep_idle_seconds", 1800)))
        self.fit_target_mb = max(0, int(cfg.get("fit_target_mb", 1024)))
        self.fit_min_ctx = max(2048, int(cfg.get("fit_min_ctx", 4096)))
        self.metrics = bool(cfg.get("metrics", True))
        self.extra_args = list(cfg.get("extra_args", []))
        # 0 disables the model's "thinking"/reasoning pass (thinking models like
        # Gemma-4 otherwise spend the whole token budget reasoning and emit no
        # answer). Set null in config to leave the model default.
        self.reasoning_budget = cfg.get("reasoning_budget", 0)
        self._runtime_probe_complete = False
        self._runtime_flags: frozenset[str] | None = None
        self._runtime_probe_error = ""
        self._startup_degraded_options: list[str] = []
        self._effective_args: list[str] = []
        self.supports_reasoning = True  # detected from /props after the model loads
        self._running_reasoning_budget = None   # what the LIVE process was launched with
        self._wanted_port = int(cfg.get("port", 8080))
        self.port = self._wanted_port
        self.proc = None
        self._proc_job: OwnedProcessTree | None = None
        self.running_binary = ""
        self.ready = False

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def request_adapter(self) -> str:
        return "llama_cpp.chat_completions"

    def _supports_flag(self, *names: str) -> bool:
        """Unknown runtime preserves compatibility; a successful probe is exact."""
        if not self._runtime_probe_complete or self._runtime_flags is None:
            return True
        return any(name in self._runtime_flags for name in names)

    def _supported_flag(self, *names: str) -> str | None:
        """Return the exact advertised spelling, preferring names in order.

        Several llama.cpp releases expose only a short or only a long alias.
        Treating either alias as support and then emitting a hard-coded spelling
        defeats the capability probe on precisely those runtimes.
        """
        if not names:
            return None
        if not self._runtime_probe_complete or self._runtime_flags is None:
            return names[0]
        return next((name for name in names if name in self._runtime_flags), None)

    def _probe_runtime_flags_sync(self) -> tuple[frozenset[str] | None, str]:
        """Read ``--help`` from the exact binary without involving a shell."""
        try:
            stat = os.stat(self.binary)
            key = (
                os.path.normcase(os.path.abspath(self.binary)),
                stat.st_mtime_ns,
                stat.st_size,
            )
        except OSError as exc:
            return None, str(exc)
        if key in _RUNTIME_FLAG_CACHE:
            return _RUNTIME_FLAG_CACHE[key], ""
        try:
            result = subprocess.run(
                [self.binary, "--help"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                creationflags=_NO_WINDOW,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return None, str(exc)
        output = f"{result.stdout}\n{result.stderr}"
        flags = frozenset(_FLAG_RE.findall(output))
        if "--host" not in flags or "--port" not in flags:
            return None, f"unrecognized help output (exit={result.returncode})"
        _RUNTIME_FLAG_CACHE[key] = flags
        return flags, ""

    async def _probe_runtime_flags(self) -> None:
        if self._runtime_probe_complete:
            return
        flags, error = await asyncio.to_thread(self._probe_runtime_flags_sync)
        self._runtime_flags = flags
        self._runtime_probe_error = error
        self._runtime_probe_complete = True
        if flags is None:
            print(f"[llama] runtime flag probe unavailable: {error}", flush=True)
        else:
            print(f"[llama] runtime flag probe: {len(flags)} options", flush=True)

    def _unsupported_requested_options(self) -> list[str]:
        """Explain requested settings that the probed runtime cannot represent."""
        omitted: list[str] = []
        if self.requested_parallel != self.parallel:
            omitted.append("parallel_clamped_to_scheduler_capacity")
        if self._runtime_flags is None:
            return omitted

        def require(label: str, enabled: bool, *flags: str) -> None:
            if enabled and not self._supports_flag(*flags):
                omitted.append(label)

        require("parallel", True, "--parallel", "-np")
        require(
            "device",
            self.backend in {"cpu", "cuda", "vulkan"} or bool(self.device),
            "--device", "-dev",
        )
        flash_supported = self._supports_flag("--flash-attn") or (
            self.flash_attn == "on" and self._supports_flag("-fa"))
        if not flash_supported:
            omitted.append("flash_attn")
        cache_enabled = self.flash_attn != "off"
        require("cache_type_k", cache_enabled and bool(self.cache_type_k),
                "--cache-type-k", "-ctk")
        require("cache_type_v", cache_enabled and bool(self.cache_type_v),
                "--cache-type-v", "-ctv")
        require("ubatch", self.ubatch > 0, "--ubatch-size", "-ub")
        require("cache_prompt", True,
                "--cache-prompt" if self.cache_prompt else "--no-cache-prompt")
        require("cache_reuse", self.cache_reuse > 0, "--cache-reuse")
        require("cache_ram", self.cache_ram_mb > 0, "--cache-ram", "-cram")
        require("ctx_checkpoints", self.ctx_checkpoints > 0,
                "--ctx-checkpoints", "-ctxcp")
        require("sleep_idle_seconds", self.sleep_idle_seconds > 0,
                "--sleep-idle-seconds")
        require("fit", self.ctx_auto, "--fit", "-fit")
        require("fit_target", self.ctx_auto and self.fit_target_mb > 0,
                "--fit-target", "-fitt")
        require("fit_ctx", self.ctx_auto and self.fit_min_ctx > 0,
                "--fit-ctx", "-fitc")
        require("reasoning_budget", self.reasoning_budget is not None,
                "--reasoning-budget")
        require("metrics", self.metrics, "--metrics")
        return omitted

    @staticmethod
    def _option_value(args: list[str], *flags: str):
        for flag in flags:
            if flag not in args:
                continue
            index = args.index(flag)
            return args[index + 1] if index + 1 < len(args) else True
        return None

    def _effective_launch_options(self) -> dict:
        args = self._effective_args
        parallel = self._option_value(args, "--parallel", "-np")
        try:
            parallel = int(parallel) if parallel is not None else None
        except (TypeError, ValueError):
            parallel = None
        flash = self._option_value(args, "--flash-attn")
        if flash is None and "-fa" in args:
            flash = "on"
        return {
            "parallel_slots": parallel,
            "flash_attn": flash,
            "cache_type_k": self._option_value(args, "--cache-type-k", "-ctk"),
            "cache_type_v": self._option_value(args, "--cache-type-v", "-ctv"),
            "metrics": "--metrics" in args,
        }

    def _build_args(self, *, core_only: bool = False) -> list:
        args = [
            self.binary,
            "-m", self.model,
            "--host", self.host,
            "--port", str(self.port),
        ]
        parallel_flag = self._supported_flag("--parallel", "-np")
        if not core_only and parallel_flag:
            args += [parallel_flag, str(self.parallel)]
        if self.backend == "cpu":
            args += ["-ngl", "0"]
        elif not self.gpu_layers_auto:
            args += ["-ngl", str(self.n_gpu_layers)]
        if not self.ctx_auto:
            args += ["-c", str(self.requested_ctx_size)]
        device_flag = self._supported_flag("--device", "-dev")
        if not core_only and device_flag:
            if self.backend == "cpu":
                args += [device_flag, "none"]
            elif self.device:
                args += [device_flag, self.device]
            elif self.backend == "cuda":
                args += [device_flag, "CUDA0"]
            elif self.backend == "vulkan":
                args += [device_flag, "Vulkan0"]
        if self.mmproj and os.path.isfile(self.mmproj):
            args += ["--mmproj", self.mmproj]
        if self.threads > 0:
            args += ["-t", str(self.threads)]
        if not core_only:
            flash_flag = self._supported_flag("--flash-attn", "-fa")
            if flash_flag == "--flash-attn":
                args += [flash_flag, self.flash_attn]
            elif flash_flag == "-fa" and self.flash_attn == "on":
                args += [flash_flag]
            # Quantized V-cache needs Flash Attention. ``auto`` lets llama.cpp
            # make the hardware decision while preserving the requested type.
            if self.flash_attn != "off":
                cache_k_flag = self._supported_flag("--cache-type-k", "-ctk")
                cache_v_flag = self._supported_flag("--cache-type-v", "-ctv")
                if self.cache_type_k and cache_k_flag:
                    args += [cache_k_flag, self.cache_type_k]
                if self.cache_type_v and cache_v_flag:
                    args += [cache_v_flag, self.cache_type_v]
            ubatch_flag = self._supported_flag("--ubatch-size", "-ub")
            if self.ubatch > 0 and ubatch_flag:
                args += [ubatch_flag, str(self.ubatch)]
            cache_flag = "--cache-prompt" if self.cache_prompt else "--no-cache-prompt"
            if self._supports_flag(cache_flag):
                args += [cache_flag]
            if self.cache_reuse > 0 and self._supports_flag("--cache-reuse"):
                args += ["--cache-reuse", str(self.cache_reuse)]
            cache_ram_flag = self._supported_flag("--cache-ram", "-cram")
            if self.cache_ram_mb > 0 and cache_ram_flag:
                args += [cache_ram_flag, str(self.cache_ram_mb)]
            checkpoints_flag = self._supported_flag("--ctx-checkpoints", "-ctxcp")
            if self.ctx_checkpoints > 0 and checkpoints_flag:
                args += [checkpoints_flag, str(self.ctx_checkpoints)]
            if self.sleep_idle_seconds > 0 and self._supports_flag("--sleep-idle-seconds"):
                args += ["--sleep-idle-seconds", str(self.sleep_idle_seconds)]
            fit_flag = self._supported_flag("--fit", "-fit")
            if self.ctx_auto and fit_flag:
                args += [fit_flag, "on"]
                fit_target_flag = self._supported_flag("--fit-target", "-fitt")
                fit_ctx_flag = self._supported_flag("--fit-ctx", "-fitc")
                if fit_target_flag:
                    args += [fit_target_flag, str(self.fit_target_mb)]
                if fit_ctx_flag:
                    args += [fit_ctx_flag, str(self.fit_min_ctx)]
            if self.reasoning_budget is not None and self._supports_flag("--reasoning-budget"):
                args += ["--reasoning-budget", str(self.reasoning_budget)]
            if self.metrics and self._supports_flag("--metrics"):
                args += ["--metrics"]
            # The first launch always honors explicit developer overrides. The
            # unprobeable-runtime retry must be genuinely core-only, otherwise
            # an unsupported extra flag deterministically defeats the fallback.
            args += self.extra_args
        return args

    def preflight(self) -> None:
        """Validate the binary + model exist before spawning."""
        if not os.path.isfile(self.binary):
            raise LocalEngineError(f"llama-server binary not found: {self.binary}")
        if not self.model or not os.path.isfile(self.model):
            raise LocalEngineError(f"model file not found: {self.model}")

    def poll_process(self) -> bool:
        """Detect a dead managed process and clear ``ready``.

        llama-server can exit after becoming healthy (GPU reset, OOM, crash)
        while VARIANT-1 still thinks the engine is up. Call this before routing
        local inference and from ``engine_ready``.
        """
        proc = self.proc
        if proc is None:
            return bool(self.ready)
        code = proc.returncode
        if code is None:
            return bool(self.ready)
        if self.ready:
            print(
                f"[llama] process exited code={code}; marking not ready "
                f"(was {self.base_url})",
                flush=True,
            )
        self.ready = False
        job = self._proc_job
        self._proc_job = None
        if job is not None:
            try:
                # The managed process has already exited, so closing the empty
                # Job is sufficient and avoids a redundant termination call.
                dispose_process_tree(job, terminate=False)
            except Exception as exc:
                print(f"[llama] failed to close exited-process Job Object: {exc}",
                      flush=True)
        self.proc = None
        self.running_binary = ""
        return False

    def runtime_status(self) -> dict:
        self.poll_process()
        return {
            "runtime_id": self.runtime_id,
            "display_name": self.display_name,
            "managed": True,
            "backend": self.backend,
            "device": self.device,
            "context": "auto" if self.ctx_auto else self.requested_ctx_size,
            "effective_context": self.ctx_size,
            "effective_context_source": self.context_size_source,
            "gpu_layers": "auto" if self.gpu_layers_auto else self.n_gpu_layers,
            "parallel_slots": self.parallel,
            "requested_parallel_slots": self.requested_parallel,
            "flash_attn": self.flash_attn,
            "cache_prompt": self.cache_prompt,
            "cache_reuse": self.cache_reuse,
            "cache_ram_mb": self.cache_ram_mb,
            "sleep_idle_seconds": self.sleep_idle_seconds,
            "metrics": self.metrics,
            "runtime_flags_probed": (
                self._runtime_probe_complete and self._runtime_flags is not None),
            "degraded_options": list(self._startup_degraded_options),
            "effective_launch": self._effective_launch_options(),
            "ready": self.ready,
            "pid": (self.proc.pid if self.proc is not None else None),
        }

    async def _wait_healthy(self, timeout_s: float) -> bool:
        """Poll GET /health until llama-server reports the model is loaded."""
        deadline = asyncio.get_event_loop().time() + timeout_s
        fails = 0
        async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
            while asyncio.get_event_loop().time() < deadline:
                # If we own the process and it died, stop waiting.
                if self.proc is not None and self.proc.returncode is not None:
                    code = self.proc.returncode
                    print(f"[llama] exited code={code} during health wait", flush=True)
                    raise LocalEngineError(
                        f"llama-server exited early (code {code})"
                    )
                try:
                    r = await client.get(f"{self.base_url}/health")
                    if r.status_code == 200:
                        body = r.json()
                        if body.get("status") == "ok":
                            if fails:
                                print(
                                    f"[llama] health ok after consecutive={fails} "
                                    f"url={self.base_url}",
                                    flush=True,
                                )
                            return True
                    fails += 1
                except Exception:
                    fails += 1
                if fails in (1, 5, 10, 20) or (fails > 0 and fails % 40 == 0):
                    print(
                        f"[llama] health fail consecutive={fails} "
                        f"url={self.base_url}",
                        flush=True,
                    )
                await asyncio.sleep(0.5)
        print(
            f"[llama] health timeout after {timeout_s:.0f}s "
            f"consecutive={fails} url={self.base_url}",
            flush=True,
        )
        return False

    async def start(self, ready_timeout_s: float = 180.0) -> None:
        """Spawn llama-server (or attach if autostart is off) and wait for ready."""
        # ``start`` is an ownership boundary, not a blind spawn primitive. A
        # prior request may have marked the endpoint unready while its managed
        # process is still alive (for example after a transport failure). Never
        # overwrite that process/job handle and leak a second GPU owner.
        if self.proc is not None or self._proc_job is not None:
            await self.stop()
        self.ready = False

        if not self.autostart:
            # Attach mode: a llama-server is expected to be running already.
            self.port = self._wanted_port
            if not await self._wait_healthy(min(ready_timeout_s, 15.0)):
                raise LocalEngineError(
                    f"no llama-server reachable at {self.base_url} (autostart=false)"
                )
            self.ready = True
            return

        self.preflight()
        await self._probe_runtime_flags()
        self._startup_degraded_options = self._unsupported_requested_options()
        if self._startup_degraded_options:
            print(
                "[llama] launch settings adjusted: "
                + ", ".join(self._startup_degraded_options),
                flush=True,
            )
        core_only = False

        # Choose a port: honour the configured one if free, else pick another.
        self.port = self._wanted_port if tcp_port_is_free(self.host, self._wanted_port) \
            else find_free_tcp_port(self.host)

        async def _spawn_and_wait() -> bool:
            args = self._build_args(core_only=core_only)
            self._effective_args = list(args[1:])
            print(f"[llama] starting: {' '.join(args)}", flush=True)
            try:
                # Inherit stdout/stderr so llama-server logs flow into the backend
                # log (which Electron captures).
                self.proc = await asyncio.create_subprocess_exec(
                    *args,
                    creationflags=_NO_WINDOW,
                    start_new_session=os.name != "nt",
                )
                self.running_binary = self.binary
                try:
                    self._proc_job = await attach_process_and_reap(
                        self._proc_job, self.proc)
                except Exception as exc:
                    # attach_process_and_reap has already killed and settled the
                    # unowned child. Do not retain a dead process across retry.
                    self.proc = None
                    self.running_binary = ""
                    raise LocalEngineError(
                        f"could not own llama-server process: {exc}") from exc
            except FileNotFoundError as e:
                raise LocalEngineError(f"could not launch llama-server: {e}")
            try:
                return await self._wait_healthy(ready_timeout_s)
            except LocalEngineError:
                return False  # exited early; caller may use a bounded fallback

        healthy = await _spawn_and_wait()
        if not healthy and self._runtime_flags is None:
            # Only an unprobeable legacy runtime gets the compatibility retry.
            # The degradation is local to this start, never sticky across models.
            print(
                "[llama] startup failed with unknown flag support; "
                "retrying core options",
                flush=True,
            )
            await self.stop()
            core_only = True
            self._startup_degraded_options.append("optional_flags_unprobed")
            self.port = self._wanted_port if tcp_port_is_free(self.host, self._wanted_port) else find_free_tcp_port(self.host)
            healthy = await _spawn_and_wait()
        if not healthy and self.mmproj:
            # The vision projector may be incompatible with this model (e.g. a 12B
            # mmproj left paired with an E4B model) — that makes llama-server fail to
            # load entirely. Retry once text-only so the user gets a WORKING engine
            # instead of a dead one; vision just stays off until a matching projector
            # is supplied. restart() re-sets self.mmproj for the next model, so this
            # only drops the projector that actually failed.
            print("[llama] startup failed; retrying text-only (dropping the vision projector)", flush=True)
            await self.stop()
            self.mmproj = ""
            self.port = self._wanted_port if tcp_port_is_free(self.host, self._wanted_port) else find_free_tcp_port(self.host)
            healthy = await _spawn_and_wait()
        if not healthy:
            await self.stop()
            raise LocalEngineError("llama-server did not become healthy in time")

        self.ready = True
        self._running_reasoning_budget = (
            self.reasoning_budget
            if "--reasoning-budget" in self._effective_args
            else None
        )
        await self._detect_reasoning()
        print(f"[llama] ready at {self.base_url} (model: {os.path.basename(self.model)})",
              flush=True)

    async def _detect_reasoning(self) -> None:
        """Heuristic: does this model's chat template support thinking/reasoning?
        Scans GET /props for reasoning markers. Defaults to True if unknown so the
        user still gets the toggle."""
        detected_context = 0
        try:
            async with httpx.AsyncClient(timeout=4.0, trust_env=False) as client:
                r = await client.get(f"{self.base_url}/props")
            if r.status_code == 200:
                props = r.json()
                blob = json.dumps(props).lower()
                self.supports_reasoning = any(
                    k in blob for k in ("reasoning", "thinking", "<think", "enable_thinking", "channel"))
                if self.ctx_auto:
                    detected_context = self._loaded_context_size(props)
            else:
                self.supports_reasoning = True
        except Exception:
            self.supports_reasoning = True
        if self.ctx_auto:
            if detected_context:
                self.ctx_size = detected_context
                self.context_size_source = "llama_props_loaded_slot"
            else:
                self.ctx_size = max(2048, int(self.fit_min_ctx))
                self.context_size_source = "conservative_fit_floor"
                print(
                    "[llama] fitted context unavailable from /props; "
                    f"budgeting conservatively at {self.ctx_size}",
                    flush=True,
                )

    @staticmethod
    def _loaded_context_size(props) -> int:
        """Read only known loaded/default generation-setting paths."""

        if not isinstance(props, dict):
            return 0
        candidates = [
            props.get("default_generation_settings"),
            props.get("generation_settings"),
            props.get("slot"),
        ]
        slots = props.get("slots")
        if isinstance(slots, list) and slots:
            candidates.append(slots[0])
        candidates.append(props)
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            for key in ("n_ctx", "ctx_size", "context_size"):
                try:
                    value = int(candidate.get(key) or 0)
                except (TypeError, ValueError):
                    continue
                if value >= 2048:
                    return value
        return 0

    async def count_prompt_tokens(self, template_payload: dict) -> int | None:
        """Count the input through llama.cpp's native chat-token endpoint.

        ``template_payload`` is the chat-template-relevant subset of the real
        OpenAI request (messages, provider-native tools, and template kwargs).
        Current builds expose ``/v1/chat/completions/input_tokens``, which owns
        chat-template and multimodal accounting. Older builds fall back to
        ``/apply-template`` + ``/tokenize``; that compatibility path accounts
        for textual template/schema framing but may differ from native media
        token accounting. Callers retain one final flattened-text fallback.
        """
        if not self.ready or not isinstance(template_payload, dict):
            return None
        messages = template_payload.get("messages")
        if not isinstance(messages, list) or not messages:
            return None
        try:
            async with httpx.AsyncClient(timeout=8.0, trust_env=False) as client:
                try:
                    native = await client.post(
                        f"{self.base_url}/v1/chat/completions/input_tokens",
                        json=template_payload,
                    )
                except Exception:
                    native = None
                if native is not None and native.status_code == 200:
                    try:
                        native_body = native.json()
                    except Exception:
                        native_body = None
                    input_tokens = (
                        native_body.get("input_tokens")
                        if isinstance(native_body, dict)
                        else None
                    )
                    if (
                        isinstance(input_tokens, int)
                        and not isinstance(input_tokens, bool)
                        and input_tokens >= 0
                    ):
                        return input_tokens

                rendered = await client.post(
                    f"{self.base_url}/apply-template",
                    json=template_payload,
                )
                if rendered.status_code != 200:
                    return None
                prompt = rendered.json().get("prompt")
                if not isinstance(prompt, str):
                    return None
                tokenized = await client.post(
                    f"{self.base_url}/tokenize",
                    json={"content": prompt},
                )
            if tokenized.status_code == 200:
                toks = tokenized.json().get("tokens")
                if isinstance(toks, list):
                    return len(toks)
        except Exception:
            pass
        return None

    async def count_tokens(self, text: str) -> int | None:
        """EXACT token count via llama-server's /tokenize — the chars-based
        estimate drifts badly on URL/markdown-heavy text (measured 25-35% under),
        and a wrong answer here means either premature compaction or a reply that
        dies at the end of the context window. None on any failure (caller falls
        back to the heuristic)."""
        if not self.ready or not text:
            return None
        try:
            async with httpx.AsyncClient(timeout=8.0, trust_env=False) as client:
                r = await client.post(f"{self.base_url}/tokenize",
                                      json={"content": text})
            if r.status_code == 200:
                toks = r.json().get("tokens")
                if isinstance(toks, list):
                    return len(toks)
        except Exception:
            pass
        return None

    async def stop(self) -> None:
        self.ready = False
        code = None
        proc = self.proc
        job = self._proc_job
        # Clear public ownership state regardless of which cleanup operation
        # fails. The local references below remain available for final cleanup.
        self.proc = None
        self._proc_job = None
        self.running_binary = ""
        process_error: BaseException | None = None
        if proc is not None:
            code = proc.returncode
            if proc.returncode is None:
                try:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=8.0)
                    except asyncio.TimeoutError:
                        proc.kill()
                        try:
                            await asyncio.wait_for(proc.wait(), timeout=2.0)
                        except BaseException as exc:
                            process_error = exc
                except ProcessLookupError:
                    pass
                except BaseException as exc:
                    process_error = exc
                code = proc.returncode
            print(f"[llama] stop exit_code={code}", flush=True)
        job_error: BaseException | None = None
        if job is not None:
            try:
                dispose_process_tree(job, terminate=True)
            except BaseException as exc:
                job_error = exc
        if process_error is not None:
            if job_error is not None:
                add_note = getattr(process_error, "add_note", None)
                if callable(add_note):
                    add_note(f"Job Object cleanup also failed: {job_error}")
            raise process_error
        if job_error is not None:
            raise job_error

    def validate_selection(self, model_path: str | None, mmproj_path: str | None) -> tuple[str, str]:
        """Resolve a candidate without stopping the currently usable runtime."""
        selected = resolve_user_model_path(self.data_dir, model_path) if model_path else self.model
        projector = resolve_user_model_path(self.data_dir, mmproj_path) if mmproj_path else ""
        for value in (selected, projector):
            if value and (not value.lower().endswith(".gguf") or not os.path.isfile(value)):
                raise LocalEngineError("Select an existing GGUF model/projector file before switching")
        return selected, projector

    async def restart(self, model_path: str = None, mmproj_path: str = None, ready_timeout_s: float = 180.0) -> None:
        """Restart the engine, optionally with a new model + vision projector (spec 3.2)."""
        selected, projector = self.validate_selection(model_path, mmproj_path)
        await self.stop()
        self.model = selected
        self.mmproj = projector
        await self.start(ready_timeout_s)


# Named internal completion profiles live in ``llm_profiles``.


