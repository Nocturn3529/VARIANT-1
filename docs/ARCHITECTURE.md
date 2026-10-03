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

Chat history, model continuation records, artifacts, reusable
tool source, and live Python objects have different persistence contracts.
The former fact/profile memory store, automatic recall, extraction, proposals,
approvals, and consolidation have been retired. Startup removes only those
memory tables from the shared ASTB database. Goal records and their controls
remain available under Goals; `/remember` reports that fact saving is retired.
Saving a conversation does not imply that its live Python objects can be restored.

Agents retrieve earlier evidence explicitly through the always-mounted
`session.context()` method, including when mutation is off. It returns a retained
view handle with `status()`, `read()`, `search(query=...)`,
`expand(source_id=...)`, and `refresh()`. This adds no provider tool or ASTB slot.
Hidden child/automation runtimes can read their own retained runtime evidence
without a canonical conversation. A parent explicitly requests an owned child
with `session.context(child_id=...)`; ownership is checked on capture and reuse,
and deletion invalidates its views. Missing canonical history is reported.
Views capture committed canonical ancestry, a cell-ledger upper sequence, and
retained native snapshot cursors. Canonical ordering follows graph edges; cells
follow ledger sequence. These owners do not form one atomic global chronology.
Search covers canonical text and retained cell source/result text; native
snapshot search covers metadata. Expansion loads the scoped source and verifies
its integrity. Native expansion projects visible messages and calls, excluding
opaque provider reasoning. Snapshots can repeat history and are not additional
actions. Pages and text expansion expose continuation cursors and coverage.
Search match offsets address the indexed text projection; structured JSON and
native snapshot expansion can have different offsets. Expansion continuation
offsets always address the returned source text.
Cell expansion accepts `part='source'`, `'result'`, or `'output'`; output-event
evidence retains its own omissions and artifact references.

The added base/children methods change the catalog contract. Previously pinned
chats retain the existing explicit structural-rebase requirement; rebase requires
an idle runtime and fences its Python generation. Existing mutation tools are
handled by that rebase contract rather than being silently overwritten at startup.

The session-context SQLite index is derived data, not a new transcript owner.
Immutable source descriptors and text are shared across frozen views. Views
retain stream-prefix bounds; ordinary capture indexes committed additions rather
than rereading the archive. Canonical divergence uses a paged ancestry rebuild
with shared records. Native snapshots have a stable source-owned commit ordinal
and deletion epoch, independent of SQLite rowid reuse or VACUUM. A view does not
copy every native thread boundary. These are storage-order cursors, not proof of
one causal timeline across source owners.

Literal search uses a case-folded FTS5 trigram candidate index followed by exact
verification. A bounded selectivity probe chooses candidate lookups for rare
matches and ordered membership pages for common matches. Short queries and
SQLite builds without the tokenizer use an explicit scan fallback. Text identity
is stable across VACUUM; equal text does not collapse distinct recorded actions.
Read pages and exports use keysets, without a full archive in Python memory.
Disk storage still grows with retained evidence and view metadata; no age policy
silently deletes historical sources. Individual native checkpoints retain their
existing bounded whole-payload codec.

Views survive host restart, remain frozen until explicit refresh, and cannot be
read from another or deleted chat. Historical content is evidence rather than
new instructions or permission to replay effects. Active user instructions,
runtime contracts, and the existing working-context compactor remain. A future
cross-session source pool is a separate design; no fact extraction or automatic
history lookup is required by this reader.
`session.context(view_id=...)` reopens a saved view identity after a kernel or
host restart with the same ownership checks.

The Session context Settings page is a user control-plane browser over an
explicitly selected conversation and saved view. Its correlated requests use
the same scoped reader; agent retrieval remains limited to its session and
explicitly owned children. Capture and refresh are explicit. Native export
selects a destination, streams JSONL or Markdown to a staging file, verifies
source integrity and scope, then publishes atomically. Receipts include byte
count, SHA-256, source count and omissions. Binary artifact references remain
references rather than embedded payloads. A failed or cancelled export cleans
staging and preserves an existing destination. Existing files require explicit
overwrite authorization; export creates no model call.

Canonical ancestry queries drive the recursive frontier before looking up each
parent edge. This retains the same private ancestry and ordering while avoiding
a planner choice that scans all conversation edges at every recursion step.

Kernel storage ownership is acquired before scratch cleanup or interrupted-cell
reconciliation. Lifetime OS locks cover the canonical scratch and ledger paths;
PID creation identity supplies additional owner evidence. A competing live owner
blocks startup. Unknown legacy ownership is preserved with diagnostics. Recovery
records uncertain effects and never re-executes a cell to reconstruct evidence.
Portable capture keeps bounded byte/hash checks and validates new encodings in
a second traversal; reused values need one traversal. Capture does not skip a
checkpoint merely because a cell succeeded.

