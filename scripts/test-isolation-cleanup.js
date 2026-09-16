'use strict';

/**
 * Isolated pytest-temp cleanup. Permission repair stays inside the given
 * root, never follows symbolic links, and only adds owner write/search bits.
 */

const fs = require('fs');
const path = require('path');

function isInsideRoot(candidate, root) {
  const resolvedRoot = path.resolve(root);
  const resolvedCandidate = path.resolve(candidate);
  const relative = path.relative(resolvedRoot, resolvedCandidate);
  if (relative === '') return true;
  if (path.isAbsolute(relative)) return false;
  const normalized = relative.split(path.sep).join('/');
  return normalized !== '..' && !normalized.startsWith('../');
}

function lstatOrNull(target) {
  try {
    return fs.lstatSync(target);
  } catch (error) {
    if (error && error.code === 'ENOENT') return null;
    throw error;
  }
}

function ownerWritableMode(stat) {
  const current = stat.mode & 0o7777;
  if (stat.isDirectory()) return current | 0o700;
  return current | 0o600;
}

function recordChmodError(chmodErrors, target, error) {
  chmodErrors.push(`${target}: ${error.code || ''} ${error.message}`.trim());
}

function makeTreeWritable(base, chmodErrors = [], isolatedRoot) {
  const root = path.resolve(isolatedRoot || base);
  const resolved = path.resolve(base);
  if (!isInsideRoot(resolved, root)) {
    recordChmodError(
      chmodErrors,
      resolved,
      new Error(`skipped (outside isolated root ${root})`),
    );
    return chmodErrors;
  }

  const stat = lstatOrNull(resolved);
  if (stat == null) return chmodErrors;
  if (stat.isSymbolicLink()) return chmodErrors;

  try {
    fs.chmodSync(resolved, ownerWritableMode(stat));
  } catch (error) {
    recordChmodError(chmodErrors, resolved, error);
  }

  if (!stat.isDirectory()) return chmodErrors;

  let entries;
  try {
    entries = fs.readdirSync(resolved, {withFileTypes: true});
  } catch (error) {
    recordChmodError(chmodErrors, resolved, error);
    return chmodErrors;
  }

  for (const entry of entries) {
    const full = path.join(resolved, entry.name);
    if (!isInsideRoot(full, root)) continue;
    if (entry.isSymbolicLink()) continue;
    const child = lstatOrNull(full);
    if (child == null || child.isSymbolicLink()) continue;
    if (child.isDirectory()) {
      makeTreeWritable(full, chmodErrors, root);
      continue;
    }
    try {
      fs.chmodSync(full, ownerWritableMode(child));
    } catch (error) {
      recordChmodError(chmodErrors, full, error);
    }
  }
  return chmodErrors;
}

function removeTreeWithRetry(base, timeoutMs = 20000) {
  const sleeper = new Int32Array(new SharedArrayBuffer(4));
  const deadline = Date.now() + timeoutMs;
  let lastError = null;
  while (fs.existsSync(base)) {
    try {
      fs.rmSync(base, {
        recursive: true,
        force: true,
        maxRetries: 2,
        retryDelay: 100,
      });
      return;
    } catch (error) {
      lastError = error;
      if (!['EPERM', 'EBUSY', 'ENOTEMPTY', 'EACCES'].includes(error.code)) throw error;
      if (Date.now() >= deadline) break;
      Atomics.wait(sleeper, 0, 0, 250);
    }
  }
  if (fs.existsSync(base)) throw lastError || new Error(`Could not remove ${base}`);
}

function cleanupIsolatedRoot(base, timeoutMs = 20000) {
  const chmodErrors = makeTreeWritable(base);
  removeTreeWithRetry(base, timeoutMs);
  return chmodErrors;
}

module.exports = {
  cleanupIsolatedRoot,
  isInsideRoot,
  makeTreeWritable,
  removeTreeWithRetry,
};
