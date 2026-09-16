'use strict';

/** Shared native binary path helpers - no Windows-only .exe defaults. */

function llamaServerBasename(platform = process.platform) {
  return platform === 'win32' ? 'llama-server.exe' : 'llama-server';
}

function llamaServerRelPath(platform = process.platform) {
  return 'bin/' + llamaServerBasename(platform);
}

function isWindowsX64NativeRecipe(platform = process.platform, arch = process.arch) {
  return platform === 'win32' && arch === 'x64';
}

function nativeArchLabel(arch = process.arch) {
  if (arch === 'x64' || arch === 'x86_64' || arch === 'amd64') return 'x64';
  if (arch === 'arm64' || arch === 'aarch64') return 'arm64';
  return String(arch || '');
}

/** Relative path under repo root, or null when no recipe file exists for the host. */
function nativeRuntimeManifestRelPath(platform = process.platform, arch = process.arch) {
  const a = nativeArchLabel(arch);
  if (platform === 'win32' && a === 'x64') return 'config/native-runtime.json';
  if (platform === 'linux' && (a === 'x64' || a === 'arm64')) {
    return 'config/native-runtime.linux-' + a + '.json';
  }
  if (platform === 'darwin' && (a === 'x64' || a === 'arm64')) {
    return 'config/native-runtime.darwin-' + a + '.json';
  }
  return null;
}

function hostVenvPython(root, platform = process.platform) {
  const path = require('node:path');
  const fs = require('node:fs');
  if (platform === 'win32') {
    return path.join(root, 'backend', '.venv', 'Scripts', 'python.exe');
  }
  const venvPy = path.join(root, 'backend', '.venv', 'bin', 'python');
  if (fs.existsSync(venvPy)) return venvPy;
  return 'python3';
}

module.exports = {
  llamaServerBasename,
  llamaServerRelPath,
  isWindowsX64NativeRecipe,
  nativeArchLabel,
  nativeRuntimeManifestRelPath,
  hostVenvPython,
};
