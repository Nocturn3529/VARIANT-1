# Release process

Source publication and a qualified installer are separate milestones. This initial
repository publishes source; no current installer is asserted by its existence.

1. Choose an exact source commit and unique application version/tag.
2. Install locked npm/Python dependencies and the build lock in an isolated Windows
   candidate workspace with Python 3.13.
3. Run `npm run prepare:native` after backend setup. The pinned manifest in
   `config/native-runtime.json` verifies upstream archives and every output DLL.
   It retains the CPU/CUDA runtime and uses official LLVM OpenMP bytes under
   ggml's required import filename, never the Microsoft debug_nonredist binary.
   Use `-- --replace` only to replace existing manifest-listed build inputs.
   The build cache can be supplied with `-- --cache PATH`.
4. Run frontend/backend tests, build the backend/kernel, and run frozen checks.
5. Package the already-tested outputs with
   `npm run dist:electron-only -- --publish never`. `npm run dist` refreezes the
   backend, so verify the newly generated outputs if using that path.
6. Audit final contents, signing status and checksum. Test ordinary-user installation,
   startup, update/data preservation and uninstallation on a clean Windows system,
   including CPU/no-NVIDIA coverage and the connections advertised for that release.
7. Upload a versioned installer, checksums, notes and required notices to a draft
   GitHub prerelease. Publish only after review; update website links afterward.

Do not commit release binaries or replace published bytes under the same version.
The website links to GitHub Releases; it does not need to host the large installer.
Manual updates are acceptable initially. An automatic update feed requires its
own tested channel, metadata, signature and migration behavior.

The Live2D cat and its overlay have been removed, including its model, vendor
libraries, configuration and tray controls. Do not reintroduce those payloads.
Do not ship a developer's model weights, accounts, profiles, test data or logs.

`dist:electron-only` verifies the native input hashes and notices before NSIS.
The renderer build collects license texts for the modules actually bundled by
esbuild; the frozen backend build includes locked Python/CPython notices.
Offline speech engines/weights are excluded. Frozen smoke checks missing local
speech setup and a configured external HTTP WAV fixture; it does not require
private speech model files. Native verification also executes a CPU matrix graph
at one and four threads without loading an LLM model. CUDA device enumeration is
a separate hardware check, not proof of every GPU/model combination.
