# Portability handoff — Linux / macOS

Living document for the cross-platform port. **All port work happens in this worktree only.** Integration into `main` waits on Codex review and Nocturn approval.

## Baseline

| Field | Value |
|-------|-------|
| Worktree | `C:\Users\noctu\Desktop\VARIANT-1-grok-port` |
| Branch | `port/linux-macos-bootstrap` |
| Baseline SHA | `ed36f87d561ab66f2000566f63741b4c7b60b59c` |
| Baseline note | GitHub `main` tip (PR #1 docs merge, 2026-09-16) |
| Upstream | `https://github.com/Nocturn3529/VARIANT-1.git` |
| Original checkout | `C:\Users\noctu\Desktop\VARIANT-1` — **do not modify** for port work |
| Branch tip (this doc) | `dd38bf9ff8ff8819f6a64d4ca6c76048005f9334` |

## Goals

1. Preserve existing architecture and **Windows functionality**.
2. Make backend/kernel + basic chat bootable on **Linux** first; treat macOS as the same Unix family where safe.
3. Progress through small milestones with focused commits and Codex review after each.
4. Prefer platform seams / drivers over scattering `win32` checks.

## Milestone plan

| ID | Scope | Status |
|----|-------|--------|
| M0 | Bootstrap this handoff doc; lock baseline | **done** — `3bd72c7e352d5f0549f084656421bc9f252a5110` |
| M1 | Backend/kernel startup + basic Deck↔backend chat | **code landed** — `d331e2a4393f17283e38583a2b99930ee2c71813` (Linux smoke still pending) |
| M2 | Files/workbench path normalization + Unix PTY (ConPTY Windows-only) | **code landed** - `2c75217514cd8a9474b002d01fd054ad331b4288` |
| M3 | Native runtimes (llama.cpp / packaged backend binaries per OS) | **CPU recipes pinned** ? `88ad2a5b7a3c280d4afcb7208dca035a306bc4dc` (live prepare:native on Linux/mac still pending) |
| M4 | Packaging (`electron-builder` linux + mac targets) | **targets done** — `c8cb94c7885c8df3521a75cd9876ff547f8b2e95`; no Linux builder smoke yet |
| M5 | Desktop automation driver seam; Win32/UIA unchanged; mac/linux stub or reduced | **unsupported seam done** — richer drivers optional |

## Known remaining Windows locks

- **Desktop fabric:** `backend/desktop/*` Win32/UIA (`uiautomation` win32-gated) — M5.
- **Terminal:** ConPTY Windows-only (lazy import); Unix `PosixPtyProcess` path in `execution_hosts/local.py` — live Linux smoke still open.
- **Native recipes:** Windows `native-runtime.json` unchanged; Unix CPU recipes pinned (`native-runtime.*.json`). GPU Unix recipes optional later.
- **Paths:** Review/Files hostSep landed in M2; watch for other UI `\` joins.
- **CI:** `.github/workflows/ci.yml` still `windows-latest` only.

Already softened: electron-builder linux/mac targets; win llama `bin/` under `build.win.extraResources`; setup-backend uses `requirements.txt` off Windows; `process_tree.OwnedProcessTree` has POSIX process-group path; electron POSIX spawn/kill.

## Platforms actually tested

| Platform | What was tested | Result | Date | Commit |
|----------|-----------------|--------|------|--------|
| Windows (host) | Worktree create; `node --check` on touched JS; static packaging/JSON parse | OK | 2026-09-16 | through `93d6c40…` |
| Linux | setup:backend + imports + PosixPty + server listen (no full Deck UI) | **partial OK** | 2026-09-16 | `38481a1…` | **not yet** | — | — |
| macOS | — | not yet | — | — |

## Change log

### M0 — Bootstrap — `3bd72c7e352d5f0549f084656421bc9f252a5110`

- Added `docs/PORTABILITY_HANDOFF.md`.
- No runtime behavior changes.

### M1 — Backend boot seams — `d331e2a4393f17283e38583a2b99930ee2c71813`

- `scripts/setup-backend.js`: non-Windows installs from `requirements.txt` (not win32 `requirements.lock`).
- `electron-backend.js`: POSIX `detached` spawn; `terminateProcessTree` via `kill(-pid)`; SearXNG docker stop on Linux/macOS too.
- Windows `taskkill` / PowerShell orphan sweep unchanged.
- Tests (Windows host): `node --check electron-backend.js`, `node --check scripts/setup-backend.js`.
- **Gap:** no live Linux/macOS boot or Deck↔backend chat yet.

### M4 — electron-builder linux/mac — `c8cb94c7885c8df3521a75cd9876ff547f8b2e95`

- `build.linux` (AppImage, deb) and `build.mac` (dmg, zip); non-Setup `artifactName`.
- Windows llama filters moved under `build.win.extraResources`; win/NSIS Setup naming preserved.
- Tests: `JSON.parse(package.json)` on Windows host; no `electron-builder --linux` run.

### M3 — native path seams — `93d6c40a87259dd78b93ebaa45520c15b55d7777`

- `scripts/native-runtime-paths.js`; prepare/check soft-fail off win32/x64.
- `llama_server` / `runtime_installer` OS-aware default binary name.
- **Gap:** Linux/macOS hash manifests / download recipes.

### M1b - Kernel lease soft-gate + Unix engine orphan sweep - 1a6c8d2e13e281a5a5c6dd766359c24af1600fab

- `backend/kernel_runtime/lease.py`: CREATE_NO_WINDOW only on Windows; POSIX uses `start_new_session` for process-group ownership via `KernelJobObject`/`OwnedProcessTree` (no Win32 Job Object abort). Frozen/packaged PATH rewrite no longer injects `C:\Windows` on Linux/macOS.
- `electron-backend.js`: POSIX orphan cleanup for owned `llama-server` / `whisper-server` under app/resources roots (Windows PowerShell CIM path unchanged).
- Tests (Windows host): `node --check electron-backend.js`; `lease.py` compile/ast imports OK.
- **Gap:** live Linux kernel boot + engine orphan sweep still unverified.

## Codex review handoff (batch 1)

Review these commits on `port/linux-macos-bootstrap` (do not merge without Nocturn + Codex):

1. `3bd72c7e352d5f0549f084656421bc9f252a5110` — M0 docs
2. `d331e2a4393f17283e38583a2b99930ee2c71813` — M1 boot
3. `c8cb94c7885c8df3521a75cd9876ff547f8b2e95` — M4 packaging targets
4. `93d6c40a87259dd78b93ebaa45520c15b55d7777` — M3 native paths (+ handoff updates)

**Preserve Windows.** Highest residual risk: untested on real Linux; packaging targets unsmoked; Unix CPU llama recipes are pinned (b10289); electron-builder --linux still unsmoked on a Linux host.


### M3 — Unix llama recipe stubs

- **SHA:** `c121afe99caee185967bb01cab655319ec6bd713` (scripts); `a5eb8308f46a50e9e1b8264768b3e84994c18205` (tracked stub JSON + gitignore allowlist)
- Tracked stub manifests via `.gitignore` allowlist (config/* was ignoring them).
- Added stub manifests: `config/native-runtime.linux-x64.json`, `linux-arm64`, `darwin-arm64`, `darwin-x64` (`status: stub`, empty files/archives).
- `native-runtime-paths.js` resolves per-OS manifest; `prepare-native-runtime` / `check-native-runtime` consume `--manifest` / stubs with clear exit(2) until hashes are filled.
- Windows `config/native-runtime.json` recipe unchanged.


### M5 — desktop unsupported seam

- **SHA:** `e7412d48a7b7c968dc7913de85337762b943e59d`
- Added `UnsupportedDesktopAdapter` (`backend/desktop_fabric/unsupported.py`).
- `create_desktop_fabric` uses Win32/UIA adapter on Windows only; non-Windows gets explicit unsupported driver (no windll/UIA calls).
- Win32/UIA code paths untouched.


### M5 follow-up

- **SHA:** a3f12e263a536574b488eb295cbe1d1c89019457
- UnsupportedDesktopAdapter._reason is an instance method so platform= overrides show in errors/capability_report.



### M2 - Host path joins + ConPTY lazy import - `2c75217514cd8a9474b002d01fd054ad331b4288`

- `ReviewPanel.tsx` / `FilesPanel.tsx`: `hostSep` / `withTrailingSep` — no hardcoded `\` joins.
- `scripts/workbench-fixture-entry.ts`: parent-dir cut accepts `\` or `/`.
- `execution_hosts/local.py`: lazy-import ConPTY only when `os.name == "nt"`; POSIX uses `PosixPtyProcess` (`pty`/`termios`). ConPTY stays Windows-only.
- Tests (Windows host): static review of path helpers; `ast.parse` on `local.py`.
- **Gap:** live Linux PTY smoke; Files/Review open on a Unix project root.

### M3 — Unix llama recipes pinned (b10289)

- **SHA:** `88ad2a5b7a3c280d4afcb7208dca035a306bc4dc` (ReviewPanel accidental staging undone in `295cb9042e58e522313be7161f9d31588103e605`)
- Hash-verified CPU archives for linux-x64/arm64 and darwin-x64/arm64 (same upstream_tag as Windows).
- `prepare-native-runtime.py` unpacks `.tar.gz` / `.tar.xz` / zip; Unix manifests `status: ready` with llama-server + runtime libs.
- Smoke: extracted linux-x64 recipe to a temp dir on Windows host (22 files including `llama-server`).
- Remaining: live `npm run prepare:native` on Linux/macOS hosts; optional GPU Unix recipes later.

## Linux smoke checklist (Runtime — M1/M2)

Run on a real Linux host against this worktree on `port/linux-macos-bootstrap` (do not use the original VARIANT-1 checkout).

1. `git checkout port/linux-macos-bootstrap && git status -sb` (clean tip).
2. `npm run setup:backend` — log should say it uses `requirements.txt` (not win32 `requirements.lock`); interpreter at `backend/.venv/bin/python`.
3. `npm start` — backend reaches ready via port-file + health; send one Deck chat turn.
4. Open a Linux project root in **Files** and **Review** — paths must join with `/` (no `\\`-only absolute()).
5. Open a terminal session — transport should be `posix_pty` unless PTY open fails (then labeled pipe fallback). Must not load ConPTY off Windows.
6. Optional: if a bundled `llama-server` / `whisper-server` exists under app/resources roots, quit uncleanly once and confirm electron orphan sweep does not leave strays.

Record date + tip SHA + pass/fail in **Platforms actually tested** when done.


### Linux smoke (coordinator box) — 2026-09-16

Host: Linux x86_64, CPython 3.13.5, Node 20. Cloned `port/linux-macos-bootstrap` @ `38481a1`.

| Check | Result |
|-------|--------|
| `npm run setup:backend` | **OK** — used `requirements.txt` (skipped win32 `uiautomation`); venv at `backend/.venv/bin/python`; Playwright Chromium installed |
| `node --check` electron-backend / setup-backend / native-runtime-paths | **OK** |
| `package.json` build.linux / build.mac | **present** |
| `UnsupportedDesktopAdapter` import | **OK** |
| `OwnedProcessTree` POSIX | **OK** (`_is_windows=False`) |
| `spawn_terminal(["/bin/echo", ...])` | **OK** — `PosixPtyProcess` / `transport=posix_pty` |
| `import server` | **OK** |
| hostSep absolute joins (Unix + Windows roots) | **OK** |
| Backend listen | **OK** — logged `listening on 127.0.0.1:37931` (llama-server missing expected without prepare:native; port-file race not asserted) |

**Not run here:** full Electron Deck UI, `prepare:native`, `electron-builder --linux`, Deck chat round-trip.


### Linux prepare:native + soname fix — 2026-09-16

- `npm run prepare:native` on Linux x64 pulled b10289 Ubuntu archive and staged 22 files.
- Bugfix: restore archive soname symlinks (`libllama-common.so.0` etc.) and executable bits; without that `llama-server` was non-executable / missing `.so.0` deps.
- After fix: `llama-server --version` → `version: 10289 (f9e832c10)` on Linux.
- CI: added `backend-linux` job on `ubuntu-latest` (requirements.txt + PosixPty smoke); Windows jobs unchanged.


### ChatGPT review R1-R6 (PR #3 follow-up) — 2026-09-16

Review comment: https://github.com/Nocturn3529/VARIANT-1/pull/3#issuecomment-5699925524  
Prior CI evidence (reviewed head 42b87b5): [run 35112895136](https://github.com/Nocturn3529/VARIANT-1/actions/runs/35112895136) — Windows `frontend-scripts` fail (R1); Linux `backend-linux` 24 failed / 2763 passed / 22 skipped (mix of R2 + fixtures/env); Linux PTY smoke skipped after suite failure.

| ID | SHA | Summary | Local verification |
|----|-----|---------|-------------------|
| **R1** | `edb32babd2a5e425f60ad7f10b33a28ac90286ff` | `test-packaged-files.js` checks win/linux/mac *effective* extraResources; Windows native allowlist kept | Needs `node_modules` (esbuild) for full script; contract logic reviewed |
| **R2** | `8f8f9e1162414683e9469b21ce8bd5fcaaa138b3` | Unix/macOS Fernet secret store (`fernet:` + 0600 keyfile); Windows DPAPI unchanged; `VARIANT1_SECRETSTORE_DISABLED` / `_KEY` | `pytest backend/tests/test_secretstore.py` — **13 passed** |
| **R3** | `d2d349fa1288fd5719ac6718982fc64df499eb7e` | Extensionless `bin/llama-server` / whisper pins; `resolve_llama_binary_relpath` | `pytest …/test_llama_default_binary_resolve.py` — included in **17 passed** with R2 |
| **R4** | `34caacc67de737025da1a0c12330e1296d7bf8e3` | Notices inventory: lock on Windows, `requirements.txt` elsewhere; CPython LICENSE(.txt) | Collector dry-run OK on Windows host |
| **R5** | `a1d3bf17b455feed29aaca219318efc8a11fd1bc` | `electron-backend-posix-engines.js` — `/proc/<pid>/exe` or absolute-path match (spaces OK) | Focused Node assertions on spaced paths — **OK**; full electron test needs Electron module |
| **R6** | same as R5 | `ci-linux-smoke.py` asserts `ci-pty` sentinel, exit 0, `DesktopUnavailable` on `UnsupportedDesktopAdapter`, finally cleanup | Syntax OK on Windows; live assert on Linux CI after push |

Docs SHA for R1/R3/R4 packaging notes: `8fb30109`.

### Linux CI failure triage (categories — from review + R2 fix)

From the 24 failures at 42b87b5 (not re-run yet on this tip):

1. **Unported product (addressed in R2):** credential/OAuth encrypt paths that raised "DPAPI is only available on Windows". Expect those to clear once `backend-linux` re-runs on this tip.
2. **Windows-assuming fixtures:** `ctypes.windll` monkeypatches, `.exe` runtime pins, Windows path/env syntax, live-canary `.venv/Scripts/python.exe`. Fix with platform markers/fixtures — **not** by weakening shared behavior asserts.
3. **Headless / env prerequisites:** CJK font coverage; display-capture without a display. Need xvfb or skip-with-reason only where justified.
4. **Integrity / semantics (separate diagnosis):** immediate child cancellation returning completed; extension staging digest-change detection; temp conversation DB cleanup EACCES.
5. **Smoke was skipped:** R6 hardening only runs when the suite step is green or when smoke is made independent — prefer keeping smoke after a green (or allow-failure-scoped) pytest gate.

A clean Windows suite does **not** settle Linux failures. Re-categorize with exact nodeids after the next `backend-linux` run on this tip.

## Remaining gaps (post R1-R6)

Branch tip at handoff update: `dd38bf9ff8ff8819f6a64d4ca6c76048005f9334`.

1. Re-run CI on pushed tip; confirm R1 clears `frontend-scripts` and R2 shrinks Linux failures; document remaining nodeids by category.
2. Live Linux Deck UI chat + Files/Review + unclean-quit orphan sweep.
3. macOS prepare:native + builder evidence (no macOS CI job yet).
4. `electron-builder --linux` (and later mac) on a real builder.
5. Broader CI (frontend-linux / macOS).
6. **Do not merge** until ChatGPT re-review + Nocturn approval.


## Review protocol

1. Focused commits on `port/linux-macos-bootstrap` only.
2. Record SHA + exact tests in this file.
3. When ready: Codex reviews the whole branch → Nocturn approval → merge to `main` (no per-milestone Codex while usage-capped).

## Coordinators

- Grok Bot (New Bot) — coordination, milestones, handoff updates.
- Port Runtime — M1/M2 (backend, kernel, chat, files, terminal).
- Port Packaging — M3/M4/M5 (natives, packaging, desktop seam).
