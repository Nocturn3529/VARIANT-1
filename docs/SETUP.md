# Windows source setup

This guide is for running the source preview on Windows. Start with the
[README](../README.md) for the product overview and current release status.
Building a distributable installer is a separate workflow; see
[Release process](RELEASING.md).

Python and desktop tools run with your Windows permissions, not in a sandbox.
Use sample files or recoverable copies for your first workflows. Choosing a
project directory does not create a filesystem security boundary.

## Prerequisites

- Windows x64. Other operating systems are not qualified for this desktop release.
- CPython **3.13 x64** on PATH. The kernel enforces the 3.13 family; other Python
  minor versions are not compatible with this runtime.
- Node.js with npm. The repository's CI uses Node 20. Release validation records
  the exact local Node version when available.
- Git for source checkout and the Git workbench features.

You will also need a supported inference connection: your own cloud provider
credentials/account, or a separately configured local runtime and model.

## Run from source

```powershell
git clone https://github.com/Nocturn3529/VARIANT-1.git
cd VARIANT-1
npm ci
npm run setup:backend
npm start
```

Backend setup creates `backend/.venv`, installs the locked Windows/Python-3.13
dependencies, and attempts to provision Chromium for development. If the browser
download fails, retry setup before using the managed browser. This installation
requires network access; it is not an offline bundle.

The default configuration contains no personal credentials or local model path.
After launch, choose and configure an inference connection in the app.

## Choose inference

### Cloud providers and custom endpoints

Connect your own supported provider credentials, account, or custom endpoint.
Follow the provider's eligibility, subscription, privacy, and usage terms.
Cloud providers set their own costs and quotas; account authorization does not
mean inference is free.

Model availability changes. A provider catalog entry does not mean that every
model supports every tool, image, or reasoning feature. Check the capabilities
of the connection you intend to use.

### Local llama.cpp inference

Obtain an appropriate upstream `llama-server` Windows build from
[llama.cpp releases](https://github.com/ggml-org/llama.cpp/releases). Keep its
matching DLL dependencies together, then configure the executable and your
separately obtained GGUF model in Local Models. Do not mix DLLs from different
builds.

Other supported local runtimes and custom endpoints have their own installation
requirements. No model weights, CUDA redistributables, or native executables are
committed to this repository.

### Native runtimes for release builders

Choosing a runtime in Settings is separate from preparing an installer payload.
The installer recipe expects a reviewed `bin/` manifest. A generic upstream
runtime is not automatically the exact payload required by that recipe.

Release builders use `npm run prepare:native` for the hash-verified native
payload and notices. Record upstream versions, checksums, licenses, the DLL list,
and hardware tests as described in [Release process](RELEASING.md).

## Google subscription OAuth limitation

This public source build does not bundle a third-party Google OAuth client
identity. Google AI subscription login and token refresh require an authorized
client configured through `VARIANT1_GOOGLE_OAUTH_CLIENT_ID` and
`VARIANT1_GOOGLE_OAUTH_CLIENT_SECRET` before app startup.

An arbitrary Google Cloud client is not guaranteed access to the subscription
service. Without an authorized configuration, use another supported inference
connection. Do not commit client credentials or copy another application's
credentials into source.

## Optional speech

The baseline installer configuration excludes offline speech engines and model
weights. Speech setup is separate from the basic source installation.

### Kokoro through an external service

Install and start your own
[OpenAI-compatible speech server](https://github.com/remsky/Kokoro-FastAPI)
following that project's setup instructions. In Voice settings, choose Kokoro
and set Speech server URL to the API base URL, including `/v1` (for example,
`http://127.0.0.1:8880/v1`). Choose a server-supported model and voice, then use
Preview.

VARIANT-1 neither installs nor starts that service. Any Docker or Python
requirements belong to the chosen server, not the baseline app.

### Local Whisper and cloud speech

Local Whisper requires a complete whisper.cpp Windows distribution:
`whisper-server.exe`, its adjacent DLLs, and a compatible ggml `.bin` model in
`%APPDATA%/VARIANT-1/models/speech/whisper`. The app starts this user-provided
sidecar when transcription is requested. Cloud speech uses its provider settings.

### Optional source-only Kokoro engine

Source developers may install `backend/requirements-speech-optional.txt` and
supply the Kokoro ONNX/voices pair for the optional in-process development path.
Installing these dependencies into system Python does not add them to a frozen
application. See [the speech model instructions](../models/speech/README.txt).

## Optional components

External MCP servers, command-line integrations, Docker-backed services, and
provider applications may require separate dependencies. They are not all
supplied by `npm ci`. Speech engines and model weights are user-supplied as
described above.

The Live2D cat, vendor libraries, and floating overlay have been removed.

## Personal state

Packaged Windows installations normally store state under `%APPDATA%\VARIANT-1`.
Development also uses local configuration/runtime roots. Do not commit populated
configuration, databases, tokens, browser profiles, conversations, or attachments.
Back up personal data before trying builds with migration changes.

Live Python state, saved chat records, checkpoints, and approved memory have
different lifetimes. See [Architecture](ARCHITECTURE.md#state-lifetimes) before
relying on state surviving a restart.
