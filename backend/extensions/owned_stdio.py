"""MCP stdio transport with a process tree assigned before Windows execution."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import os
import subprocess
import sys
from typing import TextIO

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from anyio.streams.text import TextReceiveStream
from mcp import types
from mcp.client.stdio import StdioServerParameters, get_default_environment
from mcp.os.win32.utilities import FallbackProcess, get_windows_executable_command
from mcp.shared.message import SessionMessage

from process_tree import (
    OwnedProcessTree,
    CREATE_SUSPENDED,
    dispose_process_tree,
    resume_owned_process,
)


async def _open_owned(server: StdioServerParameters, errlog: TextIO):
    command = (
        get_windows_executable_command(server.command)
        if sys.platform == "win32" else server.command
    )
    environment = (
        {**get_default_environment(), **server.env}
        if server.env is not None else get_default_environment()
    )
    owner = OwnedProcessTree()
    process = None
    try:
        options = {
            "env": environment,
            "cwd": server.cwd,
            "stderr": errlog,
        }
        if sys.platform == "win32":
            options["creationflags"] = CREATE_SUSPENDED | getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            )
        else:
            options["start_new_session"] = True
        try:
            process = await anyio.open_process([command, *server.args], **options)
        except NotImplementedError:
            if sys.platform != "win32":
                raise
            popen = subprocess.Popen(
                [command, *server.args], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=errlog,
                cwd=server.cwd, env=environment, bufsize=0,
                creationflags=options["creationflags"],
            )
            process = FallbackProcess(popen)
        owned = resume_owned_process(process, owner)
        if owned is None:
            raise RuntimeError("MCP stdio process exited before ownership admission")
        return process, owned
    except BaseException:
        if process is not None:
            with suppress(Exception):
                process.kill()
            with suppress(Exception):
                await process.wait()
        with suppress(Exception):
            dispose_process_tree(owner)
        raise


@asynccontextmanager
async def owned_stdio_client(
    server: StdioServerParameters, errlog: TextIO = sys.stderr,
):
    """Provide the SDK streams while owning every local descendant."""

    read_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_reader = anyio.create_memory_object_stream(0)
    try:
        process, owner = await _open_owned(server, errlog)
    except BaseException:
        await read_stream.aclose()
        await write_stream.aclose()
        await read_writer.aclose()
        await write_reader.aclose()
        raise

    async def stdout_reader():
        assert process.stdout is not None
        buffer = ""
        try:
            async with read_writer:
                async for chunk in TextReceiveStream(
                    process.stdout, encoding=server.encoding,
                    errors=server.encoding_error_handler,
                ):
                    lines = (buffer + chunk).split("\n")
                    buffer = lines.pop()
                    for line in lines:
                        try:
                            message = types.JSONRPCMessage.model_validate_json(line)
                        except Exception as exc:
                            await read_writer.send(exc)
                            continue
                        await read_writer.send(SessionMessage(message))
        except anyio.ClosedResourceError:
            await anyio.lowlevel.checkpoint()

    async def stdin_writer():
        assert process.stdin is not None
        try:
            async with write_reader:
                async for item in write_reader:
                    raw = item.message.model_dump_json(
                        by_alias=True, exclude_none=True,
                    )
                    await process.stdin.send(
                        (raw + "\n").encode(
                            server.encoding, errors=server.encoding_error_handler,
                        )
                    )
        except anyio.ClosedResourceError:
            await anyio.lowlevel.checkpoint()

    try:
        async with anyio.create_task_group() as group, process:
            group.start_soon(stdout_reader)
            group.start_soon(stdin_writer)
            try:
                yield read_stream, write_stream
            finally:
                if process.stdin is not None:
                    with suppress(Exception):
                        await process.stdin.aclose()
                # Closing this owner is the authoritative tree cleanup. SDK
                # stdio cleanup can otherwise stop only its immediate child.
                dispose_process_tree(owner)
                with suppress(Exception):
                    with anyio.fail_after(2):
                        await process.wait()
                await read_stream.aclose()
                await write_stream.aclose()
                await read_writer.aclose()
                await write_reader.aclose()
    finally:
        with suppress(Exception):
            dispose_process_tree(owner)


__all__ = ["owned_stdio_client"]
