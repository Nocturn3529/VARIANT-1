'use strict';

/**
 * Validate the native programs in a freshly unpacked installer: exactly the
 * pinned cua-driver, and no bundled llama.cpp (installed in the app instead).
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {spawnSync} = require('child_process');
const {VERSION} = require('./install-cua-driver');

const root = path.join(__dirname, '..');

function packagedBin() {
  const dist = path.join(root, 'dist');
  if (process.platform === 'win32') return path.join(dist, 'win-unpacked', 'resources', 'bin');
  if (process.platform === 'linux') return path.join(dist, 'linux-unpacked', 'resources', 'bin');
  for (const entry of fs.existsSync(dist) ? fs.readdirSync(dist) : []) {
    const app = path.join(dist, entry, 'VARIANT-1.app');
    if (entry.startsWith('mac') && fs.existsSync(app)) return path.join(app, 'Contents', 'Resources', 'bin');
  }
  return path.join(dist, 'mac', 'VARIANT-1.app', 'Contents', 'Resources', 'bin');
}

const runtime = packagedBin();
assert.ok(fs.existsSync(runtime), `missing packaged native runtime: ${runtime}`);
const entries = fs.readdirSync(runtime, {withFileTypes: true});
assert.deepStrictEqual(entries.map(entry => entry.name).sort(), ['cua-driver'],
  'only the desktop driver may ship in resources/bin; llama.cpp is installed in the app');

const name = process.platform === 'win32' ? 'cua-driver.exe' : 'cua-driver';
const driverDir = path.join(runtime, 'cua-driver');
assert.deepStrictEqual(fs.readdirSync(driverDir).sort(), ['VERSION', name].sort(),
  'desktop driver must exclude additional tools, caches, and private state');
assert.strictEqual(fs.readFileSync(path.join(driverDir, 'VERSION'), 'utf8').trim(), VERSION);

const result = spawnSync(path.join(driverDir, name), ['--version'], {
  cwd: driverDir, encoding: 'utf8', windowsHide: true, timeout: 30000,
  env: {...process.env, DO_NOT_TRACK: '1', CUA_DRIVER_RS_TELEMETRY_ENABLED: '0'},
});
assert.strictEqual(result.status, 0, `packaged cua-driver --version failed: ${result.error || result.stderr}`);
assert.match(String(result.stdout || ''), new RegExp(`\\b${VERSION.replace(/\./g, '\\.')}\\b`),
  'packaged cua-driver reports a different version than its pin');

const bytes = fs.statSync(path.join(driverDir, name)).size;
console.log(`packaged native runtime: cua-driver ${VERSION} (${(bytes / 1024 / 1024).toFixed(2)} MiB); no bundled llama.cpp`);
