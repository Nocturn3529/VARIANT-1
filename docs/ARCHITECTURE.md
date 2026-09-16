# Architecture

VARIANT-1 is a Windows AI host with Electron presentation, a Python backend, and
separate persistent CPython workers for live chat runtimes.

## One model-facing execution path

Models use the historical `ipython(category, code)` action. Its implementation is
a custom CPython REPL with top-level await, not Jupyter or IPython.

```text
model -> Python cell -> mounted proxy -> capability broker -> canonical service
```

ASTB progressively discloses capabilities. Ordinary Python can hold data,
compose calls, create helpers, and transform intermediate results. Resource and
service authority remains with the canonical backend owner. New integrations
should extend this path, not create competing tool loops or state controllers.

The desktop frontend source is in `frontend/main-deck/src`. Electron lifecycle,
IPC and native-window modules live at the repository root. Backend domain
packages include `kernel_runtime`, `session_catalog`, `session_runtime`,
`browser_fabric`, `desktop_fabric`, `execution_hosts`, `work_fabric`, `peers`,
and `extensions`. Provider calls use the backend model-routing path.

## State lifetimes

Variables, imports, and helper functions survive calls within a live Python
generation. Reset, restart, eviction, or process failure can end that generation.
Portable checkpoints preserve supported values and report exclusions; they do
not capture arbitrary clients, connections, threads, or live handles. Portable
checkpointing/restoration is optional and disabled in the default configuration.

Chat history, model continuation records, artifacts, approved memory, reusable
tool source, and live Python objects have different persistence contracts.
Approved memory is a separate store: explicit user memories and approved inferred
facts can be recalled in later turns.

## Mutation

Mutation authoring starts off for new chats. When enabled, a model can propose and
activate chat-local tool changes, with validation, invocation records and rollback
support. Turning authoring off does not silently erase an existing active overlay.
Mutation changes executable methods; it does not train model weights.

## Authority and intervention

The runtime is same-user software, not a security sandbox. ASTB disclosure is not
a restriction on ordinary Python authority. Stop, Steer, active cells, background
Python work, and durably admitted jobs have distinct lifecycles. Cancellation may
require ending a blocked worker and cannot reverse an external effect already
completed. Integration receipts should describe uncertainty honestly.

## Development ownership

Keep persistent service ownership in the backend and render its state through the
frontend protocol. Treat resource/chat identity and stale asynchronous results
explicitly. Test changes in isolation from personal models, accounts, browser
profiles, and runtime data.


### Installer composition — 2026-09-16

The Live2D cat overlay is retired: no avatar window, preload, renderer, animation
configuration, libraries or model payload. Start hidden starts the tray/backend;
normal startup opens Main Deck. React, p5 and xterm are build dependencies,
compiled into the renderer, and no longer ship twice as production node_modules.
Personal config/plugins are excluded from installer defaults. Both frozen Python
runtimes, native CPU/CUDA fallback, and browser provisioning remain intact.
Release freezing requires matching Electron and backend handshake versions.

The baseline lock and both frozen runtimes exclude offline speech engines.
Kokoro voice enumeration and WAV synthesis use the configured user-owned HTTP
service through the existing speech provider. No model-facing tool or second
agent loop is added. Whisper retains its user-provided executable/model sidecar;
cloud speech retains its HTTP adapters. Optional source-only Kokoro dependencies
are separate from the baseline lock. Frozen checks verify absent speech payloads,
clear unconfigured behavior, and configured external WAV transport.
