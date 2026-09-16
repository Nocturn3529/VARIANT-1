# Validation

This page describes the available checks and their limits. It is not a claim
that a particular commit or installer has passed them. Record the exact commit,
environment, commands, and results when reporting validation.

## Frontend and backend regression tests

After [source setup](SETUP.md), run:

```powershell
npm run test:frontend
npm run test:python
```

`npm test` runs both maintained suites. The Python runner isolates runtime and
configuration paths before importing backend services. Avoid importing
`server.py` merely to inspect it: importing it constructs durable services.

The public source includes synthetic fixtures and the live-canary Python modules
needed by regression tests. Historical run outputs and real-account benchmark
data are excluded. Live canaries must use your own authorized accounts and an
explicit budget; the repository does not supply a provider account.

## Native desktop tests

Desktop checks run separately:

```powershell
npm run test:deck:e2e
npm run test:python:desktop
```

Use a dedicated environment because these tests can create windows and processes.
They are not prerequisites for every documentation-only pull request.

## Frozen backend and kernel checks

`npm run test:frozen` requires built backend and kernel outputs and the test
prerequisites used by the smoke scripts. The backend smoke test checks excluded
offline speech dependencies, the unconfigured-speech error, and WAV transport
through a local HTTP fixture. It does not require private Kokoro model weights.
The fixture verifies transport, not the quality of real speech synthesis.

See `scripts/test-frozen-backend-smoke.js` and
`scripts/test-frozen-kernel-smoke.js` for the exact prerequisites and assertions.
A successful source run does not replace these frozen-output checks.

## Packaged native-runtime checks

`npm run test:native:packaged` checks the native payload under
`dist/win-unpacked/resources/bin`, loads the packaged server, requires CUDA
device enumeration, and invokes a CPU matrix calculation at one and four threads
without loading an LLM model.

The full command still requires a CUDA device. The CPU matrix check does not
establish end-to-end installation or inference on a machine without NVIDIA
hardware, and device enumeration does not qualify every GPU/model combination.

Complete the clean-system, CPU/no-NVIDIA, installation, update, and advertised
connection checks in [Release process](RELEASING.md) before publishing an installer.
