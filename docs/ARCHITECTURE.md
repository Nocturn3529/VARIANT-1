# Architecture

VARIANT-1 is an AI workspace with an Electron interface, a Python backend,
and separate persistent CPython workers for live chat runtimes. Windows is the
primary source-development path; preview packages also target Linux x86_64 and
macOS arm64. This guide explains how execution, state, and service ownership fit
together. For Windows source installation, see [Setup](SETUP.md).

## One model-facing execution path

Disposable mutation workers retain process-tree ownership until all descendants
have exited, before deleting their workspace. Retirement is protected against
repeated cancellation and has a separate bounded cleanup allowance. Candidate
errors remain candidate errors when cleanup also fails; cleanup failures have
their own diagnostic category. Persistent CPython cell policy is unchanged.
Windows persistent and mutation workers start suspended, enter their Job Object,
and then resume. This also owns the real interpreter created by a venv launcher;
a Python-level gate alone cannot prevent that earlier launcher fork.

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

`session_catalog.service` publishes categories, mounts, and disclosure; its
mutation manager delegates disposable execution to the mutation worker client.
`kernel_runtime.lease` owns one worker generation, while `continuity` coordinates
portable capture/restoration and `worker_bridge` carries capability proxies.
The separate `resources` module observes the interpreter and its owned process
tree without changing their lifetime. Resource protocols describe the process
and ownership interfaces used by that observer.

## State lifetimes

**Live state is not the same as saved history or restart recovery.** Variables,
imports, and helper functions survive calls within one live Python generation.
A reset, restart, eviction, or process failure can end that generation.

Persistent kernels have no default cell deadline, resource quota, or automatic
retirement based on idle time, age, or the number of live chats. Process-tree
ownership and explicit shutdown still apply. Positive host overrides can enable
quotas or retirement; disposable mutation workers have their own bounded policy.
Resource status reports a disabled memory quota and its pressure ratio as `null`,
while retaining available usage measurements. A configured quota with an unknown
usage measurement also has an unknown pressure ratio.

The Python runtime view inventories retained sessions and distinguishes live
interpreter measurements from earlier worker snapshots. Tree RSS is a sum of
owned processes and can count shared pages more than once. Releasing a session
is explicit, generation-fenced, and refused while a run is admitted, including
between cells. Optional checkpoint policy applies to operator release; default
checkpointing and retirement behavior is unchanged.

Portable checkpoints preserve supported values and report exclusions. They do
not capture arbitrary clients, connections, threads, or live handles. Portable
checkpointing and restoration are optional and disabled by default.

Chat history, model continuation records, artifacts, approved memory, reusable
tool source, and live Python objects have different persistence contracts.
Approved memory is a separate store: explicit user memories and approved inferred
facts can be recalled in later turns. Saving a conversation does not imply that
all of its live Python objects can be restored.

The activity trace is a bounded display projection, not a complete output log.
Live and saved ordinary traces keep the newest 48 events and record earlier
omissions; provider summaries retain their separate storage bound. Tool failure,
interruption, timeout, skipped/degraded work, and unknown outcomes remain distinct.
Managed credentials and recognized secret patterns are sanitized before display
clipping, activity broadcast, and annotation persistence. Execution inputs and
internal outcome records keep their existing contracts; cloud-envelope secret
egress remains a separate firewall.

Client trace enrichment is written after the run's durable assistant commit.
An explicit commit confirmation starts delivery even if the later append event
is lost; legacy completion frames wait for the durable append. Session/run/request
acknowledgments confirm that separate write. Unconfirmed
activity saves have bounded retries and reconnect reconciliation; the frontend
does not claim that an unconfirmed trace is saved merely because its reply is.
Disclosure state is chat/run/call scoped. Shared elapsed timing and selected
store subscriptions avoid unrelated control updates during token streaming.
Idle projection eviction and confirmed deletion coordinate context, receipt,
disclosure, and turn data without retiring a Python generation. Active work,
drafts, pending input/preparation, and unfinished settings or annotation requests
are protected, including unconfirmed trace evidence; settled admission fences
survive idle view eviction.

Review presents bounded unified patches in collapsible file cards with old/new
gutters, safe code tokenization, and virtualized rendering. Its Changed files
sidebar is hidden by default and opens on the right. Uncommitted, staged, and
unstaged scopes inspect the current worktree and index. Branch selection browses
first-parent history without checking out a branch; selected commits must form
a continuous first-parent range. Root and merge commits have explicit comparison
boundaries, and historical patches do not link their line numbers to current
source. Current-file navigation is available only where a live comparison can
identify the current line.

Read-only Electron observations bound branches, history, file lists, patches,
and unchanged-context expansion; binary and truncated output are explicit.
Concurrent cards share verified selection/file observations for at most one
second with at most eight cached selections. Repository watcher events and Git
mutations invalidate those observations. This view provides no per-turn edit
attribution, split view, whitespace toggle, or wrapping option. File-level Git
actions retain sender, path, and recoverable-discard protections. Commit, push,
and pull-request operations can be requested through the agent rather than a
Review footer.
Native browser moves retain their live pages rather than reloading them.

## Mutation

Mutation means changing executable tool methods, not training model weights.
Authoring is off for new chats. When enabled, a model can propose and activate
chat-local tool changes with validation, invocation records, and rollback support.
Turning authoring off does not silently remove an already-active tool overlay.

## Authority and intervention

Cancelling kernel boot retires the partially started lease before propagating
the caller's original cancellation. Repeated cancellation cannot interrupt that
cleanup, and cancellation is not converted into a retryable startup failure.
The manager still retries genuine boot failures; a later explicit request can
start a new generation after cancellation.

The runtime operates with the current user's permissions; it is not a security
sandbox. ASTB controls capability discovery, not ordinary Python's underlying
authority. A selected project directory is not a filesystem security boundary.

Stop, Steer, active cells, background Python work, and durably admitted jobs have
distinct lifecycles. Cancellation may require terminating a blocked worker and
cannot reverse an external effect that has already completed. Integration
receipts should report uncertainty rather than imply success or rollback that
has not been established.

An interrupted cell can retain its generation and ordinary background Python
tasks when interruption completes cooperatively. Terminal Stop can close the
generation if work does not settle; closing or restarting the kernel ends its
live state. Ordinary chat steering waits for the current cell to complete.

## Development ownership

Keep persistent service ownership in the backend and render its state through
the frontend protocol. Handle resource/chat identity and stale asynchronous
results explicitly. Test changes in isolation from personal models, accounts,
browser profiles, and runtime data. See [Validation](VALIDATION.md).

Each renderer's backend connection owns and releases its status subscription.
This includes docked chats borrowing the parent window's bridge, whose callbacks
must be removed when the child connection stops.

Fresh workbenches keep empty side panes hidden until requested or a first project
is selected. Saved layouts and explicit visibility changes take precedence.
The empty chat guide reads configuration for the chat's selected model route
from backend projections. Examples fill drafts without sending them.
Mutation authoring is available under
Session tools; disabling authoring preserves already activated overlays.

Production renderer builds strip fixture event ingress. The separate development
entry and fixture payloads are excluded from packages. Runtime stop hooks dispose
view polling; leaving or closing Overview does not leave its telemetry timers
running against an inactive connection.

### Installer composition

Published prereleases provide unsigned Windows, Linux, and macOS packages.
CI builds and checks packaged startup and installation/removal on those platforms.
Release notes record the exact build source and qualification limits; a current
source checkout can be newer than a published binary.

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
