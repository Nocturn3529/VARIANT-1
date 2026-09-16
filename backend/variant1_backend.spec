# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the VARIANT-1 backend.

Freezes backend/server.py (and its sibling modules + all pip dependencies) into
a self-contained Variant1Backend executable so the installed app needs NO Python
on the target machine. Built via `node scripts/build-backend.js` (or directly:
`python -m PyInstaller --noconfirm --clean variant1_backend.spec` from backend/).

Output: backend/dist/Variant1Backend/Variant1Backend.exe (+ _internal/). The Electron
packaging step (package.json) copies that folder to <resources>/backend/, where
main.js launches Variant1Backend.exe.

Notes:
- Offline speech engines/weights are not bundled. Cloud speech uses HTTP clients;
  local Kokoro connects to a user-owned server. MCP and web/document/capture
  libraries use exact first-party imports plus maintained PyInstaller hooks.
- console=True keeps ``sys.stdout``/``sys.stderr`` usable for Uvicorn and lets
  Electron capture backend diagnostics into ``logs/main.log``. Electron starts
  the process with ``windowsHide: true``, so this does not open a terminal.
"""

from PyInstaller.utils.hooks import (
    collect_submodules,
    copy_metadata,
)

datas = []
binaries = []
hiddenimports = []

# Offline speech engines are user-provided services, not frozen payloads.

# Domain packages register implementations dynamically.
hiddenimports += collect_submodules("work_fabric")
hiddenimports += collect_submodules("chat_sessions")
hiddenimports += collect_submodules("execution_hosts")
hiddenimports += collect_submodules("peers")
hiddenimports += collect_submodules("coding")
hiddenimports += collect_submodules("goals")
hiddenimports += collect_submodules("artifacts")
hiddenimports += collect_submodules("web_search")
hiddenimports += collect_submodules("browser_fabric")
hiddenimports += collect_submodules("desktop_fabric")
hiddenimports += collect_submodules("extensions")
hiddenimports += collect_submodules("speech")
hiddenimports += collect_submodules("messaging_gateway")
hiddenimports += collect_submodules("edge_tts")
datas += copy_metadata("edge-tts")
hiddenimports += [
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
]

# Our own sibling modules (imported by name from server.py).
hiddenimports += [
    "paths", "llm_router", "security.secretstore",
    "desktop.vision", "desktop.vision_capture", "speech.local_stt",
    "speech.local_tts", "speech.xai", "tools", "builtin_tools",
    "extensions.mcp_v2",
    "model_runtime", "model_runtime.capabilities", "model_runtime.context",
    "model_runtime.engine_manager", "model_runtime.hardware",
    "model_runtime.llama_server", "model_runtime.message_graph",
    "model_runtime.request_manifest",
    "model_runtime.telemetry", "tool_discovery", "model_providers",
    "messaging_gateway.credentials", "observability.cloud_usage", "xai_oauth",
    "openai_codex_oauth", "llm_openai_codex_responses",
    "messaging_gateway", "messaging_gateway.adapters.telegram",
    "messaging_gateway.adapters.discord",
    "kernel_runtime", "kernel_runtime.manager", "kernel_runtime.bridge_protocol",
    "kernel_runtime.repl_protocol",
    "kernel_runtime.output", "kernel_runtime.job_object",
    "kernel_runtime.bridge", "kernel_runtime.integration",
    "kernel_runtime.capsules", "kernel_runtime.capsule_contracts",
    "kernel_runtime.cell_ledger", "kernel_runtime.capabilities",
]


a = Analysis(
    ["server.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "torch", "kokoro",
        "kokoro_onnx", "phonemizer", "espeakng_loader", "onnxruntime",
        "neutts", "kittentts", "piper", "soundfile",  # optional user-owned speech engines
        # Model-authored Python and portable codecs live only in Variant1Kernel.
        "IPython", "ipykernel", "jupyter_client", "jupyter_core", "zmq",
        "traitlets", "tornado", "comm", "debugpy", "jedi", "parso",
        "prompt_toolkit", "matplotlib_inline", "nest_asyncio",
        "duckdb", "matplotlib", "pandas", "plotly", "pyarrow", "safetensors", "scipy",
        # Dependency hooks must never freeze their own validation suites.
        "pytest", "_pytest", "hypothesis",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Variant1Backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # A windowed PyInstaller process sets sys.stdout/sys.stderr to None on
    # Windows. Uvicorn configures its formatter against stdout during startup,
    # so a windowed build fails before it can publish the port-file handshake.
    # Electron already supplies windowsHide=true and piped streams.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Variant1Backend",
)
