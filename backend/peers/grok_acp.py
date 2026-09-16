"""ACP transport over an existing ExecutionHost-owned Grok process.

This adapts a protocol; it does not execute a second VARIANT-1 model loop.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import uuid


class ACPError(RuntimeError):
    code = "grok_acp_error"


class GrokACP:
    def __init__(self, execution, process_id, *, on_event=None, on_request=None):
        self.execution = execution
        self.process_id = process_id
        self.on_event = on_event
        self.on_request = on_request
        self.pending = {}
        self.cursor = 0
        self.buffer = b""
        self.closed = False
        self.task = None
        self.handlers = set()

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._pump(), name=f"grok-acp:{self.process_id}")

    def _write(self, message):
        if self.closed:
            raise ACPError("Grok protocol connection is closed")
        raw = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        return self.execution.processes.write(self.process_id, raw)

    async def request(self, method, params, *, timeout=20, write_timeout=20, on_written=None):
        self.start()
        request_id = "variant_" + uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            receipt = self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            write_id = (receipt or {}).get("write_id")
            if not write_id:
                raise ACPError("Grok protocol input has no delivery receipt")
            started = asyncio.get_running_loop().time()
            while write_id:
                if self.closed:
                    raise ACPError("Grok connection closed before input delivery settled")
                writes = self.execution.processes.input_status(self.process_id)
                write = next((row for row in writes if row.get("write_id") == write_id), None)
                if write and write.get("state") == "written":
                    break
                if write and write.get("state") in {"partial", "cancelled", "unknown_effect"}:
                    raise ACPError("Grok protocol input was not completely written")
                if asyncio.get_running_loop().time() - started > write_timeout:
                    raise ACPError("Grok protocol input delivery timed out")
                await asyncio.sleep(.025)
            if on_written:
                result = on_written()
                if inspect.isawaitable(result):
                    await result
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Also retrieve failures raised while waiting for the OS write.

    async def _server_request(self, message):
        try:
            if self.on_request is None:
                raise ACPError(f"Unsupported Grok client request: {message['method']}")
            result = self.on_request(message)
            if inspect.isawaitable(result):
                result = await result
            self._write({"jsonrpc": "2.0", "id": message["id"], "result": result})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not self.closed:
                self._write({"jsonrpc": "2.0", "id": message["id"],
                             "error": {"code": -32603, "message": str(error)[:500]}})

    async def _message(self, message):
        if not isinstance(message, dict):
            raise ACPError("Grok sent a non-object protocol message")
        identity = message.get("id")
        if "method" in message:
            if identity is not None:
                task = asyncio.create_task(self._server_request(message), name="grok-acp-client-request")
                self.handlers.add(task)
                task.add_done_callback(self._handler_done)
            elif self.on_event:
                result = self.on_event(message)
                if inspect.isawaitable(result):
                    await result
        elif identity in self.pending:
            future = self.pending[identity]
            if not future.done():
                if "error" in message:
                    error = message["error"]
                    future.set_exception(ACPError(str(error.get("message", error)) if isinstance(error, dict) else str(error)))
                else:
                    future.set_result(message.get("result"))

    def _handler_done(self, task):
        self.handlers.discard(task)
        if not task.cancelled() and task.exception() is not None:
            # Losing the client response makes permission delivery uncertain.
            self.closed = True
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ACPError("Grok client response could not be delivered"))

    async def _pump(self):
        failure = ACPError("Grok protocol connection ended")
        try:
            while not self.closed:
                page = self.execution.processes.logs(self.process_id, after_cursor=self.cursor,
                                                     max_bytes=262144, max_frames=1000).to_dict()
                self.cursor = int(page.get("next_cursor", self.cursor))
                for frame in page.get("frames", []):
                    if frame.get("stream") != "stdout":
                        continue
                    raw = (base64.b64decode(frame["data_base64"]) if frame.get("data_base64")
                           else str(frame.get("text", "")).encode("utf-8"))
                    self.buffer += raw
                    while b"\n" in self.buffer:
                        line, self.buffer = self.buffer.split(b"\n", 1)
                        if len(line) > 8 * 1024 * 1024:
                            raise ACPError("Grok protocol frame exceeded its maximum size")
                        if line.strip():
                            await self._message(json.loads(line))
                    if len(self.buffer) > 8 * 1024 * 1024:
                        raise ACPError("Grok protocol frame exceeded its maximum size")
                if not self.execution.processes.get(self.process_id).live and not page.get("more"):
                    break
                await asyncio.sleep(0.025 if page.get("frames") else 0.075)
        except asyncio.CancelledError:
            pass
        except Exception as error:
            failure = ACPError(str(error))
        finally:
            self.closed = True
            for handler in self.handlers:
                handler.cancel()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(failure)

    async def close(self):
        self.closed = True
        tasks = [task for task in (self.task, *self.handlers) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