`children.wait(targets=..., timeout_s=0, after_cursor=...)` observes a bounded
set on the existing children object. Zero timeout takes a snapshot; a positive
timeout, at most 30 seconds, waits for the first committed terminal or attention
state under one shared budget. Cursors suppress repeated outcome delivery,
partial rosters are explicit, and full reports remain on child handles. A timeout
does not cancel children. Parent Stop retains descendant ownership; no paid idle
parent wake or model polling is needed for status updates.

Agent command/process modes use noninteractive editor, pager, Git prompt, and
color defaults; explicit environment settings override them. Interactive
terminal profiles keep their existing behavior. Windows PowerShell and
taskkill helpers resolve from absolute system paths, avoiding project-directory
lookup. This adds no dirty-worktree guard or destructive Git command policy.

Stream framing uses consumed/search cursors and amortized buffer compaction.
Valid UTF-8 is clipped at complete characters with byte accounting. Bounded
diagnostics and degraded broker results expose omissions. MCP inventories have
page, item, and elapsed limits; a failed refresh preserves the last good catalog
rather than publishing a partial replacement.

Compaction labels matched tool outcomes with names and failure state. Its
file-state section has a character budget, retains modified paths before reads,
and reports omissions. Oversized complete metadata is retained in a scoped
artifact for later recovery. An edit-call path alone does not prove a successful
change. The existing protected tail supplies recent assistant context, so no
extra assistant anchor is injected.

Provider recovery stays inside the canonical router. The optional
`provider_recovery` configuration defaults to disabled and names explicit
`fallback_routes` plus `auxiliary_routes` for `internal_json`, `internal_prose`,
and `vision`. Routes name mode/provider/model; each chain contains at most four
entries. `max_attempts` bounds physical attempts across credential alternatives,
retries and candidates (default 4, configurable 1–12). `max_wait_seconds` bounds
retry backoff waits (default 60, configurable 0–600); existing wire timeouts still
apply. No alternate route is discovered or activated without configuration.

Structured errors distinguish quota, rate, authentication, transient, context,
modality, request, and content-policy failures. Refusals are terminal. Cooldowns
honor reported reset times and credential identity; ordinary same-account token
refresh does not reset a quota cooldown. Cooldowns live in process memory and
create no timer-driven model calls. Main fallback promotion belongs to the run;
auxiliary routes belong to their individual calls. Process-wide selection stays
with the user. Cross-route requests use portable copied messages, preserve exact
tool-call/result relationships, and omit provider-owned opaque replay. Published
output/tool calls and completed effects fence retry and fallback.

Native checkpoints retain the selected/effective route and hashed wire identity.
Resume restores an effective fallback only when current configuration and wire
identity still permit it; otherwise outbound copies use portable history. The
stored checkpoint remains intact and pending tool ownership remains unchanged.
Qualification requests explicitly disable both the new policy and the existing
unbound provider fallback chain, so a backup cannot qualify as the requested
primary.

Provider routing Settings expose the ordered main chain, named auxiliary chains,
optional route effort and attempt/backoff limits. Saves validate declared effort
values and known providers, fence stale drafts with a policy revision, and report
success only after durable configuration saving. Failed saves restore the prior
policy. Route availability and request capability/context admission remain
runtime checks; a saved configuration is not a model qualification result.
An explicitly chosen auxiliary effort overrides that profile's lightweight
reasoning default for the selected call. Without that choice, existing internal
profile defaults remain. Auxiliary calls never promote the main run's route.


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
unstaged scopes inspect the current worktree and index. All commits compares a
selected branch tip to its merge base with a detected or explicitly chosen base
ref; this net branch delta is independent of history pagination. Automatic base
discovery uses repository remote-default metadata or a valid configured default,
and reports unavailable or ambiguous bases rather than assuming the repository
root or feature-branch upstream. Branch history includes every commit reachable
from the tip beyond that base. All branches history includes local and remote
branch tips, with resolved tips pinned across pages; it excludes tag-only and
stash history.

Individual selected commits have separate first-parent patches, including root
and merge commits. Their file list is a path union and their totals sum the
selected edits; it does not include unselected intervening commits or claim to
be one net branch delta. Historical patches do not link their line numbers to
current source. Current-file navigation is available only where a live comparison
can identify the current line. Counts expose incomplete observations, and binary
file changes remain distinct from text-line counts.

Read-only Electron observations bound branches, history, file lists, patches,
and unchanged-context expansion; binary and truncated output are explicit.
Concurrent cards share verified selection/file observations for at most one
second with at most eight cached selections. Selected patch sections share one
display-size budget. Repository watcher events and Git
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
