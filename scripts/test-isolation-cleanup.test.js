'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  cleanupIsolatedRoot,
  isInsideRoot,
  makeTreeWritable,
} = require('./test-isolation-cleanup');

const isWin = process.platform === 'win32';
const isRoot = typeof process.getuid === 'function' && process.getuid() === 0;

function modeOf(target) {
  return fs.lstatSync(target).mode & 0o7777;
}

function makeTemp(label) {
  return fs.mkdtempSync(path.join(os.tmpdir(), `variant1-${label}-`));
}

function trySymlink(target, linkPath, type) {
  try {
    fs.symlinkSync(target, linkPath, type);
    return true;
  } catch (error) {
    if (['EPERM', 'EACCES', 'ENOTSUP', 'EEXIST'].includes(error.code)) return false;
    throw error;
  }
}

function tryJunction(target, linkPath) {
  try {
    fs.symlinkSync(target, linkPath, 'junction');
    return true;
  } catch (error) {
    if (['EPERM', 'EACCES', 'ENOTSUP', 'EINVAL', 'EEXIST'].includes(error.code)) return false;
    throw error;
  }
}

test('isInsideRoot rejects siblings and parent paths', () => {
  const root = path.join(os.tmpdir(), 'variant1-root');
  assert.equal(isInsideRoot(root, root), true);
  assert.equal(isInsideRoot(path.join(root, 'child'), root), true);
  assert.equal(isInsideRoot(path.join(root, '..'), root), false);
  assert.equal(isInsideRoot(path.join(root, '..', 'other'), root), false);
});

test('cleanup restores owner bits on a read-only directory and removes it', () => {
  const root = makeTemp('cleanup-readonly');
  const nested = path.join(root, 'locked');
  fs.mkdirSync(nested);
  const filePath = path.join(nested, 'conversations.sqlite3');
  fs.writeFileSync(filePath, 'closed');
  fs.chmodSync(filePath, 0o400);
  fs.chmodSync(nested, 0o500);
  cleanupIsolatedRoot(root);
  assert.equal(fs.existsSync(root), false);
});

test('cleanup skips broken symbolic links and still removes the root', () => {
  const root = makeTemp('cleanup-broken');
  const linkPath = path.join(root, 'broken-link');
  if (!trySymlink(path.join(root, 'missing-target'), linkPath, 'file')) {
    fs.rmSync(root, {recursive: true, force: true});
    return;
  }
  cleanupIsolatedRoot(root);
  assert.equal(fs.existsSync(root), false);
});

test('cleanup does not chmod an external private file through a symlink', {
  skip: isRoot ? 'run as non-root so chmod of foreign files is meaningful' : false,
}, () => {
  const outside = makeTemp('cleanup-outside-file');
  const target = path.join(outside, 'secret.bin');
  fs.writeFileSync(target, 'private');
  fs.chmodSync(target, 0o600);
  const before = modeOf(target);
  const root = makeTemp('cleanup-link-file');
  const linked = trySymlink(target, path.join(root, 'alias.bin'), 'file');
  if (!linked) {
    fs.rmSync(root, {recursive: true, force: true});
    fs.rmSync(outside, {recursive: true, force: true});
    return;
  }
  try {
    makeTreeWritable(root);
    assert.equal(modeOf(target), before);
    assert.equal(fs.readFileSync(target, 'utf8'), 'private');
    cleanupIsolatedRoot(root);
    assert.equal(fs.existsSync(target), true);
    assert.equal(modeOf(target), before);
  } finally {
    fs.rmSync(outside, {recursive: true, force: true});
  }
});

test('cleanup does not chmod an external private directory through a symlink', {
  skip: isRoot ? 'run as non-root so chmod of foreign files is meaningful' : false,
}, () => {
  const outside = makeTemp('cleanup-outside-dir');
  const nested = path.join(outside, 'private-dir');
  fs.mkdirSync(nested);
  const secret = path.join(nested, 'secret.txt');
  fs.writeFileSync(secret, 'keep');
  fs.chmodSync(secret, 0o600);
  fs.chmodSync(nested, 0o700);
  fs.chmodSync(outside, 0o700);
  const beforeDir = modeOf(nested);
  const beforeFile = modeOf(secret);
  const root = makeTemp('cleanup-link-dir');
  const linked = trySymlink(nested, path.join(root, 'alias-dir'), 'dir');
  if (!linked) {
    fs.rmSync(root, {recursive: true, force: true});
    fs.rmSync(outside, {recursive: true, force: true});
    return;
  }
  try {
    makeTreeWritable(root);
    assert.equal(modeOf(nested), beforeDir);
    assert.equal(modeOf(secret), beforeFile);
    assert.equal(fs.readFileSync(secret, 'utf8'), 'keep');
    cleanupIsolatedRoot(root);
    assert.equal(fs.existsSync(nested), true);
    assert.equal(modeOf(nested), beforeDir);
    assert.equal(modeOf(secret), beforeFile);
  } finally {
    fs.chmodSync(nested, 0o700);
    fs.rmSync(outside, {recursive: true, force: true});
  }
});

