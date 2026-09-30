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
data are excluded. Live canaries use your own authorized accounts; the repository
does not supply a provider account. Choose cases, repetitions, and usage according
to the situation, the tests needed, and the user's specifications. A spend cap is
optional unless the user specifies one; honor any specified spend or usage limit.
Record observed usage, available cost information, and any uncertainty
in how the selected account or subscription reports consumption.

Repeat task qualification with `experiments/live-canary/qualify.py`, supplying
the provider route, model ID, reasoning effort, cases, and repetitions. The
default gate requires full task completion in three trials, with each case
finishing within its declared timeout. A routing-only desktop result does not
satisfy this gate. For example, use `--model grok --model-id grok-4.7
--reasoning-effort low --oauth-only` for the existing xAI subscription route.

`--max-cost-usd` is optional. When specified for a supported model, a source
backend meter reserves conservative request costs before dispatch. Observed
provider costs replace reservations; missing usage keeps its reservation.
API-equivalent estimates do not certify a subscription's billing or quota rules.
Evidence records exact source identity, route, seeds, outcomes, latency, and
available cost measurements under the locally ignored canary run directory.

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
