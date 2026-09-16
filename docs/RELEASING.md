# Release process

Source publication and a qualified installer are separate milestones. This initial
repository publishes source; no current installer is asserted by its existence.

1. Choose an exact source commit and unique application version/tag.
2. Install locked npm/Python dependencies and the build lock in an isolated Windows
   candidate workspace with Python 3.13.
3. Stage the reviewed native runtime and privately supplied test assets. Record
   upstream versions, hashes, licenses and the exact allowed DLL set.
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

The optional Live2D runtime/model files excluded from source must not be silently
added to an installer without their redistribution requirements being established.
Do not ship a developer's model weights, accounts, profiles, test data or logs.
