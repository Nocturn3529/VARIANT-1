"""
Local Whisper speech-to-text sidecar for VARIANT-1.

Speech-to-text via the whisper.cpp `whisper-server` runtime and a user-supplied
ggml model, run as
a local HTTP sidecar. Audio is sent in-memory (multipart POST); VARIANT-1 never
writes the recording to disk (spec 6.2 privacy).

TTS (Kokoro) is the next step. This module is STT only.
"""

import asyncio
import os
import sys

import httpx

from process_tree import (
    CREATE_SUSPENDED,
    OwnedProcessTree,
    dispose_process_tree,
    find_free_tcp_port,
    resume_owned_process_and_reap,
    tcp_port_is_free,
)
from speech.assets import (
    resolve_whisper_binary,
    resolve_whisper_model,
    whisper_drop_dir,
    whisper_runtime_complete,
)

# Suppress the console window when a windowless (packaged) parent spawns this
# console sidecar on Windows. 0 on POSIX.
_NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0


class VoiceUnavailable(RuntimeError):
    pass


class WhisperServer:
    """Owns the whisper-server subprocess and its /inference endpoint."""

    def __init__(self, cfg: dict, app_root: str, data_root: str | None = None):
        self.app_root = app_root
        self.data_root = data_root
        self.host = cfg.get("host", "127.0.0.1")
        self.autostart = cfg.get("autostart", True)
        self.configured_binary = cfg.get("binary", "")
        self.binary = str(resolve_whisper_binary(
            self.configured_binary, app_root=app_root, data_dir=data_root,
        ))
        self.configured_model = cfg.get("model", "")
        self.model = str(resolve_whisper_model(
            self.configured_model, app_root=app_root, data_dir=data_root,
        ))
        self.model_drop_dir = str(whisper_drop_dir(data_root))
        self.threads = int(cfg.get("threads", 0))
        self.language = cfg.get("language", "auto")
        self.extra_args = list(cfg.get("extra_args", []))
        self._wanted_port = int(cfg.get("port", 8081))
        self.port = self._wanted_port
        self.proc = None
        self._proc_job: OwnedProcessTree | None = None
        self.ready = False
        self._start_lock = None
        self._start_task = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def installed(self) -> bool:
        """Both the binary and the model are present on disk."""
        self.binary = str(resolve_whisper_binary(
            self.configured_binary, app_root=self.app_root, data_dir=self.data_root,
        ))
        self.model = str(resolve_whisper_model(
            self.configured_model, app_root=self.app_root, data_dir=self.data_root,
        ))
        return bool(self.binary and whisper_runtime_complete(self.binary)
                    and self.model and os.path.isfile(self.model))

    def _build_args(self) -> list:
        args = [self.binary, "-m", self.model, "--host", self.host, "--port", str(self.port)]
        if self.threads > 0:
            args += ["-t", str(self.threads)]
        args += self.extra_args
        return args

    async def _wait_healthy(self, timeout_s: float) -> bool:
        deadline = asyncio.get_event_loop().time() + timeout_s
        async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
            while asyncio.get_event_loop().time() < deadline:
                if self.proc is not None and self.proc.returncode is not None:
                    raise VoiceUnavailable(f"whisper-server exited early (code {self.proc.returncode})")
                try:
                    r = await client.get(self.base_url + "/")
                    if r.status_code in (200, 404):   # server is up (root may 404)
                        return True
                except Exception:
                    pass
                await asyncio.sleep(0.4)
        return False

    async def start(self, ready_timeout_s: float = 120.0) -> None:
        self.ready = False
        if not self.installed():
            raise VoiceUnavailable(
                f"whisper not installed (need binary {self.binary} + model {self.model})")

        if not self.autostart:
            self.port = self._wanted_port
            if not await self._wait_healthy(min(ready_timeout_s, 10.0)):
                raise VoiceUnavailable(f"no whisper-server reachable at {self.base_url}")
            self.ready = True
            return

        self.port = self._wanted_port if tcp_port_is_free(self.host, self._wanted_port) else find_free_tcp_port(self.host)
        args = self._build_args()
        print(f"[whisper] starting: {' '.join(args)}", flush=True)
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *args,
                # A Windows child must be assigned to its Job Object before
                # whisper-server (or any immediate descendant) can execute.
                creationflags=_NO_WINDOW | (CREATE_SUSPENDED if os.name == "nt" else 0),
                start_new_session=os.name != "nt",
            )
            try:
                self._proc_job = await resume_owned_process_and_reap(
                    self.proc, self._proc_job)
            except Exception as exc:
                await self._stop_owned(asyncio.current_task())
                raise VoiceUnavailable(
                    f"could not own whisper-server process: {exc}") from exc
        except FileNotFoundError as e:
            raise VoiceUnavailable(f"could not launch whisper-server: {e}")
        if not await self._wait_healthy(ready_timeout_s):
            await self.stop()
            raise VoiceUnavailable("whisper-server did not become healthy in time")
        self.ready = True
        print(f"[whisper] ready at {self.base_url} (model: {os.path.basename(self.model)})", flush=True)

    async def ensure_started(self, ready_timeout_s: float = 120.0) -> None:
        """Start on first transcription, coalescing every caller onto one task.

        A transcription task is intentionally cancellable: a newer recording,
        typed message, or explicit stop cancels the waiter.  Starting Whisper is
        shared process lifecycle, though, and must outlive any one waiter.  If
        the startup coroutine is cancelled after spawning the executable, the
        next recording otherwise starts a second server on a fallback port and
        loses the first process reference.  Keep one shielded startup task so
        caller cancellation cannot multiply sidecars.
        """
        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        async with self._start_lock:
            await self._reap_exited_owned_process()
            if self.ready:
                return
            if self._start_task is None or self._start_task.done():
                self._start_task = asyncio.create_task(self.start(ready_timeout_s))
                # Retrieve a background failure even if every transcription
                # waiter is cancelled before startup finishes.
                self._start_task.add_done_callback(self._consume_start_result)
            start_task = self._start_task
        await asyncio.shield(start_task)

    async def _reap_exited_owned_process(self) -> None:
        """Clear one exited owned generation before readiness is trusted."""

        proc = self.proc
        if proc is None:
            if self.autostart and self.ready:
                self.ready = False
            return
        if proc.returncode is None:
            return
        try:
            await proc.wait()
        except Exception:
            pass
        self.proc = None
        self.ready = False
        job = self._proc_job
        self._proc_job = None
        if job is not None:
            try:
                dispose_process_tree(job, terminate=False)
            except Exception:
                pass

    @staticmethod
    def _consume_start_result(task) -> None:
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass

    async def _stop_owned(self, owner_task=None) -> None:
        self.ready = False
        start_task = self._start_task
        self._start_task = None
        current = owner_task
        if start_task is not None and start_task is not current and not start_task.done():
            start_task.cancel()
            try:
                await start_task
            except (asyncio.CancelledError, Exception):
                pass
        proc = self.proc
        job = self._proc_job
        self.proc = None
        self._proc_job = None
        process_error: BaseException | None = None
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=6.0)
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

    async def stop(self) -> None:
        """Serialize startup cancellation and process/job teardown as one generation."""

        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        owner = asyncio.current_task()
        cancellation = None
        async with self._start_lock:
            cleanup = asyncio.create_task(self._stop_owned(owner))
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError as exc:
                    cancellation = cancellation or exc
                    continue
            cleanup.result()
        if cancellation is not None:
            raise cancellation

    async def transcribe(self, wav_bytes: bytes, language: str = None, timeout_s: float = 60.0) -> str:
        """Send 16 kHz mono WAV bytes to /inference and return the transcript text."""
        if not self.ready:
            raise VoiceUnavailable("whisper-server not ready")
        if not wav_bytes:
            raise VoiceUnavailable("no audio captured")
        files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
        data = {"response_format": "json", "temperature": "0",
                "language": (language or self.language or "auto")}
        url = self.base_url + "/inference"
        async with httpx.AsyncClient(timeout=timeout_s, trust_env=False) as client:
            r = await client.post(url, files=files, data=data)
            if r.status_code != 200:
                raise VoiceUnavailable(f"whisper {r.status_code}: {r.text[:160]}")
            try:
                txt = (r.json() or {}).get("text", "")
            except Exception:
                txt = r.text
        return (txt or "").strip()
