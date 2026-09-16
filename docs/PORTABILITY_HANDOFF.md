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
| M0 | Bootstrap this handoff doc; lock baseline | **in progress** |
| M1 | Backend/kernel startup + basic Deck↔backend chat | pending |
| M2 | Files/workbench path normalization + Unix PTY (ConPTY Windows-only) | pending |
| M3 | Native runtimes (llama.cpp / packaged backend binaries per OS) | pending |
| M4 | Packaging (`electron-builder` linux + mac targets) | pending |
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

### M0 — Bootstrap (this commit)

- Added `docs/PORTABILITY_HANDOFF.md`.
- No runtime behavior changes.

## Remaining gaps

See milestone table. Highest leverage next: **M1** process/path/python startup so Deck can reach backend on Linux without desktop automation.

## Review protocol

1. Finish milestone with focused commit(s) on `port/linux-macos-bootstrap`.
2. Record final SHA + exact test commands/results in this file.
3. Hand off to Codex for review.
4. Merge to `main` only after Codex review **and** Nocturn approval.

## Coordinators

- Grok Bot (New Bot) — coordination, milestones, handoff updates.
- Port Runtime — M1/M2 (backend, kernel, chat, files, terminal).
- Port Packaging — M3/M4/M5 (natives, packaging, desktop seam).
