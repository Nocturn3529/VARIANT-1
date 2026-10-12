'use strict';

/**
 * Download the pinned cua-driver build into bin/cua-driver during setup.
 * Desktop control has no separate toggle, so the driver is a normal dependency.
 *
 * macOS installs trycua's signed CuaDriver.app, not the bare binary: macOS
 * gives Accessibility and Screen Recording to that app, so the user's grants
 * survive VARIANT-1 updates. The bundle must keep trycua's signature.
 */

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const {execFileSync, spawnSync} = require('child_process');
const {downloadPinnedAsset} = require('./download-pinned-asset');

const VERSION = '0.34.1';
const TAG = 'cua-driver-rs-v0.34.1';
const BASE = 'https://github.com/trycua/cua/releases/download/' + TAG + '/';
const APP_NAME = 'CuaDriver.app';
const BUNDLE_ID = 'com.trycua.driver';
const TEAM_IDS = ['4YEC26S9KF', 'YCK386LBJ7'];

const ASSETS = {
  'win32-x64': {
    name: 'cua-driver-rs-0.34.1-windows-x86_64-binary.zip',
    sha256: '021bc2e5330c80a6c5e2c347c305fafbd4defe195d6282e0d20436f2f13c19bb',
  },
  'win32-arm64': {
    name: 'cua-driver-rs-0.34.1-windows-arm64-binary.zip',
    sha256: '21820a2ae20cb4bb998a340ff87c11007b9eb5cec8d1b8338c7b0d6dc88b01a6',
  },
  'linux-x64': {
    name: 'cua-driver-rs-0.34.1-linux-x86_64-binary.tar.gz',
    sha256: 'a41fe2bdcbeea4501dc594c074f16b0975728e0cc7b08b90acea8564d48fafee',
  },
  'linux-arm64': {
    name: 'cua-driver-rs-0.34.1-linux-arm64-binary.tar.gz',
    sha256: '1c839d35401bbdea20f3e1cf50ace926ef17be6f32cea7e821b02b263c4933dc',
  },
  'darwin-x64': {
    name: 'cua-driver-rs-0.34.1-darwin-universal.tar.gz',
    sha256: '4c7edf1ebbad7e4bc9fac90a4dc4f50d544592e00b35f15b681ccf8b41cb4ff1',
  },
  'darwin-arm64': {
    name: 'cua-driver-rs-0.34.1-darwin-universal.tar.gz',
    sha256: '4c7edf1ebbad7e4bc9fac90a4dc4f50d544592e00b35f15b681ccf8b41cb4ff1',
  },
};

function assetFor(platform, arch) {
  return ASSETS[platform + '-' + arch] || null;
}

function binaryName(platform) {
  return platform === 'win32' ? 'cua-driver.exe' : 'cua-driver';
}

/** The driver executable inside bin/cua-driver for a platform. */
function driverEntry(platform) {
  if (platform === 'darwin') return path.join(APP_NAME, 'Contents', 'MacOS', 'cua-driver');
  return binaryName(platform);
}

function sha256(file) {
  const hash = crypto.createHash('sha256');
  hash.update(fs.readFileSync(file));
  return hash.digest('hex');
}

function findBinary(directory, name, wantDirectory = false) {
  const entries = fs.readdirSync(directory, {withFileTypes: true});
  for (const entry of entries) {
    const full = path.join(directory, entry.name);
    if (entry.name === name && entry.isDirectory() === wantDirectory) return full;
    if (entry.isDirectory() && !entry.name.endsWith('.app')) {
      const nested = findBinary(full, name, wantDirectory);
      if (nested) return nested;
    }
  }
  return '';
}

/**
 * Windows' own bsdtar, which reads zips and drive-letter paths. A GNU tar
 * earlier on PATH (Git Bash, MSYS) takes "C:" for a remote host.
 */
function tarCommand() {
  if (process.platform === 'win32') {
    const systemTar = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'tar.exe');
    if (fs.existsSync(systemTar)) return systemTar;
  }
  return 'tar';
}

