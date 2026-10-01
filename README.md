# VARIANT-1

**Work with local files, Python, and AI in one Windows workspace.**

VARIANT-1 brings a live Python environment, connected tools, and supported local
or cloud models into a desktop workspace for coding, research, and desktop work.
Keep variables, imports, and helper functions available across calls within a
live session, combine tools through Python, and inspect intermediate results.

The current preview is for technically comfortable users who want to inspect
and adapt multi-step workflows. Windows is the primary source-development path;
preview packages are also available for Linux and Apple Silicon macOS. It uses
an Electron/React interface and a CPython backend; see
[Architecture](docs/ARCHITECTURE.md) for the technical design.

[Website](https://variant-1-silk.vercel.app/) · [Setup](docs/SETUP.md) ·
[Architecture](docs/ARCHITECTURE.md) · [Issues](https://github.com/Nocturn3529/VARIANT-1/issues)

## Status

VARIANT-1 is an early public **preview**, under active development. Unsigned
Windows x64, Linux x86_64, and macOS arm64 packages are available in
[GitHub Releases](https://github.com/Nocturn3529/VARIANT-1/releases), with checksums
and release-specific installation instructions. Packaged launch checks do not
certify every provider, model, hardware combination, or desktop workflow.

Current source can contain fixes newer than the latest published installer.
Use the release notes for the behavior and qualification of a downloaded build.

## What it provides

- **Continue work within a live session.** Reuse Python variables, imports, and
  helper functions across calls instead of recreating that working state for
  each step. Live state is not a guarantee of recovery after a restart.
  The Python runtime view shows retained sessions and offers explicit release
  controls; closing a generation ends its live objects and background Python work.
- **Combine code and tools.** Work with files, processes, browser/desktop
  operations, and connectors through Python, with inspectable results. The ASTB
  tool-discovery layer exposes capabilities as needed through shared backend services.
- **Inspect execution and changes.** Activity distinguishes failures, stopped
  work, and uncertain outcomes. Bounded previews redact managed or recognized
  credentials and mark omitted content. Review Git changes in collapsible file
  cards with line numbers and bounded expansion of unchanged context. Browse
  a branch's complete delta against its detected or chosen base, browse history
  across branches without checkout, or inspect individually selected commits.
  Branch totals describe net changes; selected-commit totals sum those commits'
  edits. The Changed files sidebar opens on the right when needed; source-line
  links are limited to live changes. Request commits, pushes, or pull requests
  through the agent.
- **Choose supported local or cloud inference.** Use local models, your own API
  keys or custom endpoints, or supported provider-account connections. Available
  features depend on the model, provider, and configuration.
- **Stay involved as work runs.** Steer or cancel execution, with explicit state
  and resource lifetimes. Stopping work does not undo actions already completed.
  Cancelling kernel startup retires that generation without retrying or running
  the cancelled request; a later explicit request can start normally.
- **Adapt tools when needed.** Optional chat-local tool authoring can change
  executable tool methods. Authoring is off by default and does not train model weights.

## Costs and model access

The app requires no VARIANT subscription. You supply the inference: local models
use your hardware, while cloud providers set their own costs, quotas, and account
requirements. OAuth authorizes access; it does not promise free inference.

Model weights and native inference binaries are not stored in this repository.
Google subscription OAuth also requires an authorized client configuration that
this source preview does not bundle. See [Setup](docs/SETUP.md) for requirements
and limitations before choosing a connection.

## Run from source

Use Windows x64, Python **3.13**, Node.js, and Git. Read
[the setup guide](docs/SETUP.md) for runtime prerequisites and optional components.

```powershell
git clone https://github.com/Nocturn3529/VARIANT-1.git
cd VARIANT-1
npm ci
npm run setup:backend
npm start
```

After launch, configure a supported provider or local runtime in the app.
The empty-chat guide helps connect a model, optionally choose a project, and
prepare a first task. Mutation authoring is under the composer's Session tools.
Start with sample files or recoverable copies while learning how a workflow behaves.
The Live2D cat and its floating overlay have been removed.

## Execution and data

**This is not a sandbox.** Python and desktop tools run with your operating-system
permissions. They can change files and operate applications; choosing a project
folder does not restrict them to that folder. Inspect important results and keep
recoverable copies. Cancellation cannot reverse completed effects.

Cloud model requests send selected messages and tool results to the chosen
provider. Using a desktop app does not make cloud inference local.

Live Python state can be lost on restart, reset, eviction, or failure. Optional
portable checkpoints, durable chat records, and approved memory are separate
mechanisms, and not every live object is restorable. Portable checkpointing and
restoration are disabled by default. See [state lifetimes](docs/ARCHITECTURE.md#state-lifetimes).

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md), [validation](docs/VALIDATION.md), and
[release management](docs/RELEASING.md). Report ordinary bugs through Issues with
redacted reproduction steps. For sensitive reports, use [SECURITY.md](SECURITY.md).

Original VARIANT-1 code is available under the [MIT License](LICENSE).
Third-party components retain their own licenses; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
