'use strict';

const {execFileSync} = require('child_process');
const fs = require('fs');
const path = require('path');

const POSIX_ENGINE_NAMES = Object.freeze(['llama-server', 'whisper-server']);

/**
 * Resolve a POSIX engine executable by verified OS identity only.
 *
 * Linux: readlink(/proc/<pid>/exe). macOS: lsof text-file identity
 * (`/usr/sbin/lsof -a -p <pid> -d txt -F n`). Never select from argv —
 * `tail -f /owned/bin/llama-server` must not be cleaned up.
 * If identity cannot be verified, return '' (caller skips SIGTERM).
 *
 * @param {number|string} pid
 * @param {string} [_command] retained for call-site compatibility; ignored
 * @param {readonly string[]} [names]
 * @param {{ platform?: string, readlinkSync?: Function, resolveDarwinExe?: Function, execFileSync?: Function }} [deps]
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
    try {
      linked = typeof deps.resolveDarwinExe === 'function'
        ? String(deps.resolveDarwinExe(cleanPid) || '')
        : resolveDarwinExecutable(cleanPid, deps);
    } catch (_) {
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

function parseLsofTextPath(output) {
  const paths = [];
  for (const line of String(output || '').split(/\n/)) {
    if (!line.startsWith('n')) continue;
    const value = line.slice(1).replace(/ \(deleted\)$/, '').trim();
    if (value.startsWith('/')) paths.push(value);
  }
  const unique = [...new Set(paths)];
  return unique.length === 1 ? unique[0] : '';
}

function resolveDarwinExecutable(pid, deps = {}) {
  const exec = typeof deps.execFileSync === 'function' ? deps.execFileSync : execFileSync;
  let output = '';
  try {
    output = String(exec(deps.lsofPath || '/usr/sbin/lsof', [
      '-a', '-p', String(pid), '-d', 'txt', '-F', 'n',
    ], {
      encoding: 'utf8',
      timeout: 2000,
      windowsHide: true,
    }) || '');
  } catch (_) {
    return '';
  }
  return parseLsofTextPath(output);
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
  parseLsofTextPath,
  resolveDarwinExecutable,
  resolvePosixEngineExecutable,
  isOwnedPosixEnginePath,
};
