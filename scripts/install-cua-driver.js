'use strict';

/**
 * Download the pinned cua-driver build into bin/cua-driver during setup.
 * Desktop control has no separate toggle, so the driver is a normal dependency.
 */

const crypto = require('crypto');
const fs = require('fs');
const https = require('https');
const path = require('path');
const {execFileSync} = require('child_process');

const VERSION = '0.28.2';
const TAG = 'cua-driver-rs-v0.28.2';
const BASE = 'https://github.com/trycua/cua/releases/download/' + TAG + '/';

const ASSETS = {
  'win32-x64': {
    name: 'cua-driver-rs-0.28.2-windows-x86_64-binary.zip',
    sha256: '1f4bfceeab64cb7f56be7aad774c3dc2d2910d1427e4be1d79939c706e8029ba',
  },
  'win32-arm64': {
    name: 'cua-driver-rs-0.28.2-windows-arm64-binary.zip',
    sha256: '578b88ff2dd56f06eb7e984d73aaf5e76f59c6fde9542c967d6a30d00213c680',
  },
  'linux-x64': {
    name: 'cua-driver-rs-0.28.2-linux-x86_64-binary.tar.gz',
    sha256: 'a1d99fd04bb4927ef5ffdbe60eb91ed8b51a2bab60e10fc604a75bd59ce69c3e',
  },
  'linux-arm64': {
    name: 'cua-driver-rs-0.28.2-linux-arm64-binary.tar.gz',
    sha256: '55e8a32839a4ac369a773df4dac87b345bd4567779221ade4a5e39223a45a2e8',
  },
  'darwin-x64': {
    name: 'cua-driver-rs-0.28.2-darwin-universal-binary.tar.gz',
    sha256: '386db225a3080714a0f9f935525e61efaf46709587ef8b94dd2df81aeb2f6daa',
  },
  'darwin-arm64': {
    name: 'cua-driver-rs-0.28.2-darwin-universal-binary.tar.gz',
    sha256: '386db225a3080714a0f9f935525e61efaf46709587ef8b94dd2df81aeb2f6daa',
  },
};

function assetFor(platform, arch) {
  return ASSETS[platform + '-' + arch] || null;
}

function binaryName(platform) {
  return platform === 'win32' ? 'cua-driver.exe' : 'cua-driver';
}

function download(url, destination) {
  return new Promise((resolve, reject) => {
    const file = fs.createWriteStream(destination);
    const request = https.get(url, {headers: {'user-agent': 'variant1-setup'}}, (response) => {
      if (response.statusCode >= 300 && response.statusCode < 400 && response.headers.location) {
        file.close();
        fs.rmSync(destination, {force: true});
        download(response.headers.location, destination).then(resolve, reject);
        return;
      }
      if (response.statusCode !== 200) {
        file.close();
        fs.rmSync(destination, {force: true});
        reject(new Error('download failed: HTTP ' + response.statusCode));
        return;
      }
      response.pipe(file);
      file.on('finish', () => file.close(resolve));
    });
    request.on('error', reject);
    file.on('error', reject);
  });
}

function sha256(file) {
  const hash = crypto.createHash('sha256');
  hash.update(fs.readFileSync(file));
  return hash.digest('hex');
}

function findBinary(directory, name) {
  const entries = fs.readdirSync(directory, {withFileTypes: true});
  for (const entry of entries) {
    const full = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      const nested = findBinary(full, name);
      if (nested) return nested;
    } else if (entry.name === name) {
      return full;
    }
  }
  return '';
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
  const binary = path.join(destinationDir, binaryName(platform));
  const stamp = path.join(destinationDir, 'VERSION');
  if (fs.existsSync(binary) && fs.existsSync(stamp) &&
      fs.readFileSync(stamp, 'utf8').trim() === VERSION) {
    console.log('cua-driver ' + VERSION + ' already installed.');
    return binary;
  }
  fs.mkdirSync(destinationDir, {recursive: true});
  const archive = path.join(destinationDir, asset.name);
  const url = BASE + asset.name;
  console.log('downloading cua-driver ' + VERSION + ' (' + asset.name + ')');
  await download(url, archive);
  const digest = sha256(archive);
  if (digest !== asset.sha256) {
    fs.rmSync(archive, {force: true});
    throw new Error('cua-driver checksum mismatch');
  }
  const extractDir = path.join(destinationDir, 'extract');
  fs.rmSync(extractDir, {recursive: true, force: true});
  fs.mkdirSync(extractDir, {recursive: true});
  execFileSync('tar', ['-xf', archive, '-C', extractDir], {stdio: 'inherit'});
  const extracted = findBinary(extractDir, binaryName(platform));
  if (!extracted) throw new Error('cua-driver archive did not contain ' + binaryName(platform));
  fs.copyFileSync(extracted, binary);
  if (platform !== 'win32') fs.chmodSync(binary, 0o755);
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
  installCuaDriver,
};

if (require.main === module) {
  installCuaDriver().catch((error) => {
    console.error(error.message || error);
    process.exit(1);
  });
}
