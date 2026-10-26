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

const VERSION = '0.38.0';
const TAG = 'cua-driver-rs-v0.38.0';
const BASE = 'https://github.com/trycua/cua/releases/download/' + TAG + '/';
const APP_NAME = 'CuaDriver.app';
const BUNDLE_ID = 'com.trycua.driver';
const TEAM_IDS = ['4YEC26S9KF', 'YCK386LBJ7'];

const ASSETS = {
  'win32-x64': {
    name: 'cua-driver-rs-0.38.0-windows-x86_64-binary.zip',
    sha256: '5edaa74d0afa1b55a9668dbc4f32736507bd2bd4d75b18ca1dc8881bb12402b5',
  },
  'win32-arm64': {
    name: 'cua-driver-rs-0.38.0-windows-arm64-binary.zip',
    sha256: 'd25c8ebfffb34e4c095ea170baf47b87c35dc0d1116f42e46b39df2bcbff8700',
  },
  'linux-x64': {
    name: 'cua-driver-rs-0.38.0-linux-x86_64-binary.tar.gz',
    sha256: '31ab9709dd91ce73677e49b9f3e75c6f0d3e4d9625c286c9e58262bfbea698dd',
  },
  'linux-arm64': {
    name: 'cua-driver-rs-0.38.0-linux-arm64-binary.tar.gz',
    sha256: '80c9ae66bc7322052dd426c6133c96086b33894bf06c972bca8215384400b33d',
  },
  'darwin-x64': {
    name: 'cua-driver-rs-0.38.0-darwin-universal.tar.gz',
    sha256: '121cf20cafd2cfa69dd720e8967350c6406b22e558be6889de7a1ae3343b6d4b',
  },
  'darwin-arm64': {
    name: 'cua-driver-rs-0.38.0-darwin-universal.tar.gz',
    sha256: '121cf20cafd2cfa69dd720e8967350c6406b22e558be6889de7a1ae3343b6d4b',
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
