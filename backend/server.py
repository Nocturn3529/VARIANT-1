"""VARIANT-1 backend composition root.

Builds the process-wide service graph and exposes the FastAPI application.
Domain logic belongs in focused ``server_*``, ``host_*``, ``ws_*`` modules and
backend packages; this module retains only composition and live boundary calls.
"""

# The frozen backend executable is also the executable-extension worker.  This
# dispatch must stay above FastAPI/AppHost imports: a plugin subprocess must not
# construct, inherit, or import VARIANT-1's trusted service graph.
import sys

if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--peer-bridge":
    from peers.bridge_worker import main as _peer_bridge_main

    raise SystemExit(_peer_bridge_main(sys.argv[2:]))

if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--extension-worker":
    from extensions.worker_entry import main as _extension_worker_main

    raise SystemExit(_extension_worker_main(sys.argv[2:]))

import argparse
import os
import secrets
import socket

import time
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse
import uvicorn

import lifecycle
import host_gateway
from host_runtime_builder import install_host_runtime
from observability.activity import (
    HUB,
)

from paths import APP_ROOT  # frozen-aware app root (PyInstaller-safe)

VERSION = "0.1.0"
START_TIME = time.time()

# Writable base for user data (config, memory, downloaded models). Defaults to
# the app root so dev behavior is unchanged; in a packaged install Electron sets
# VARIANT1_DATA_DIR to a writable per-user location (the install dir / userData),
# since the app bundle itself is read-only. Read-only assets such as the model
# catalog still live under APP_ROOT and are seeded when needed.
DATA_DIR = os.path.abspath(os.environ.get("VARIANT1_DATA_DIR") or APP_ROOT)
CONFIG_DIR = os.path.join(DATA_DIR, "config")

AUTH_TOKEN = secrets.token_hex(24)
ACTIVITY_TOKEN = secrets.token_hex(24)
# Electron supplies a fresh nonce for each spawn attempt. This is stronger than
# comparing PIDs on Windows, where a venv launcher may wait on a different
# interpreter process. Direct/backend-only launches still generate their own.
INSTANCE_ID = (
    os.environ.get("VARIANT1_BACKEND_INSTANCE_ID", "").strip()
    or secrets.token_hex(16)
)
BACKEND_HOST = "127.0.0.1"
_RUNTIME = {
    "host": BACKEND_HOST,
    "port": 0,
    "port_file": None,
    "instance_id": INSTANCE_ID,
}

# Session state — held per WebSocket connection so two windows can never
# interleave their conversations. Implementation: chat_session.py
from chat_session import ConnectionSession  # noqa: E402

# --------------------------------------------------------------------------
# AppHost bootstrap — sole construction of router, tools, stores (issue #1)
# --------------------------------------------------------------------------
import host_bootstrap

LLM_CONFIG_PATH = os.environ.get("VARIANT1_LLM_CONFIG") or os.path.join(CONFIG_DIR, "llm_config.json")

APP = host_bootstrap.bootstrap_app_host(
    app_root=APP_ROOT,
    data_dir=DATA_DIR,
    config_dir=CONFIG_DIR,
    version=VERSION,
    hub=HUB,
    llm_config_path=LLM_CONFIG_PATH,
)
APP.instance_id = INSTANCE_ID
APP.start_time = START_TIME
APP.hardware = None

def _install_runtime() -> None:
    """Install concrete typed services once after ``AppHost`` bootstrap."""
    install_host_runtime(APP)


_install_runtime()

lifespan = lifecycle.make_lifespan(
    # Install late ops once; services already live on APP from bootstrap.
    runtime=_RUNTIME,
    auth_token=lambda: AUTH_TOKEN,
    version=APP.version,
    start_workers=APP.require_runtime().lifecycle.start_workers,
    shutdown=APP.require_runtime().lifecycle.shutdown,
    activity_token=lambda: ACTIVITY_TOKEN,
)

app = FastAPI(title="VARIANT-1 Backend", version=APP.version, lifespan=lifespan)

# --------------------------------------------------------------------------
# HTTP / WebSocket (bodies in server_http.py)
# --------------------------------------------------------------------------
import server_http
from model_runtime import openai_gateway

@app.get("/health")
async def health():
    return JSONResponse(server_http.health_payload(APP))


@app.post("/shutdown")
async def shutdown_backend(request: Request):
    return server_http.handle_shutdown(_RUNTIME, AUTH_TOKEN, request)

@app.post("/hook/{token}")
async def webhook(token: str, request: Request):
    """Fire a webhook-triggered automation. Loopback-only (the server binds
    127.0.0.1); the per-task random token is the gate. Runs in the background and
    returns immediately."""
    return await server_http.handle_webhook(APP, token, request)

