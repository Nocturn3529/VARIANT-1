'use strict';

const {execFile} = require('child_process');
const {promisify} = require('node:util');
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
 * @param {{ platform?: string, readlink?: Function, runFile?: Function }} [deps]
 */
async function resolvePosixEngineExecutable(
  pid,
  _command,
  names = POSIX_ENGINE_NAMES,
  deps = {},
) {
  const cleanPid = Number(pid);
  if (!Number.isSafeInteger(cleanPid) || cleanPid <= 1) return '';

  const allowed = names || POSIX_ENGINE_NAMES;
  const platform = deps.platform || process.platform;
  let linked = '';
  if (platform === 'linux') {
    try {
      linked = await (deps.readlink || fs.promises.readlink)('/proc/' + cleanPid + '/exe');
    } catch (_) {
      return '';
    }
  } else if (platform === 'darwin') {
    try {
      const runFile = deps.runFile || promisify(execFile);
      const {stdout} = await runFile('/usr/sbin/lsof', [
        '-a', '-p', String(cleanPid), '-d', 'txt', '-F', 'n',
      ], {encoding: 'utf8', timeout: 2000});
      linked = parseLsofTextPath(stdout);
    } catch (_) {
      return '';
    }
  } else {
    return '';
  }

  const exe = linked.replace(/ \(deleted\)$/, '').trim();
  if (!exe || exe === '/') return '';
  if (!allowed.includes(path.posix.basename(exe))) return '';
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

async function sweepPosixEngines(roots, {platform, runFile, timeout, log, signal = process.kill}) {
  const {stdout} = await runFile('ps', ['-ax', '-o', 'pid=', '-o', 'comm='], {encoding: 'utf8', timeout});
  for (const line of stdout.split('\n')) {
    const row = line.trim().match(/^(\d+)\s+(.+)$/);
    if (!row || !POSIX_ENGINE_NAMES.includes(path.posix.basename(row[2]))) continue;
    const pid = Number(row[1]);
    const exe = await resolvePosixEngineExecutable(pid, '', POSIX_ENGINE_NAMES, {platform, runFile});
    if (!exe) {
      log(`[backend] skip posix engine cleanup pid=${pid}: executable identity unverified`);
    } else if (isOwnedPosixEnginePath(exe, roots)) {
      try { signal(pid, 'SIGTERM'); } catch { /* process exited */ }
    }
  }
}

function isOwnedPosixEnginePath(exePath, roots) {
  if (typeof exePath !== 'string' || !path.posix.isAbsolute(exePath)) return false;
  const exe = path.posix.resolve(exePath);
  return (roots || []).some((root) => {
    if (typeof root !== 'string' || !path.posix.isAbsolute(root)) return false;
    const resolved = path.posix.resolve(root);
    const prefix = resolved.endsWith('/') ? resolved : resolved + '/';
    return exe === resolved || exe.startsWith(prefix);
  });
}

module.exports = {
  POSIX_ENGINE_NAMES,
  parseLsofTextPath,
  sweepPosixEngines,
  resolvePosixEngineExecutable,
  isOwnedPosixEnginePath,
};
