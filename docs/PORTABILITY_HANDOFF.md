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
| Branch tip (this doc) | `93d6c40a87259dd78b93ebaa45520c15b55d7777` |

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
| M2 | Files/workbench path normalization + Unix PTY (ConPTY Windows-only) | **next** (Runtime) |
| M3 | Native runtimes (llama.cpp / packaged backend binaries per OS) | **path seams done** — `93d6c40a87259dd78b93ebaa45520c15b55d7777`; Unix hash recipes still open |
| M4 | Packaging (`electron-builder` linux + mac targets) | **targets done** — `c8cb94c7885c8df3521a75cd9876ff547f8b2e95`; no Linux builder smoke yet |
| M5 | Desktop automation driver seam; Win32/UIA unchanged; mac/linux stub or reduced | **in progress** — unsupported adapter seam |

## Known remaining Windows locks

- **Desktop fabric:** `backend/desktop/*` Win32/UIA (`uiautomation` win32-gated) — M5.
- **Terminal:** ConPTY Windows-only; Unix `pty`/`termios` path exists in `execution_hosts/local.py` — verify/finish in M2.
- **Native recipes:** `config/native-runtime.json` still win32/x64 hash recipe; Linux/mac prepare exits 2 until recipes exist.
- **Paths:** some UI helpers hardcode `\` (e.g. Review `absolute()`) — M2.
- **CI:** `.github/workflows/ci.yml` still `windows-latest` only.

Already softened: electron-builder linux/mac targets; win llama `bin/` under `build.win.extraResources`; setup-backend uses `requirements.txt` off Windows; `process_tree.OwnedProcessTree` has POSIX process-group path; electron POSIX spawn/kill.

## Platforms actually tested

| Platform | What was tested | Result | Date | Commit |
|----------|-----------------|--------|------|--------|
| Windows (host) | Worktree create; `node --check` on touched JS; static packaging/JSON parse | OK | 2026-09-16 | through `93d6c40…` |
| Linux | live `setup:backend` / `npm start` / chat | **not yet** | — | — |
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

**Preserve Windows.** Highest residual risk: untested on real Linux; packaging targets unsmoked; Unix llama recipes are stubs until archive hashes are filled.


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

## Remaining gaps

1. Live Linux smoke: `npm run setup:backend` + backend up + basic chat (M1 closure).
2. M2: FS/git path normalization + Unix PTY verification.
3. M3: fill Unix stub manifests with real llama.cpp archive hashes (stubs tracked).
4. M4: `electron-builder --linux` (and later mac) on a real builder.
5. M5: richer mac/linux desktop drivers beyond unsupported seam (optional).
6. CI ubuntu/macOS jobs.

## Review protocol

1. Focused commits on `port/linux-macos-bootstrap` only.
2. Record SHA + exact tests in this file.
3. Codex review → Nocturn approval → merge to `main`.

## Coordinators

- Grok Bot (New Bot) — coordination, milestones, handoff updates.
- Port Runtime — M1/M2 (backend, kernel, chat, files, terminal).
- Port Packaging — M3/M4/M5 (natives, packaging, desktop seam).
