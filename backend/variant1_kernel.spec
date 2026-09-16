# -*- mode: python ; coding: utf-8 -*-
"""Separately frozen persistent CPython/ASTB worker.

Output: ``backend/dist/Variant1Kernel/Variant1Kernel.exe``. Host services remain in
Variant1Backend; this executable owns only the REPL, ASTB proxies, mutation
candidate dispatch, and portable analytical codecs.
"""

from PyInstaller.utils.hooks import copy_metadata


datas = []
binaries = []
hiddenimports = [
    "kernel_runtime.repl_protocol",
    "kernel_runtime.repl_worker",
    "kernel_runtime.worker_context",
    "kernel_runtime.bridge_protocol",
    "kernel_runtime.worker_bridge",
    "kernel_runtime.capsule_worker",
    "kernel_runtime.capsule_contracts",
    "kernel_runtime.runtime_profile",
    "kernel_runtime.mutation_worker",
    "psutil",
    "numpy",
    "pandas",
    "pyarrow",
    "duckdb",
    "matplotlib",
    "plotly",
    "plotly.express",
    "plotly.graph_objects",
    "safetensors",
    "safetensors.numpy",
]

# Runtime-profile validation and portable codecs query exact installed
# distribution identities inside the frozen worker.
for distribution in (
    "psutil",
    "duckdb",
    "matplotlib",
    "numpy",
    "pandas",
    "plotly",
    "pyarrow",
    "safetensors",
):
    datas += copy_metadata(distribution)

a = Analysis(
    ["kernel_runtime/worker_main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={"matplotlib": {"backends": ["Agg"]}},
    runtime_hooks=[],
    excludes=[
        "server", "fastapi", "uvicorn", "playwright", "mcp", "kokoro_onnx",
        "phonemizer", "espeakng_loader", "onnxruntime", "neutts", "kittentts", "piper", "soundfile",
        "IPython", "ipykernel", "jupyter_client", "jupyter_core", "zmq",
        "traitlets", "tornado", "comm", "debugpy", "jedi", "parso",
        "prompt_toolkit", "matplotlib_inline", "nest_asyncio", "tkinter",
        "torch", "scipy", "pytest", "_pytest", "hypothesis",
        "numpy.tests", "pandas.tests", "pyarrow.tests", "psutil.tests",
        "matplotlib.tests", "plotly.tests",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Variant1Kernel",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # The host launches with CREATE_NO_WINDOW and captures JSONL/stdout/stderr
    # through private pipes. A console build keeps those handles usable.
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
    name="Variant1Kernel",
)
