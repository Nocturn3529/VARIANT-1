'use strict';
/**
 * Release-input gate for bin/: the pinned cua-driver is the only bundled
 * native program. llama.cpp is not bundled; users install it from the app
 * (Settings > Providers > Local models), verified against pinned digests.
 */
const fs = require('node:fs');
const path = require('node:path');
const {VERSION, driverEntry} = require('./install-cua-driver');

const root = path.resolve(__dirname, '..');
const driverDir = path.join(root, 'bin', 'cua-driver');
const binary = path.join(driverDir, driverEntry(process.platform));
const stamp = path.join(driverDir, 'VERSION');

if (!fs.existsSync(binary) || !fs.existsSync(stamp)) {
  console.error('check-native-runtime: missing ' + path.relative(root, binary) +
    ' or its VERSION. Run npm run setup:backend.');
  process.exit(2);
}
const installed = fs.readFileSync(stamp, 'utf8').trim();
if (installed !== VERSION) {
  console.error('check-native-runtime: bin/cua-driver is ' + installed +
    ', but the pinned version is ' + VERSION + '. Run npm run setup:backend.');
  process.exit(2);
}
console.log('Native release inputs: cua-driver ' + VERSION + ' (llama.cpp is installed in the app, not bundled)');
