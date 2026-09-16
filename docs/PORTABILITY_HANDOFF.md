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

## Goals

1. Preserve existing architecture and **Windows functionality**.
2. Make backend/kernel + basic chat bootable on **Linux** first; treat macOS as the same Unix family where safe.
3. Progress through small milestones with focused commits and Codex review after each.
4. Prefer platform seams / drivers over scattering `win32` checks.

## Milestone plan

| ID | Scope | Status |
|----|-------|--------|
| M0 | Bootstrap this handoff doc; lock baseline | **done** — 3bd72c7e352d5f0549f084656421bc9f252a5110 |
| M1 | Backend/kernel startup + basic Deck↔backend chat | **in progress** (Runtime) |
| M2 | Files/workbench path normalization + Unix PTY (ConPTY Windows-only) | pending |
| M3 | Native runtimes (llama.cpp / packaged backend binaries per OS) | **in progress** — path seams; win x64 recipe unchanged |
| M4 | Packaging (`electron-builder` linux + mac targets) | **in progress** — linux/mac targets added; win/NSIS preserved |
| M5 | Desktop automation driver seam; Win32/UIA unchanged; mac/linux stub or reduced | pending |

## Known Windows locks (pre-port scan)

- **Packaging:** `package.json` build only defines `win` / NSIS; artifact name assumes Setup.
- **Bundled natives:** `extraResources` references Windows `llama-server.exe` + DLLs; SETUP documents Windows llama builds only.
- **Desktop fabric:** `backend/desktop/*` uses `ctypes.windll`, UIA (`uiautomation` win32-gated), elevation, SendInput.
- **Terminal:** `backend/execution_hosts/windows_conpty.py` + ConPTY path; Unix `pty`/`termios` path already present in `local.py`.
- **Kernel leases:** Windows Job Objects for child cleanup (see ARCHITECTURE).
- **Electron backend spawn:** `electron-backend.js` already branches exe name / python vs python3; orphan cleanup still PowerShell/CIM on Windows only.
- **Paths:** some UI/helpers hardcode `\` joins (e.g. Review `absolute()`); git porcelain uses `/`.

## Platforms actually tested

| Platform | What was tested | Result | Date | Commit |
|----------|-----------------|--------|------|--------|
| Windows (host) | Worktree create from baseline; clean tree on branch | OK | 2026-09-16 | `ed36f87…` |
| Linux | — | not yet | — | — |
| macOS | — | not yet | — | — |

## Change log

### M0 — Bootstrap

- **SHA:** `3bd72c7e352d5f0549f084656421bc9f252a5110`
- Added `docs/PORTABILITY_HANDOFF.md`.
- No runtime behavior changes.
- Platforms tested: Windows host (worktree create + commit only). Linux/macOS: not yet.

### M4 — electron-builder linux/mac targets

- Added `build.linux` (AppImage, deb) and `build.mac` (dmg, zip) with non-Setup `artifactName`.
- Moved Windows `bin/` llama DLL/`llama-server.exe` filters under `build.win.extraResources` so Unix packaging is not tied to Windows natives.
- Kept `win`/`nsis` and Windows Setup `artifactName` unchanged in behavior.
- Platforms tested: config validated on Windows host (no full `electron-builder --linux` run here).

### M3 — native binary path seams

- Added `scripts/native-runtime-paths.js` (`llama-server` vs `llama-server.exe`).
- `prepare-native-runtime.js` / `check-native-runtime.js`: clear exit(2) on non-win32/x64 instead of hard throw; Windows x64 recipe unchanged (`config/native-runtime.json`).
- `model_runtime.llama_server` + `runtime_installer` default bundled binary is OS-aware (no shared `.exe`-only default).
- Remaining: per-OS hash manifests / download recipes for Linux and macOS llama builds.

### M1 — Backend boot seams (Runtime)

- **SHA:** d331e2a4393f17283e38583a2b99930ee2c71813
- `scripts/setup-backend.js`: on non-Windows, install from `requirements.txt` (platform markers) instead of the win32-targeted `requirements.lock` (`pywin32` / `uiautomation`).
- `electron-backend.js`: POSIX spawn uses `detached: true`; `terminateProcessTree` uses process-group `kill(-pid)`; SearXNG `docker stop` safety net no longer skipped on Linux/macOS.
- Windows spawn/kill paths unchanged (`taskkill /T`, PowerShell orphan sweep).

**Tests run (Windows host):**
- `node --check electron-backend.js`
- `node --check scripts/setup-backend.js`
- Static review of `requirements.lock` header (`Target environment: ... win32`) and `requirements.txt` `uiautomation ; sys_platform == "win32"`

**Not yet verified:** live `npm run setup:backend` / `npm start` on Linux or macOS; Deck↔backend chat round-trip on Unix.

## Remaining gaps

See milestone table. Runtime: **M1** process/path/python startup. Packaging: finish Unix llama hash manifests (M3) and smoke `electron-builder --linux` on a Linux host (M4); M5 desktop stub still pending.

## Review protocol

1. Finish milestone with focused commit(s) on `port/linux-macos-bootstrap`.
2. Record final SHA + exact test commands/results in this file.
3. Hand off to Codex for review.
4. Merge to `main` only after Codex review **and** Nocturn approval.

## Coordinators

- Grok Bot (New Bot) — coordination, milestones, handoff updates.
- Port Runtime — M1/M2 (backend, kernel, chat, files, terminal).
- Port Packaging — M3/M4/M5 (natives, packaging, desktop seam).
