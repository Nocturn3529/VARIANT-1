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
| Branch tip (this doc) | `386f4456` (code + main merge; handoff pin follows) |

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



### ChatGPT re-review round 2 (eb52e83) — 2026-09-16

Review comment: https://github.com/Nocturn3529/VARIANT-1/pull/3#issuecomment-5700671135  
CI at review: [run 35117989259](https://github.com/Nocturn3529/VARIANT-1/actions/runs/35117989259) — Windows `frontend-scripts` **passed**; Linux `backend-linux` **15 failed** / 2783 passed / 22 skipped; smoke **skipped** after suite failure; + `EACCES` cleanup on temp `conversations.sqlite3`.

| ID | SHA | Summary | Local verification |
|----|-----|---------|-------------------|
| **R2.a–c** | `d4c7449e6946809513b7512d593ae1ea58819b47` | Existing key: regular file, owner, mode 0600 (no symlink); decrypt never creates replacement key; `VARIANT1_SECRETSTORE_KEY` forced under test runtime in `conftest.py` + `test-python.js` | `pytest tests/test_secretstore.py` — **17 passed, 2 skipped** (Unix mode/symlink on Windows host) |
| **R3** | `d606824e85f0a83f75a76666cec24c0848a501ee` | Rewrite only `bin/llama-server[.exe]` (and whisper equivalents); preserve `runtimes/.../llama-server` customs | `test_llama_default_binary_resolve` + `test_speech_assets` in combined **32 passed** |
| **R4** | `74548984a71927efd566d28c619a8233efd4e232` | Requires-Dist closure; marker/comment parse; missing package/license = hard error | `test_collect_python_notices.py` included in **32 passed** |
| **R5** | `7bd018027d0a0834fa4b5b0ff87c1f13efe02d01` | `/proc/<pid>/exe` only; no argv fallback | `node scripts/test-electron-backend.js` passed |
| **R6** | same commit | Smoke step `if: always() && !cancelled()` so it runs after a failed suite; sentinel byte accumulation | Needs live Linux CI result on this tip |

Docs notes: `bf4b573c` (R3/R4); this section supersedes tip SHA below.

### Linux CI triage (run 35117989259 — exact nodeids)

| Category | Nodeids | Action |
|----------|---------|--------|
| **A — Windows-assuming fixtures** | `test_desktop_fabric.py` ×5 (`ctypes.windll`); `test_phase12_eval_plan.py::test_canary_seeds_…` (`.venv/Scripts/python.exe`); `test_tools_safety.py::test_file_tools_expand_windows_percent_environment_vars`; `test_llama_runtime_efficiency.py::test_relative_model_pins_…` (expects `.exe` on Linux); `test_inference_platform.py::test_packaged_llamacpp_…` (`bundled-llamacpp` key) | Platform markers / OS-aware paths; **do not** weaken shared security asserts. R3 may already clear the `.exe` pin once CI re-runs. |
| **B — Headless / env** | `test_artifact_unicode_images_hunt.py::test_pdf_preserves_cjk_…` (CJK font); `test_vision_capture.py::test_grab_active_monitor_…` (no `$DISPLAY`) | Install font / xvfb, or skip-with-reason only where justified. |
| **C — Unsupported / env tooling** | `test_file_review.py::test_unchanged_or_non_git_…` (`GitUnavailable: /usr/bin/git`) | Confirm git on runner or mark appropriately. |
| **D — Semantic / integrity** | `test_child_sessions.py::test_child_cancelled_in_spawn_tick_is_durably_terminal` (`completed` vs `cancelled`); `test_extensions_v2.py::test_extension_copy_must_match_preflight_digest`; `test_model_options.py::test_model_options_groups_…` (assert 2==1) | Diagnose as product bugs; keep asserts. |
| **E — Cleanup infra** | `TEST ISOLATION FAILURE` `EACCES` unlink `conversations.sqlite3` under variant1-test-tmp | Close/unlock DB handles before rmtree; unrelated to R2–R5 product fixes. |

Counts: 15 FAILED + 1 isolation cleanup error. Smoke skipped because suite step failed — addressed by `always()` on the smoke step in `7bd01802`.

## CI triage — backend-linux run 35117989259 (eb52e83)

15 pytest failures + EACCES temp cleanup. Categories:

| Category | Nodeids | Action |
|----------|---------|--------|
| Win32 physical desktop | 5× `test_desktop_fabric.py::test_physical_*` (`ctypes.windll`) | **skipif non-win32** (landed) |
| Display capture | `test_vision_capture.py::test_grab_active_monitor_reports_scope` | **skipif** no DISPLAY and not win32 (landed) |
| Windows %env% paths | `test_tools_safety.py::test_file_tools_expand_windows_percent_environment_vars` | **skipif non-win32** (landed) |
| OS-aware llama binary | `test_llama_runtime_efficiency.py::test_relative_model_pins_*` expected `.exe` | **assert uses host binary name** (landed) |
| Win venv layout in canary | `test_phase12_eval_plan.py::test_canary_seeds_*` looks for `Scripts/python.exe` | still open — BackendProcess path seam |
| Fonts | `test_artifact_unicode_images_hunt.py::test_pdf_preserves_cjk_*` | open — Linux font coverage |
| Shared/flaky | child_sessions cancel; extensions digest; inference bundled-llamacpp; model_options count; file_review GitUnavailable | keep asserts; investigate separately |
| Isolation | EACCES unlink `conversations.sqlite3` under pytest tmp | open — SQLite handle leak on Linux |

R6: Linux smoke step now uses `if: always() && !cancelled()` so it runs after pytest failure.

## Remaining gaps (post re-review round 2)

Branch tip at handoff update: `78f642053a3fd20890290c457a1b4a796cbf6492`.

1. Re-run CI on this tip; confirm R6 smoke runs (even if pytest red) and record pass/fail; re-check which category-A items clear.
2. Fixture/marker fixes for remaining category A; diagnose D items without weakening asserts.
3. Live Linux Deck UI chat + Files/Review + unclean-quit orphan sweep.
4. macOS prepare:native + builder evidence.
5. `electron-builder --linux` (and later mac) on a real builder.
6. **Do not merge** until ChatGPT re-review + Nocturn approval.

## Grok takeover — N1 / R2.a / R3 / R4 (2026-09-16)

Paused for ChatGPT re-review. **Do not merge.** PR #3 remains draft. `main` and `C:\Users\noctu\Desktop\VARIANT-1` were not modified.

Linux tests were run on GitHub `ubuntu-latest`, not a local Linux VM. This desktop only ran Windows pytest.

### Review SHAs

- **Tested SHA** (Linux 2820 passed / 29 skipped / 0 failed): `e56275358ec791a9e53b2551c3bd5e72c0fe811a`
- **Code SHA after interim decisions:** `67aee736`
- **Handoff SHA:** this commit (recorded in the branch-tip row after push)

### Review findings addressed

| ID | Change |
|----|--------|
| **N1** | Restored docstring + `from __future__` before ordinary imports in `backend/tests/test_vision_capture.py`. Collection SyntaxError gone. Kept DISPLAY skipif. |
| **R2.a** | `fstat` the opened key fd; validate the real parent-directory chain (reject world-writable / symlink-into-0777). Exclusive create recovers by reading the winner's key. Tests for TOCTOU, parents, corrupt key, concurrent init. Missing-key decrypt and R2.c isolation kept. |
| **R3** | Whisper resolver no longer `lstrip("./")`. Parent-relative `../models/...` and `../../...` preserved; `./` bundled defaults still adapt. Llama resolver unchanged. |
| **R4** | Notice collector retains extras, includes `pkg[extra]` as the package plus extra deps, revisits when a new extra activates edges. Homepage-only metadata is not notice material. Missing extra deps still hard-fail. |

### Linux follow-ups from actual CI tracebacks (after N1 collection)

Cleared on Linux pytest at `e5627535` (run `35132963407`): Git missing-cwd mislabeled as `GitUnavailable`; WindowsPath from patching `os.name`; canary `Scripts/python.exe`; packaged `llama-server.exe`; local-model path grouping; extension restage digest vs copy order; child cancel-in-spawn-tick (later given full pump lifecycle); architecture unowned `create_task`. The STSong CID CJK fallback that helped that run is **not** the portable default; it was removed after the interim review.

### Exact results

**Local Windows** (`node scripts/test-python.js` on this desktop, after N1/R2.a/R3/R4, before Linux follow-ups): **2837 passed, 1 failed, 9 skipped**. Failure: `test_execution_hosts.py::test_signal_acceptance_is_separate_from_observed_effect[False-terminal]` — ConPTY Ctrl+C did not write the SIGINT marker within 5s. Pre-existing host/runtime, not N1/R2/R3/R4. Re-run on the same nodeid failed the same way.

**GitHub Windows backend** run [35128949311](https://github.com/Nocturn3529/VARIANT-1/actions/runs/35128949311) at `d38dda9d` (N1+R2.a+R3+R4 only): **backend job success**. Frontend-scripts success. Linux pytest 9 failed / 2809 passed / 29 skipped (pre-follow-up).

**GitHub tip** run [35132963407](https://github.com/Nocturn3529/VARIANT-1/actions/runs/35132963407) at `e5627535`:

| Job | Result |
|-----|--------|
| frontend-scripts | **success** |
| backend-linux pytest | **2820 passed, 29 skipped, 0 failed** in 353.04s |
| backend-linux smoke | `linux_smoke_ok UnsupportedDesktopAdapter 1` |
| backend-linux job | **failure** — isolation cleanup only |
| backend (Windows) | Isolated suite still **in_progress** at 18:14:01Z on job 104918214770 when this section was written. Prior Windows backend **success** at `d38dda9d` (run 35128949311). |

Linux isolation: `TEST ISOLATION FAILURE: EACCES: permission denied, unlink '.../conversation-sessions2828/conversations.sqlite3'`. On normal Linux, an open file can still be unlinked (EACCES is permissions/ownership/sticky-bit, not “file is open”). Open-handle remains a **hypothesis**. Cleanup now logs euid/egid, lstat mode/owner for the file and parents, symlink targets, chmod errors, mount type, and lsof when present. The failure stays failing.

### Interim reviewer decisions implemented (comment 5702435497)

Code SHA after those decisions: `67aee736`.

1. Review may start with cleanup still red. Failure kept visible. Diagnostics added; cause not established.
2. Removed `UnicodeCIDFont('STSong-Light')` and the synthetic `range(0x4E00, 0xA000)` map. Outline registration now logs path, TTC subfont index, cmap size, and `U+4E2D` membership. Extraction whitespace normalization kept. **Embeddable TrueType-outline CJK qualification is unfinished** (no vendored/subset face this round; Noto TTC still not proven to expose the glyph to ReportLab).
3. Spawn pumps use generation tokens, done-callbacks for exception retrieval/reference cleanup, and `drain_spawn_pumps()` on test teardown. Tests: gated queued cancel, running cancel, already-completed cancel (must not rewrite), pump failure, started scheduler.

Local Windows after those three commits: child/architecture/CJK tests **22 passed**.

### Remaining gaps

1. Linux job red on EACCES temp cleanup after green pytest at `e5627535`. Next Linux run at `67aee73` should print the new diagnostic block; do not chmod-777 or skip it.
2. Portable embedded CJK outline font (source/version/license, actual cmap, PDF font resource, render samples). Not CID.
3. Windows backend job of run 35132963407 may still be running; record its conclusion when available without blocking re-review.
4. Local Windows ConPTY SIGINT observation (`[False-terminal]`).
5. Live Linux Deck UI chat + Files/Review + unclean-quit orphan sweep.
6. macOS `prepare:native` + builder evidence.
7. `electron-builder --linux` (and later mac) on a real builder.
8. **Merging remains Nocturn's decision after ChatGPT full re-review.** PR #3 stays draft.


## ChatGPT re-review of 65e36f4 (comment 5703076728) — R7 / R8 / CJK

Paused for the next ChatGPT re-review. **Do not merge.** PR #3 remains draft. `main` and `C:\Users\noctu\Desktop\VARIANT-1` were not modified.

Review comment: https://github.com/Nocturn3529/VARIANT-1/pull/3#issuecomment-5703076728

### Commits since 65e36f4

| SHA | Change |
| --- | --- |
| `2ddb9590` | **R7.** Isolation cleanup skips symbolic links before chmod/recursion, stays inside the isolated root, and adds only owner write/search bits (`0700`/`0600`). Node tests cover internal/external links, broken links, Windows junctions, and read-only directories. Maintained `test-python.js` run requires those tests and still fails on unexpected cleanup errors. |
| `1c31c64b` | **R8.** `WorkScheduler.run_once()` holds one admission lock across capacity checks and leases. Unstarted compositions get an owned follow-up pump when a slot frees. Non-blocking spawn, queued/running/completed cancel, and per-kind limits kept. |
| `42b0f323` | **CJK.** Vendored TrueType `glyf` subset of Noto Sans SC VF 2.004 (Regular), OFL 1.1, packaged as `Variant1CJK-Regular.ttf`. ReportLab embeds `/FontFile2`. Tests check cmap membership, rendered ink, extractable text, and registration logs. CID stays removed. |
| `9d941c92` | Empty retrigger while the PR was unmergeable with `main` (GitHub skipped `pull_request` CI). |
| `386f4456` | Merge `origin/main` (`d58837b3`) so the draft PR is mergeable again. Keep the Prime Agent notice and the CJK font notice. |

Retained from earlier reviews: N1, R2.a–c, R3, R4, R1, R5 (macOS orphan still deferred), R6, spawn-pump lifecycle, CID removal.

### Exact CI — run 35142327887 at `386f4456`

[GitHub Actions run 35142327887](https://github.com/Nocturn3529/VARIANT-1/actions/runs/35142327887) tests `386f4456c16f77ea8a40ddea2f9dcbffbea23a02`. Overall **success**.

| Check | Observed result |
| --- | --- |
| frontend-scripts | **success** |
| backend-linux cleanup unit tests | 8 passed, 1 skipped (Windows junction) |
| backend-linux pytest | **2836 passed, 29 skipped, 0 failed** in 324.31s |
| backend-linux isolation cleanup | **success** (no EACCES / no `TEST ISOLATION FAILURE`) |
| backend-linux smoke | `linux_smoke_ok UnsupportedDesktopAdapter 1` |
| backend-linux job | **success** |
| backend (Windows) | **2855 passed, 10 skipped** in 2231.10s; job **success** |

Local Windows before push: isolation-cleanup node tests 8 passed / 1 skipped; focused pytest 20 passed (scheduler admission, child simultaneous/restart, CJK qualification, retained cancel/architecture/per-kind tests).

### Remaining gaps

1. Live Linux Deck UI chat + Files/Review + unclean-quit orphan sweep.
2. macOS `prepare:native` + builder evidence.
3. `electron-builder --linux` (and later mac) on a real builder.
4. Local Windows ConPTY SIGINT observation (`test_execution_hosts.py::test_signal_acceptance_is_separate_from_observed_effect[False-terminal]`), author-reported on this desktop, not reproduced in CI.
5. Japanese and Korean PDF claims still need their own evidence. Simplified Chinese now uses the Noto Sans SC Regular and Bold TrueType faces, not the six-character sample. A live Linux desktop pass is still separate.
6. **Merging remains Nocturn's decision after ChatGPT re-review.** PR #3 stays draft.

### Linux VM qualification — 2026-09-23

Detached checkout of `dcb04fa203cd3ced57117ba5020fe2e30a4cbe78` on a Linux desktop VM. No merge, no push.

| Step | Result |
| --- | --- |
| `npm ci` + `setup:backend` | Pass. `requirements.txt`; `backend/.venv/bin/python` 3.13.5 |
| Deck start + one chat turn | Pass. Backend connected. Reply was “My local model isn't loaded yet.” |
| Files + Review | Pass on `/workspace/VARIANT-1-linux-qual` |
| Terminal | Backend PosixPty and `ci-linux-smoke` passed. Deck Open Terminal failed because the UI always requested `powershell`. |
| `prepare:native` + unclean quit | Pass. `kill -9` leaves `llama-server`; the next launch sweeps it. No `whisper-server` in the linux-x64 recipe. |
| Package | AppImage ran with `APPIMAGE_EXTRACT_AND_RUN=1` (FUSE denied on that box). `.deb` failed: no Linux maintainer. |

Follow-up on this branch: Deck terminal open lets the host choose the shell, and `build.linux.maintainer` is set without adding `package.json` `author`.


## Review protocol

1. Focused commits on `port/linux-macos-bootstrap` only.
2. Record SHA + exact tests in this file.
3. When ready: Codex reviews the whole branch → Nocturn approval → merge to `main` (no per-milestone Codex while usage-capped).

## Coordinators

- Grok Bot (New Bot) — coordination, milestones, handoff updates.
- Port Runtime — M1/M2 (backend, kernel, chat, files, terminal).
- Port Packaging — M3/M4/M5 (natives, packaging, desktop seam).
