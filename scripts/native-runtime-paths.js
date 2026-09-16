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
  hostVenvPython,
};
