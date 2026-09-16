'use strict';
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {
  hostVenvPython,
  nativeRuntimeManifestRelPath,
} = require('./native-runtime-paths');

const root = path.resolve(__dirname, '..');
const rel = nativeRuntimeManifestRelPath();
if (!rel) {
  console.error(
    'prepare:native: no recipe mapping for ' + process.platform + '/' + process.arch + '.'
  );
  process.exit(2);
}
const manifestPath = path.join(root, rel);
if (!fs.existsSync(manifestPath)) {
  console.error('prepare:native: missing manifest ' + rel);
  process.exit(2);
}
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
const files = Array.isArray(manifest.files) ? manifest.files : [];
if (manifest.status === 'stub' || files.length === 0) {
  console.error(
    'prepare:native: ' + rel + ' is a stub (no hash-verified archives yet).'
  );
  console.error(
    'Fill files/archives in that manifest, or place ' +
      require('./native-runtime-paths').llamaServerBasename() +
      ' under bin/ manually. See docs/PORTABILITY_HANDOFF.md M3.'
  );
  process.exit(2);
}

execFileSync(
  hostVenvPython(root),
  [
    path.join(__dirname, 'prepare-native-runtime.py'),
    '--manifest',
    manifestPath,
    ...process.argv.slice(2),
  ],
  {cwd: root, stdio: 'inherit', windowsHide: true}
);
