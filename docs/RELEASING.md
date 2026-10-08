# Release process

Publishing source and publishing a qualified Windows installer are separate
milestones. The source preview does not, by itself, establish that an installer
is available or ready for use. This guide is for release builders; users running
from source should follow [Setup](SETUP.md).

## Prepare and validate a candidate

1. Choose an exact source commit and a unique application version/tag.
2. Install the locked npm/Python dependencies and build lock in an isolated
   Windows candidate workspace with Python 3.13.
3. Backend setup installs the pinned cua-driver into `bin/cua-driver`, the
   only bundled native program. llama.cpp is not bundled: users install it from
   Settings > Providers > Local models, against pinned SHA-256 digests.
4. Run the frontend/backend tests, build the backend and kernel, then run the
   frozen checks. See [Validation](VALIDATION.md) for commands and coverage limits.
5. Package the already-tested outputs with
   `npm run dist:electron-only -- --publish never`. Using `npm run dist` refreezes
   the backend, so verify the newly generated outputs when taking that route.
6. Audit the final contents, signing status, and checksum. On a clean Windows
   system, test installation as an ordinary user, startup, updates and data
   preservation, and uninstallation. Include CPU/no-NVIDIA coverage and the
   connections advertised for that release.
7. Push the tag `v<version>` (it must equal `package.json`'s version). CI builds
   and qualifies every platform, then drafts a GitHub release with the installers,
   the update metadata (`preview*.yml`/`latest*.yml` and blockmaps) and
   `SHA256SUMS`. A prerelease version drafts a prerelease. Review the draft, add
   release notes, publish it, then update website links. CI never publishes and
   refuses to replace an existing release.

## What the packaging checks establish

`dist:electron-only` checks that `bin/` holds the pinned cua-driver before packaging.
The renderer build collects license texts for the modules actually bundled by
esbuild. The frozen backend build includes locked Python/CPython notices.

Offline speech engines and weights are excluded. The frozen backend smoke test
checks unconfigured speech and WAV transport through a configured HTTP fixture;
it does not require private speech model files or validate real synthesis quality.

The packaged native test checks that `resources/bin` holds only the pinned
cua-driver and that it reports its pinned version. Local inference is covered by
the in-app llama.cpp install on each platform, which is not part of the package.

## Publish and update responsibly

Do not commit release binaries or replace published bytes under the same version.
The website links to GitHub Releases; it does not need to host the installer.
Installed apps check this repository's published GitHub releases at start and
every 24 hours, and the user can check from Settings > About. Drafts are not
visible to them. Downloading and installing happen only when the user presses
the update buttons. Without a code-signing identity, macOS shows the release page
instead of installing in place.

The Live2D cat and its overlay have been removed, including the model, vendor
libraries, configuration, and tray controls. Do not reintroduce those payloads.
Do not ship a developer's model weights, accounts, profiles, test data, or logs.

## Keep cua-driver current

The `cua-driver bump` workflow runs every Monday. When trycua/cua has a newer
`cua-driver-rs` release, it pins that version in `scripts/install-cua-driver.js`
from GitHub's published digests, checks that the Linux binary reports the version,
and opens or refreshes one draft pull request. Run it by hand from the Actions
tab to pin an exact version. It needs the repository setting that lets GitHub
Actions create pull requests, and pull requests it opens do not start CI on their
own: run CI on the branch, check desktop control on each platform, then merge.
