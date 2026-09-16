# Windows source setup

## Optional speech

The baseline installer does not include offline speech engines or model weights.
For Kokoro, install/start a user-owned
[OpenAI-compatible speech server](https://github.com/remsky/Kokoro-FastAPI)
following that project's setup instructions. In Voice settings choose Kokoro,
set Speech server URL to its API base URL including `/v1` (for example
`http://127.0.0.1:8880/v1`), then choose a server-supported model/voice and Preview.
VARIANT-1 neither installs nor starts that service. Docker/Python requirements
belong to the chosen server, not the baseline app.

Local Whisper needs a complete whisper.cpp Windows distribution with
`whisper-server.exe`, its adjacent DLLs and a compatible ggml `.bin` model in
`%APPDATA%/VARIANT-1/models/speech/whisper`. The app starts that user-provided
sidecar when transcription is requested. Cloud speech retains provider settings.

Source developers may install `backend/requirements-speech-optional.txt` and
supply the Kokoro ONNX/voices pair for the optional in-process development path.
Installing it into system Python does not add it to a frozen application.

## Google subscription OAuth limitation

This public source build does not bundle a third-party Google OAuth client
identity. Google AI subscription login and token refresh require an authorized
client configured through `VARIANT1_GOOGLE_OAUTH_CLIENT_ID` and
`VARIANT1_GOOGLE_OAUTH_CLIENT_SECRET` before app startup. An arbitrary Google
Cloud client is not guaranteed access to the subscription service. Without an
authorized configuration, use another supported inference connection. Do not
commit client credentials or copy another application's credentials into source.

## Prerequisites

- Windows x64. Other operating systems are not qualified for this desktop release.
- CPython **3.13 x64** on PATH. The kernel runtime enforces the 3.13 family;
  Other Python minor versions are not a compatible kernel runtime.
- Node.js with npm. Existing CI uses Node 20; the initial public-export validation
  also records its exact local Node version in the release notes when available.
- Git for source checkout and the Git workbench features.

```powershell
git clone https://github.com/Nocturn3529/VARIANT-1.git
cd VARIANT-1
npm ci
npm run setup:backend
npm start
```

Backend setup creates `backend/.venv`, installs the Windows/Python-3.13 dependency
lock, and attempts to provision Chromium for development. A failed browser
download requires retrying setup before using that managed browser. This is a
networked installation, not an offline bundle. The default configuration has no
personal credentials or local model path.

## Choose inference

For cloud use, connect your own supported provider credentials or account. Follow
that provider's eligibility, subscription, privacy, and usage terms. Exact model
availability changes; a provider catalog entry is not a claim that every model
supports every tool, image, or reasoning feature.

For local llama.cpp inference, obtain an appropriate upstream `llama-server`
Windows build from [llama.cpp releases](https://github.com/ggml-org/llama.cpp/releases),
keep its matching DLL dependencies together, and configure its executable and
your separately obtained GGUF model in Local Models. Do not mix DLLs from different
builds. Other supported local/custom endpoints have their own installation requirements.
No model weights, CUDA redistributables, or native executables are committed here.

The current installer recipe expects its reviewed `bin/` manifest. A generic
upstream runtime is not automatically the exact installer payload: the release
builder must record its version, checksums, license, DLL list, and hardware tests.
Release builders use `npm run prepare:native` for the hash-verified native
payload and notices. This is separate from users choosing a runtime in Settings.

## Optional components

The Live2D cat, vendor libraries and floating overlay have been removed.

Speech engines and model weights are user-supplied; see
[the model-drop instructions](../models/speech/README.txt). External MCP servers,
command-line integrations, Docker-backed services, and provider applications can
require separate dependencies. They are not all supplied by `npm ci`.

## Personal state

Packaged Windows installations normally store state under `%APPDATA%\VARIANT-1`.
Development also has local configuration/runtime roots. Do not commit populated
configuration, databases, tokens, browser profiles, conversations, or attachments.
Back up personal data before trying builds with migration changes.

The app's runtime permissions are your Windows permissions. Choosing a project
directory does not create a filesystem security boundary.
