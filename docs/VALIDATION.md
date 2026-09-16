# Validation

```powershell
npm run test:frontend
npm run test:python
```

`npm test` runs both maintained owners' suites. The Python runner isolates runtime
and configuration paths before importing backend services. Avoid importing
`server.py` casually for inspection: it constructs durable services.

The public source includes synthetic fixtures and the live-canary Python modules
needed by regression tests. It excludes historical run outputs and real-account
benchmark data. Do not run live canaries without configuring your own authorized
accounts and budget. No provider account is supplied by the repository.

Native desktop tests are separate (`npm run test:deck:e2e` and
`npm run test:python:desktop`). Run them in a dedicated environment because they
can create windows/processes. They are not prerequisites for every documentation PR.

Frozen/package tests require built outputs and optional fixture assets.
`test:frozen` currently expects privately staged Kokoro weights for its WAV check.
`test:native:packaged` currently checks a CUDA device, so it does not establish
CPU/no-NVIDIA compatibility. See [release requirements](RELEASING.md).