# OpenAI-compatible local gateway. It follows the active VARIANT-1 runtime, so
# desktop tools can keep one stable URL while users switch models/backends.
@app.get("/v1/models")
async def inference_gateway_models(request: Request):
    denied = server_http.require_loopback_bearer(AUTH_TOKEN, request)
    if denied is not None:
        return denied
    return await openai_gateway.models(APP)

@app.post("/v1/chat/completions")
async def inference_gateway_chat(request: Request):
    denied = server_http.require_loopback_bearer(AUTH_TOKEN, request)
    if denied is not None:
        return denied
    return await openai_gateway.chat_completions(APP, request)

@app.post("/v1/tokenize")
@app.post("/tokenize")
async def inference_gateway_tokenize(request: Request):
    denied = server_http.require_loopback_bearer(AUTH_TOKEN, request)
    if denied is not None:
        return denied
    return await openai_gateway.tokenize(APP, request)

@app.get("/v1/status")
@app.get("/status")
async def inference_gateway_status(request: Request):
    denied = server_http.require_loopback_bearer(AUTH_TOKEN, request)
    if denied is not None:
        return denied
    return await openai_gateway.status(APP)

# Local-Studio-style controller projections make VARIANT-1 a useful node for
# other controller clients as well as a controller of remote nodes.
@app.get("/gpus")
async def inference_gateway_gpus(request: Request):
    denied = server_http.require_loopback_bearer(AUTH_TOKEN, request)
    if denied is not None:
        return denied
    from model_runtime import hardware as inference_hardware
    return (inference_hardware.telemetry() or {}).get("gpus", [])

@app.get("/runtime/targets")
async def inference_gateway_targets(request: Request):
    denied = server_http.require_loopback_bearer(AUTH_TOKEN, request)
    if denied is not None:
        return denied
    return {"items": APP.runtime_installer.discover_targets()}

@app.get("/recipes")
async def inference_gateway_recipes(request: Request):
    denied = server_http.require_loopback_bearer(AUTH_TOKEN, request)
    if denied is not None:
        return denied
    return APP.runtime_recipes.list()

@app.post("/launch/{recipe_id}")
async def inference_gateway_launch(recipe_id: str, request: Request):
    denied = server_http.require_loopback_bearer(AUTH_TOKEN, request)
    if denied is not None:
        return denied
    return APP.runtime_recipes.start_launch(recipe_id)

@app.post("/evict")
async def inference_gateway_evict(request: Request):
    denied = server_http.require_loopback_bearer(AUTH_TOKEN, request)
    if denied is not None:
        return denied
    await APP.runtime_recipes.evict()
    return {"ok": True}

async def _gateway_route(envelope, text: str) -> str:
    """Route one authorized remote message through the ordinary chat pipeline."""
    return await host_gateway.gateway_route(APP, envelope, text)


APP.gateway.set_router(_gateway_route)

# --------------------------------------------------------------------------
# WebSocket IPC
# --------------------------------------------------------------------------
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    return await server_http.websocket_endpoint(
        APP, websocket,
        auth_token=AUTH_TOKEN, session_factory=ConnectionSession,
    )


@app.websocket("/ws/activity")
async def activity_ws_endpoint(websocket: WebSocket):
    return await server_http.activity_websocket_endpoint(
        APP,
        websocket,
        activity_token=ACTIVITY_TOKEN,
    )


from peers.bridge_api import publish_bridge, register_bridge_routes
register_bridge_routes(app, APP)

# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------
def _bind_server_socket(port: int = 0) -> socket.socket:
    """Bind and retain the backend listener socket before publishing its port.

    Keeping this socket open removes the probe/close/rebind race that existed
    when a free port number was discovered separately from Uvicorn startup.
    The host is deliberately not configurable: loopback is a product security
    boundary, not a deployment option.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform.startswith("win") and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((BACKEND_HOST, port))
        sock.set_inheritable(True)
        return sock
    except Exception:
        sock.close()
        raise

def main() -> int:
    parser = argparse.ArgumentParser(description="VARIANT-1 backend")
    parser.add_argument("--port-file", required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()

    config = uvicorn.Config(
        app, host=BACKEND_HOST, port=args.port, log_level="warning", access_log=False)
    server_socket = _bind_server_socket(args.port)
    port = server_socket.getsockname()[1]
    config.port = port
    _RUNTIME["host"] = BACKEND_HOST
    _RUNTIME["port"] = port
    publication = publish_bridge(APP, f"http://127.0.0.1:{port}")
    _RUNTIME["port_file"] = args.port_file

    server = uvicorn.Server(config)
    _RUNTIME["server"] = server
    try:
        server.run(sockets=[server_socket])
    finally:
        publication.close()
        _RUNTIME.pop("server", None)
        server_socket.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
