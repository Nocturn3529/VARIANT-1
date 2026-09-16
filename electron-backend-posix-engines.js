'use strict';

const fs = require('fs');
const path = require('path');

const POSIX_ENGINE_NAMES = Object.freeze(['llama-server', 'whisper-server']);

/**
 * Resolve a POSIX engine executable by verified OS identity only.
 *
 * Linux: readlink(/proc/<pid>/exe). Never select from argv/command text — a
 * process like `tail -f /owned/bin/llama-server` must not be cleaned up.
 * If identity cannot be verified, return '' (caller skips SIGTERM).
 *
 * @param {number|string} pid
 * @param {string} [_command] retained for call-site compatibility; ignored
 * @param {readonly string[]} [names]
 * @param {{ platform?: string, readlinkSync?: Function, resolveDarwinExe?: Function }} [deps]
 */
function resolvePosixEngineExecutable(
  pid,
  _command,
  names = POSIX_ENGINE_NAMES,
  deps = {},
) {
  const cleanPid = Number(pid);
  if (!Number.isFinite(cleanPid) || cleanPid <= 1) return '';

  const allowed = names || POSIX_ENGINE_NAMES;
  const platform = deps.platform || process.platform;
  const readlinkSync = typeof deps.readlinkSync === 'function'
    ? deps.readlinkSync
    : fs.readlinkSync;

  let linked = '';
  if (platform === 'linux') {
    try {
      linked = String(readlinkSync('/proc/' + cleanPid + '/exe') || '');
    } catch (_) {
      return '';
    }
  } else if (platform === 'darwin') {
    if (typeof deps.resolveDarwinExe === 'function') {
      try {
        linked = String(deps.resolveDarwinExe(cleanPid) || '');
      } catch (_) {
        return '';
      }
    } else {
      return '';
    }
  } else {
    return '';
  }

  const exe = linked.replace(/ \(deleted\)$/, '').trim();
  if (!exe || exe === '/') return '';
  if (!allowed.includes(path.basename(exe))) return '';
  return exe;
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
