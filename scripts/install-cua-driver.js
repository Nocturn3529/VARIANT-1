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

const VERSION = '0.34.0';
const TAG = 'cua-driver-rs-v0.34.0';
const BASE = 'https://github.com/trycua/cua/releases/download/' + TAG + '/';
const APP_NAME = 'CuaDriver.app';
const BUNDLE_ID = 'com.trycua.driver';
const TEAM_IDS = ['4YEC26S9KF', 'YCK386LBJ7'];

const ASSETS = {
  'win32-x64': {
    name: 'cua-driver-rs-0.34.0-windows-x86_64-binary.zip',
    sha256: 'bcc520e50861c7092cf775846fec76ae386d7dcd6b5b408608b0ea4423a8b888',
  },
  'win32-arm64': {
    name: 'cua-driver-rs-0.34.0-windows-arm64-binary.zip',
    sha256: 'df5786c6e7841d2f0d88f31c627c487181463ed99614b03efc0e907acfc698c3',
  },
  'linux-x64': {
    name: 'cua-driver-rs-0.34.0-linux-x86_64-binary.tar.gz',
    sha256: '629ac96eff829d4dfd5cf221f3f2165c2d813aed91e5efb7b20777a741cd70a7',
  },
  'linux-arm64': {
    name: 'cua-driver-rs-0.34.0-linux-arm64-binary.tar.gz',
    sha256: '9db8b9084add57eb97be8164367b24b6be54ed4f3dc01213e64b72d7fc09fddb',
  },
  'darwin-x64': {
    name: 'cua-driver-rs-0.34.0-darwin-universal.tar.gz',
    sha256: '2d0ade531c07b4d16e8078844fe1b63a0dfa0ee19677c9dcc079b4d3460ab387',
  },
  'darwin-arm64': {
    name: 'cua-driver-rs-0.34.0-darwin-universal.tar.gz',
    sha256: '2d0ade531c07b4d16e8078844fe1b63a0dfa0ee19677c9dcc079b4d3460ab387',
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
