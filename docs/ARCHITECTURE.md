# Architecture

VARIANT-1 is a Windows AI workspace with an Electron interface, a Python backend,
and separate persistent CPython workers for live chat runtimes. This guide
explains how execution, state, and service ownership fit together. For source
installation, see [Setup](SETUP.md).

## One model-facing execution path

Models use the historical `ipython(category, code)` action. Despite the name,
it runs a custom CPython REPL with top-level await, not Jupyter or IPython.

```text
model -> Python cell -> mounted proxy -> capability broker -> canonical service
```

ASTB exposes capabilities progressively, as they are needed. Python can hold
data, compose calls, create helpers, and transform intermediate results. The
backend service responsible for a resource remains its authoritative owner.
New integrations should extend this path rather than introduce competing tool
loops or state controllers.

The desktop frontend is in `frontend/main-deck/src`. Electron lifecycle, IPC,
and native-window modules live at the repository root. Backend domain packages
include `kernel_runtime`, `session_catalog`, `session_runtime`, `browser_fabric`,
`desktop_fabric`, `execution_hosts`, `work_fabric`, `peers`, and `extensions`.
Provider calls use the backend model-routing path.

## State lifetimes

**Live state is not the same as saved history or restart recovery.** Variables,
imports, and helper functions survive calls within one live Python generation.
A reset, restart, eviction, or process failure can end that generation.

Portable checkpoints preserve supported values and report exclusions. They do
not capture arbitrary clients, connections, threads, or live handles. Portable
checkpointing and restoration are optional and disabled by default.

Chat history, model continuation records, artifacts, approved memory, reusable
tool source, and live Python objects have different persistence contracts.
Approved memory is a separate store: explicit user memories and approved inferred
facts can be recalled in later turns. Saving a conversation does not imply that
all of its live Python objects can be restored.

## Mutation

Mutation means changing executable tool methods, not training model weights.
Authoring is off for new chats. When enabled, a model can propose and activate
chat-local tool changes with validation, invocation records, and rollback support.
Turning authoring off does not silently remove an already-active tool overlay.

## Authority and intervention

The runtime operates with the current user's permissions; it is not a security
sandbox. ASTB controls capability discovery, not ordinary Python's underlying
authority. A selected project directory is not a filesystem security boundary.

Stop, Steer, active cells, background Python work, and durably admitted jobs have
distinct lifecycles. Cancellation may require terminating a blocked worker and
cannot reverse an external effect that has already completed. Integration
receipts should report uncertainty rather than imply success or rollback that
has not been established.

## Development ownership

Keep persistent service ownership in the backend and render its state through
the frontend protocol. Handle resource/chat identity and stale asynchronous
results explicitly. Test changes in isolation from personal models, accounts,
browser profiles, and runtime data. See [Validation](VALIDATION.md).

### Installer composition — 2026-09-16

The Live2D cat overlay has been removed, including its avatar window, preload,
renderer, animation configuration, libraries, and model payload. Start hidden
starts the tray and backend; normal startup opens Main Deck.

React, p5, and xterm are build dependencies compiled into the renderer, rather
than duplicated in production `node_modules`. Personal configuration and plugins
are excluded from installer defaults. The build retains both frozen Python
runtimes, the native CPU/CUDA fallback, and browser provisioning. Retaining these
components is not a claim that every hardware combination has been qualified.
Release freezing requires matching Electron and backend handshake versions.

The baseline dependency lock and both frozen runtimes exclude offline speech
engines. Kokoro voice enumeration and WAV synthesis use a configured, user-owned
HTTP service through the existing speech provider; no model-facing tool or
second agent loop is added. Whisper uses a user-provided executable/model
sidecar, and cloud speech uses its HTTP adapters. Optional source-only Kokoro
dependencies are separate from the baseline lock.

Frozen checks verify the absence of offline speech payloads, clear behavior when
speech is unconfigured, and WAV transport through a configured HTTP fixture.
These checks are not a speech-quality benchmark or a substitute for release
qualification. See [Release process](RELEASING.md).