test('internal directory symlink is unlinked without rewriting the real directory mode', {
  skip: isRoot ? 'run as non-root so directory modes stay distinguishable' : false,
}, () => {
  const root = makeTemp('cleanup-internal-link');
  const realDir = path.join(root, 'a-conversation-sessions');
  fs.mkdirSync(realDir);
  const db = path.join(realDir, 'conversations.sqlite3');
  fs.writeFileSync(db, 'closed-ordinary');
  fs.chmodSync(db, 0o600);
  fs.chmodSync(realDir, 0o700);
  const linked = trySymlink(realDir, path.join(root, 'sessions-alias'), 'dir');
  if (!linked) {
    fs.rmSync(root, {recursive: true, force: true});
    return;
  }
  makeTreeWritable(root);
  assert.equal(fs.lstatSync(path.join(root, 'sessions-alias')).isSymbolicLink(), true);
  if (!isWin) {
    const dirMode = modeOf(realDir);
    assert.equal(dirMode & 0o100, 0o100, `directory search bit must remain, got ${dirMode.toString(8)}`);
    assert.equal(dirMode & 0o022, 0, `group/other write must not be added, got ${dirMode.toString(8)}`);
  }
  cleanupIsolatedRoot(root);
  assert.equal(fs.existsSync(root), false);
});

test('cleanup root that is itself a symlink is not followed for chmod', {
  skip: isRoot ? 'run as non-root so target modes stay distinguishable' : false,
}, () => {
  const target = makeTemp('cleanup-root-target');
  fs.chmodSync(target, 0o700);
  const parent = makeTemp('cleanup-root-parent');
  const linkRoot = path.join(parent, 'isolated-link');
  const linked = trySymlink(target, linkRoot, 'dir');
  if (!linked) {
    fs.rmSync(parent, {recursive: true, force: true});
    fs.rmSync(target, {recursive: true, force: true});
    return;
  }
  try {
    const before = modeOf(target);
    makeTreeWritable(linkRoot);
    assert.equal(modeOf(target), before);
  } finally {
    fs.rmSync(parent, {recursive: true, force: true});
    fs.rmSync(target, {recursive: true, force: true});
  }
});

test('Windows directory junction is skipped when it can be created', {
  skip: isWin ? false : 'Windows junction coverage',
}, () => {
  const outside = makeTemp('cleanup-junction-outside');
  const nested = path.join(outside, 'keep');
  fs.mkdirSync(nested);
  fs.writeFileSync(path.join(nested, 'file.txt'), 'keep');
  const root = makeTemp('cleanup-junction-root');
  const junctionPath = path.join(root, 'alias');
  if (!tryJunction(nested, junctionPath)) {
    fs.rmSync(root, {recursive: true, force: true});
    fs.rmSync(outside, {recursive: true, force: true});
    return;
  }
  try {
    cleanupIsolatedRoot(root);
    assert.equal(fs.existsSync(path.join(nested, 'file.txt')), true);
    assert.equal(fs.readFileSync(path.join(nested, 'file.txt'), 'utf8'), 'keep');
  } finally {
    fs.rmSync(outside, {recursive: true, force: true});
  }
});

test('owner bits are added without flattening to 0777/0666', {
  skip: isWin ? 'POSIX permission bits are not preserved on Windows' : false,
}, () => {
  const root = makeTemp('cleanup-owner-bits');
  const dirPath = path.join(root, 'dir');
  fs.mkdirSync(dirPath);
  const filePath = path.join(dirPath, 'file.txt');
  fs.writeFileSync(filePath, 'x');
  fs.chmodSync(filePath, 0o640);
  fs.chmodSync(dirPath, 0o750);
  makeTreeWritable(root);
  assert.equal(modeOf(dirPath), 0o750);
  assert.equal(modeOf(filePath), 0o640);
  fs.chmodSync(filePath, 0o400);
  fs.chmodSync(dirPath, 0o500);
  makeTreeWritable(root);
  assert.equal(modeOf(dirPath), 0o700);
  assert.equal(modeOf(filePath), 0o600);
  cleanupIsolatedRoot(root);
});
