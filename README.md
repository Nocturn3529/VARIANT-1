# VARIANT-1

**A persistent AI workspace for coding, research, and desktop work.**

VARIANT-1 is a Windows desktop application for AI power users. It combines a
persistent CPython environment, composable tools, and supported local or cloud
models in an Electron/React workspace.

[Website](https://variant-1-silk.vercel.app/) · [Setup](docs/SETUP.md) ·
[Architecture](docs/ARCHITECTURE.md) · [Issues](https://github.com/Nocturn3529/VARIANT-1/issues)

## Status

This is the first public **source preview**, under active development. A new
public Windows installer is still being prepared. Downloadable installers will
appear in [GitHub Releases](https://github.com/Nocturn3529/VARIANT-1/releases)
after their build and installation checks are complete. This source publication
does not certify every provider, model, or desktop workflow.

## What it provides

- Persistent Python variables, imports, and helpers across calls in a live kernel.
- ASTB: discoverable tools composed through Python and canonical host services.
- Optional chat-local tool mutation, with authoring off by default.
- Files, processes, browser/desktop operations, connectors, and inspectable results.
- Supported local models, your own API keys/custom endpoints, and provider-account connections.
- User steering and cancellation, with explicit state and resource lifetimes.

The app requires no VARIANT subscription. Users supply inference: local models
use their hardware; cloud providers set their own costs, quotas, and account
requirements. OAuth is an authorization method, not a promise of free inference.

## Run from source

Use Windows x64, Python **3.13**, Node.js, and Git. Read [the setup guide](docs/SETUP.md)
for model/runtime prerequisites and optional components.

```powershell
npm ci
npm run setup:backend
npm start
```

Choose and configure a supported provider or local runtime in the app. Model
weights and native inference binaries are not stored in this repository.
The Live2D cat and its floating overlay have been removed.
Google subscription OAuth additionally requires an authorized client configuration;
this source preview does not bundle one. See [setup limitations](docs/SETUP.md).

## Execution and data

Python and desktop tools run with your operating-system permissions; this is
not a sandbox. They can change files and operate applications. Inspect important
results and use recoverable copies for unfamiliar workflows. Stopping execution
does not reverse completed effects.

Cloud model requests send selected messages and tool results to the chosen
provider. Live Python state can be lost on restart, reset, eviction, or failure.
Optional portable checkpoints, durable chat records, and approved memory are
different mechanisms; not every live object is restorable.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md), [validation](docs/VALIDATION.md), and
[release management](docs/RELEASING.md). Report ordinary bugs through Issues with
redacted reproduction steps. For sensitive reports, use [SECURITY.md](SECURITY.md).

Original VARIANT-1 code is available under the [MIT License](LICENSE).
Third-party components retain their own licenses; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
