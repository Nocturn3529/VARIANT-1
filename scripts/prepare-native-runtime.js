'use strict';
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {
  isWindowsX64NativeRecipe,
  hostVenvPython,
} = require('./native-runtime-paths');

const root = path.resolve(__dirname, '..');

if (!isWindowsX64NativeRecipe()) {
  console.error(
    'prepare:native: hash-verified recipe is Windows x64 only (this host is ' +
      process.platform + '/' + process.arch + ').'
  );
  console.error(
    'Ship or download a per-OS llama-server into bin/ (see docs/PORTABILITY_HANDOFF.md M3).'
  );
  console.error(
    'Windows release inputs remain in config/native-runtime.json — run prepare:native on win32/x64.'
  );
  process.exit(2);
}

execFileSync(
  hostVenvPython(root),
  [path.join(__dirname, 'prepare-native-runtime.py'), ...process.argv.slice(2)],
  {cwd: root, stdio: 'inherit', windowsHide: true}
);
