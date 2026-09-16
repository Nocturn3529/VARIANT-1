'use strict';

const fs = require('fs');
const path = require('path');

const POSIX_ENGINE_NAMES = Object.freeze(['llama-server', 'whisper-server']);

/**
 * Resolve a POSIX engine executable path without splitting on spaces.
 * Prefer /proc/<pid>/exe on Linux; otherwise match an absolute path ending
 * in /llama-server or /whisper-server (directories may contain spaces).
 */
function resolvePosixEngineExecutable(pid, command, names = POSIX_ENGINE_NAMES) {
  const cleanPid = Number(pid);
  if (Number.isFinite(cleanPid) && cleanPid > 1 && process.platform === 'linux') {
    try {
      const linked = fs.readlinkSync('/proc/' + cleanPid + '/exe');
      if (linked) return linked;
    } catch (_) {
      /* fall through */
    }
  }
  const text = String(command || '');
  for (const name of names) {
    const escaped = String(name).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const match = text.match(new RegExp('(?:^|\\s)(/[^\\n]*?/' + escaped + ')(?=\\s|$)'));
    if (match) return match[1];
  }
  return '';
}

function isOwnedPosixEnginePath(exePath, roots) {
  const exe = path.resolve(String(exePath || ''));
  if (!exe) return false;
  return (roots || []).some((root) => {
    const resolved = path.resolve(String(root || ''));
    if (!resolved) return false;
    const prefix = resolved.endsWith(path.sep) ? resolved : resolved + path.sep;
    return exe === resolved || exe.startsWith(prefix);
  });
}

module.exports = {
  POSIX_ENGINE_NAMES,
  resolvePosixEngineExecutable,
  isOwnedPosixEnginePath,
};