/** Refuse anything but trycua's intact, signed CuaDriver.app. */
function verifyMacApp(app) {
  execFileSync('codesign', ['--verify', '--deep', '--strict', app], {stdio: 'pipe'});
  const shown = spawnSync('codesign', ['-dv', app], {encoding: 'utf8'});
  const fields = {};
  for (const line of String(shown.stderr || '').split(/\r?\n/)) {
    const at = line.indexOf('=');
    if (at > 0 && !(line.slice(0, at) in fields)) fields[line.slice(0, at)] = line.slice(at + 1).trim();
  }
  if (shown.status !== 0 || fields.Identifier !== BUNDLE_ID || !TEAM_IDS.includes(fields.TeamIdentifier)) {
    throw new Error('CuaDriver.app is not signed by trycua (' + (fields.Identifier || '?') + ', team ' +
      (fields.TeamIdentifier || '?') + ')');
  }
}

async function installCuaDriver(options = {}) {
  const platform = options.platform || process.platform;
  const arch = options.arch || process.arch;
  const root = options.root || path.resolve(__dirname, '..');
  const asset = assetFor(platform, arch);
  if (!asset) {
    throw new Error('no pinned cua-driver build for ' + platform + '/' + arch);
  }
  const destinationDir = path.join(root, 'bin', 'cua-driver');
  const binary = path.join(destinationDir, driverEntry(platform));
  const stamp = path.join(destinationDir, 'VERSION');
  if (fs.existsSync(binary) && fs.existsSync(stamp) &&
      fs.readFileSync(stamp, 'utf8').trim() === VERSION) {
    console.log('cua-driver ' + VERSION + ' already installed.');
    return binary;
  }
  fs.mkdirSync(destinationDir, {recursive: true});
  const archive = path.join(destinationDir, asset.name);
  const url = BASE + asset.name;
  // An archive left by an interrupted setup is reused only if it matches the pin.
  if (!fs.existsSync(archive) || sha256(archive) !== asset.sha256) {
    console.log('downloading cua-driver ' + VERSION + ' (' + asset.name + ')');
    await downloadPinnedAsset(url, archive, {
      onRetry: ({attempt, attempts, delayMs, error}) => console.warn(
        `cua-driver download attempt ${attempt}/${attempts} failed (${error.code || error.message}); retrying in ${delayMs}ms`,
      ),
    });
  }
  const digest = sha256(archive);
  if (digest !== asset.sha256) {
    fs.rmSync(archive, {force: true});
    throw new Error('cua-driver checksum mismatch');
  }
  const extractDir = path.join(destinationDir, 'extract');
  if (path.dirname(path.resolve(extractDir)) !== path.resolve(destinationDir)) {
    throw new Error('cua-driver extraction path escaped its install directory');
  }
  fs.rmSync(extractDir, {recursive: true, force: true});
  fs.mkdirSync(extractDir, {recursive: true});
  execFileSync(tarCommand(), ['-xf', archive, '-C', extractDir], {stdio: 'inherit'});
  if (platform === 'darwin') {
    const app = findBinary(extractDir, APP_NAME, true);
    if (!app) throw new Error('cua-driver archive did not contain ' + APP_NAME);
    verifyMacApp(app);
    const target = path.join(destinationDir, APP_NAME);
    fs.rmSync(target, {recursive: true, force: true});
    // ditto keeps the bundle's symlinks and metadata, so its signature holds.
    execFileSync('ditto', [app, target], {stdio: 'inherit'});
    verifyMacApp(target);
    fs.rmSync(path.join(destinationDir, 'cua-driver'), {force: true});
  } else {
    const extracted = findBinary(extractDir, binaryName(platform));
    if (!extracted) throw new Error('cua-driver archive did not contain ' + binaryName(platform));
    fs.copyFileSync(extracted, binary);
    if (platform !== 'win32') fs.chmodSync(binary, 0o755);
  }
  fs.writeFileSync(stamp, VERSION + '\n');
  fs.rmSync(extractDir, {recursive: true, force: true});
  fs.rmSync(archive, {force: true});
  console.log('installed ' + binary);
  return binary;
}

module.exports = {
  ASSETS,
  VERSION,
  assetFor,
  driverEntry,
  installCuaDriver,
};

if (require.main === module) {
  installCuaDriver().catch((error) => {
    console.error(error.message || error);
    process.exit(1);
  });
}
