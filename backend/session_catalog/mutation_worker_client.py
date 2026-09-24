"""Same-user disposable process transport for mutation candidates."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from typing import Any, Awaitable, Callable

from kernel_runtime.job_object import KernelJobObject
from kernel_runtime.worker_path import packaged_kernel_executable
from process_tree import CREATE_SUSPENDED, resume_owned_process_and_reap

from .mutation_contracts import (
    MutationWorkerError,
    WorkerLimits,
    stable_json,
)


WORKER_PROTOCOL = "variant1.astb.mutation-worker.v2"

class MutationWorkerClient:
    """Runs one candidate in one parent-owned, same-user disposable process.

    The process and Job Object provide lifecycle cleanup and resource bounds,
    not a security boundary. Once the user enables mutation for a chat,
    candidate Python has the same operating-system authority as VARIANT-1.
    """

    def __init__(
        self,
        root: str,
        *,
        worker_executable: str = "",
        limits: WorkerLimits | None = None,
    ):
        self.root = os.path.abspath(root)
        self.worker_executable = (
            os.path.abspath(worker_executable) if worker_executable else ""
        )
        self.limits = limits or WorkerLimits()
        os.makedirs(self.root, exist_ok=True)

    def set_worker_executable(self, path: str) -> None:
        self.worker_executable = (
            os.path.abspath(path) if str(path or "").strip() else ""
        )

    def _command(self) -> list[str]:
        if self.worker_executable:
            if not os.path.isfile(self.worker_executable):
                raise MutationWorkerError(
                    "worker_unavailable", "configured Variant1Kernel executable is missing"
                )
            return [self.worker_executable, "--mutation-worker"]
        if getattr(sys, "frozen", False):
            candidate = packaged_kernel_executable()
            if not os.path.isfile(candidate):
                raise MutationWorkerError(
                    "worker_unavailable", "packaged mutation worker is missing"
                )
            return [candidate, "--mutation-worker"]
        script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "kernel_runtime", "mutation_worker.py",
        )
        return [sys.executable, script]

    @staticmethod
    def _environment(root: str, gate: str, token: str) -> dict[str, str]:
        result = dict(os.environ)
        result.pop("PYTHONNOUSERSITE", None)
        result.update({
            "VARIANT1_KERNEL_GATE_FILE": gate,
            "VARIANT1_KERNEL_GATE_TOKEN": token,
            "VARIANT1_KERNEL_GATE_TIMEOUT_S": "10",
            "PYTHONUNBUFFERED": "1",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "TEMP": root,
            "TMP": root,
        })
        return result

    @staticmethod
    def execution_status() -> dict[str, Any]:
        return {
            "execution_mode": "same_user",
            "execution_grade": "same-user-separate-worker.v1",
            "worker_boundary": "separate-process-job.v1",
            "filesystem_access": "user",
            "network_access": "user",
            "process_access": "user",
        }

    async def _launch(
        self,
        *,
        work: str,
        gate: str,
        token: str,
        command: list[str],
    ) -> tuple[asyncio.subprocess.Process, KernelJobObject]:
        environment = self._environment(work, gate, token)
        job = KernelJobObject(
            max_processes=self.limits.max_processes,
            process_memory_bytes=self.limits.process_memory_bytes,
            job_memory_bytes=self.limits.job_memory_bytes,
            cpu_percent=self.limits.cpu_percent,
        )
        try:
            process = await self._spawn(command, work, environment)
        except BaseException as exc:
            try:
                job.close()
            except Exception as close_error:
                exc.add_note(f"Mutation owner cleanup: {close_error}")
            raise
        return process, job

    async def _spawn(self, command, work, environment):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if os.name == "nt":
            # Assign the job before the venv launcher can start the real
            # interpreter. Otherwise that child, and its children, escape
            # termination.
            flags |= CREATE_SUSPENDED
        return await asyncio.create_subprocess_exec(
            *command,
            cwd=work,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # ``StreamReader.readline`` otherwise inherits asyncio's 64 KiB
            # default even though this protocol admits frames up to 2 MiB.
            # Leave one byte of headroom for the newline delimiter so the
            # explicit protocol quota below remains the authoritative bound.
            limit=max(1024, int(self.limits.max_frame_bytes)) + 1,
            creationflags=flags,
            start_new_session=os.name != "nt",
        )

    @staticmethod
    async def _retire(process, job) -> None:
        """Settle descendants before releasing ownership or deleting their cwd."""
        async def cleanup():
            errors = []
            try:
                try:
                    if process.stdin is not None:
                        process.stdin.close()
                except Exception as exc:
                    errors.append(exc)
                # A Windows venv launcher may exit before its interpreter.
                # Terminate the complete Job first; waiting on just the launcher
                # does not establish that inherited pipes/cwd handles are closed.
                try:
                    job.terminate()
                except Exception as exc:
                    errors.append(exc)
                try:
                    if process.returncode is None:
                        with suppress(ProcessLookupError):
                            process.kill()
                except Exception as exc:
                    errors.append(exc)
                try:
                    deadline = asyncio.get_running_loop().time() + 5.0
                    while job.active_process_count():
                        if asyncio.get_running_loop().time() >= deadline:
                            raise TimeoutError("mutation process tree did not retire")
                        await asyncio.sleep(0.01)
                except Exception as exc:
                    errors.append(exc)
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except Exception as exc:
                    errors.append(exc)
            finally:
                try:
                    job.close()
                except Exception as exc:
                    errors.append(exc)
            if errors:
                error = MutationWorkerError(
                    "worker_cleanup", "mutation worker retirement failed"
                )
                for cause in errors:
                    error.add_note(f"{type(cause).__name__}: {cause}")
                raise error from errors[0]

        task = asyncio.create_task(cleanup(), name="mutation-worker-retire")
        cancellation = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancellation = exc
        task.result()
        if cancellation is not None:
            raise cancellation

    @asynccontextmanager
    async def _workspace(self):
        directory = tempfile.TemporaryDirectory(prefix="mutation-", dir=self.root)
        try:
            yield directory.name
        finally:
            original_error = sys.exception()
            try:
                for attempt in range(16):
                    try:
                        directory.cleanup()
                        break
                    except PermissionError:
                        # All owned processes have been retired. Windows may
                        # retain a sharing handle after their exit. A busy
                        # runner can hold the worker directory for a few seconds.
                        if attempt == 15:
                            raise
                        await asyncio.sleep(min(0.25 * (attempt + 1), 0.5))
            except OSError as exc:
                if original_error is None:
                    raise MutationWorkerError(
                        "worker_cleanup", "mutation workspace cleanup failed"
                    ) from exc
                original_error.add_note(f"Mutation workspace cleanup: {exc}")

    async def _run_once(
        self,
        request: dict[str, Any],
        *,
        proxy_call: Callable[[str, dict[str, Any], str], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        async with self._workspace() as work:
            gate = os.path.join(work, "parent-owned.gate")
            token = secrets.token_urlsafe(32)
            command = self._command()
            process, job = await self._launch(
                work=work,
                gate=gate,
                token=token,
                command=command,
            )
            try:
                # The venv launcher can fork before Python reaches the gate.
                # Assign the suspended launcher so every interpreter/descendant
                # inherits ownership, then resume and open the Python gate.
                await resume_owned_process_and_reap(process, job)
                with open(gate, "x", encoding="utf-8", newline="") as handle:
                    handle.write(token)
                    handle.flush()
                    os.fsync(handle.fileno())
                assert process.stdin is not None and process.stdout is not None
                raw = (stable_json({**request, "schema": WORKER_PROTOCOL}) + "\n").encode("utf-8")
                if len(raw) > self.limits.max_frame_bytes:
                    raise MutationWorkerError("worker_frame_quota", "worker request is too large")
                process.stdin.write(raw)
                await process.stdin.drain()
                deadline = time.monotonic() + self.limits.timeout_s
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise MutationWorkerError("worker_timeout", "mutation worker timed out")
                    try:
                        line = await asyncio.wait_for(
                            process.stdout.readline(), timeout=remaining
                        )
                    except ValueError as exc:
                        # StreamReader reports an over-limit line as ValueError
                        # instead of LimitOverrunError from ``readline``.
                        raise MutationWorkerError(
                            "worker_frame_quota", "worker response is too large"
                        ) from exc
                    except asyncio.TimeoutError:
                        raise
                    except (OSError, asyncio.IncompleteReadError) as exc:
                        raise MutationWorkerError(
                            "worker_protocol",
                            "mutation worker response stream failed",
                        ) from exc
                    if not line:
                        stderr = b""
                        if process.stderr is not None:
                            stderr = await process.stderr.read()
                        raise MutationWorkerError(
                            "worker_crash",
                            "mutation worker closed without a result: "
                            + stderr.decode("utf-8", errors="replace")[-4000:],
                        )
                    if len(line) > self.limits.max_frame_bytes:
                        raise MutationWorkerError("worker_frame_quota", "worker response is too large")
                    try:
                        message = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise MutationWorkerError(
                            "worker_protocol",
                            "mutation worker emitted a non-protocol frame: "
                            + line.decode("utf-8", errors="replace")[-1200:],
                        ) from exc
                    if message.get("schema") != WORKER_PROTOCOL:
                        raise MutationWorkerError("worker_protocol", "worker protocol mismatch")
                    if message.get("type") == "proxy_call":
                        response = await proxy_call(
                            str(message.get("proxy") or ""),
                            dict(message.get("arguments") or {}),
                            str(message.get("request_id") or ""),
                        )
                        response_raw = (stable_json({
                            **response,
                            "request_id": str(message.get("request_id") or ""),
                        }) + "\n").encode("utf-8")
                        if len(response_raw) > self.limits.max_frame_bytes:
                            raise MutationWorkerError(
                                "worker_frame_quota",
                                "worker proxy response is too large",
                            )
                        process.stdin.write(response_raw)
                        await process.stdin.drain()
                        continue
                    if message.get("type") != "result":
                        raise MutationWorkerError("worker_protocol", "unexpected worker message")
                    if not message.get("ok"):
                        error = dict(message.get("error") or {})
                        raise MutationWorkerError(
                            str(error.get("code") or "candidate_execution_error"),
                            str(error.get("message") or "candidate failed"),
                            traceback=str(message.get("traceback") or "")[-8000:],
                        )
                    return message
            finally:
                original_error = sys.exception()
                try:
                    await self._retire(process, job)
                except Exception as cleanup_error:
                    if original_error is None:
                        raise
                    original_error.add_note(f"Mutation cleanup: {cleanup_error}")

    async def run(
        self,
        request: dict[str, Any],
        *,
        proxy_call: Callable[[str, dict[str, Any], str], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        try:
            message = await asyncio.wait_for(
                self._run_once(request, proxy_call=proxy_call),
                # The candidate's own deadline is unchanged. Retirement must
                # not be cancelled just as a valid result arrives at that limit.
                timeout=self.limits.timeout_s + 15.0,
            )
        except MutationWorkerError:
            raise
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as exc:
            raise MutationWorkerError(
                "worker_timeout", "mutation worker timed out"
            ) from exc
        except (OSError, BrokenPipeError, ConnectionError) as exc:
            raise MutationWorkerError(
                "worker_crash", "mutation worker transport failed"
            ) from exc
        except Exception as exc:
            # Activated mutation invocation accounting catches the protocol's
            # single normalized error type. Never let an implementation-level
            # stream/parser exception bypass its durable failure receipt.
            raise MutationWorkerError(
                "worker_protocol",
                f"mutation worker protocol failed: {type(exc).__name__}",
            ) from exc
        message.update(self.execution_status())
        return message

__all__ = ["MutationWorkerClient", "WORKER_PROTOCOL"]
