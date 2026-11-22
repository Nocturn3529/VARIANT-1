'use strict';

const {isUtf8} = require('node:buffer');
const fs = require('node:fs');
const path = require('node:path');
const {createHash, randomUUID} = require('node:crypto');

const MAX_BYTES = 16 * 1024 * 1024;

// Text editing must round-trip the bytes as UTF-8. An extension is only a
// preview hint; it cannot make a binary/UTF-16 payload safe for the text writer.
function isEditableText(bytes) {
  return isUtf8(bytes) && !bytes.some(byte => byte < 32 && ![9, 10, 13].includes(byte));
}

async function readFileSnapshot(target, io = fs.promises) {
  const file = await io.open(target, fs.constants.O_RDONLY | (fs.constants.O_NONBLOCK || 0));
  try {
    const stat = await file.stat();
    if (!stat.isFile()) return {ok: false, error: 'not_a_file'};
    if (stat.size > MAX_BYTES) return {ok: false, error: 'file_too_large', size: stat.size, mtimeMs: stat.mtimeMs, truncated: true};
    const bytes = Buffer.allocUnsafe(stat.size);
    let offset = 0;
    while (offset < bytes.length) {
      const {bytesRead} = await file.read(bytes, offset, bytes.length - offset, offset);
      if (!bytesRead) break;
      offset += bytesRead;
    }
    const after = await file.stat();
    if (offset !== bytes.length || stat.size !== after.size || stat.mtimeMs !== after.mtimeMs || stat.ctimeMs !== after.ctimeMs) {
      return {ok: false, conflict: true, error: 'File changed while reading it.'};
    }
    return {ok: true, stat, bytes, revision: createHash('sha256').update(bytes).digest('hex')};
  } finally {
    await file.close();
  }
}

/** The textarea uses LF. Preserve original separators on unchanged lines, including a trailing newline. */
function editorBytes(draft, original) {
  const normalized = draft.replace(/\r\n?/g, '\n');
  if (!original.includes('\r')) return Buffer.from(normalized);
  if (!original.includes('\n')) return Buffer.from(normalized.replace(/\n/g, '\r'));
  if (!/(?:^|[^\r])\n|\r(?!\n)/.test(original)) return Buffer.from(normalized.replace(/\n/g, '\r\n'));
  const ends = original.match(/\r\n|\r|\n/g);
  const prior = original.split(/\r\n|\r|\n/), lines = normalized.split('\n');
  let suffix = 0;
  while (suffix < Math.min(prior.length, lines.length) && prior.at(-1 - suffix) === lines.at(-1 - suffix)) suffix++;
  const counts = new Map();
  for (const end of ends) counts.set(end, (counts.get(end) || 0) + 1);
  const usual = [...counts.keys()].sort((a, b) => counts.get(b) - counts.get(a))[0];
  return Buffer.from(lines.map((line, i) => {
    if (i === lines.length - 1) return line;
    const oldIndex = i >= lines.length - suffix ? i + prior.length - lines.length : i < prior.length - suffix ? i : -1;
    return line + (ends[oldIndex] || usual);
  }).join(''));
}

function createEditorWriter(io = fs.promises) {
  const pending = new Map();
  async function save(target, physical, text, mtime, revision) {
    const before = await readFileSnapshot(physical, io);
    if (!before.ok) return before;
    if (!isEditableText(before.bytes)) return {ok: false, error: 'binary_file_not_editable'};
    if ((revision && revision !== before.revision) || (mtime > 0 && before.stat.mtimeMs !== mtime)) {
      return {ok: false, conflict: true, mtimeMs: before.stat.mtimeMs};
    }
    await io.access(physical, fs.constants.W_OK);
    const bytes = editorBytes(text, before.bytes.toString('utf8'));
    if (bytes.length > MAX_BYTES) return {ok: false, error: 'content_too_large'};
    const temp = path.join(path.dirname(physical), '.variant1-' + randomUUID() + '.tmp');
    const file = await io.open(temp, 'wx', 0o600);
    let published = false;
    try {
      try {
        await file.writeFile(bytes);
        await file.chmod(before.stat.mode & 0o777);
        await file.sync();
      } finally { await file.close(); }
      if (await io.realpath(target) !== physical) return {ok: false, conflict: true};
      const latest = await readFileSnapshot(physical, io);
      if (!latest.ok || latest.revision !== before.revision || latest.stat.dev !== before.stat.dev || latest.stat.ino !== before.stat.ino
          || latest.stat.ctimeMs !== before.stat.ctimeMs) return {ok: false, conflict: true};
      const committed = await io.stat(temp);
      await io.rename(temp, physical);
      published = true;
      return {ok: true, revision: createHash('sha256').update(bytes).digest('hex'), mtimeMs: committed.mtimeMs};
    } finally {
      if (!published) await io.unlink(temp);
    }
  }
  return async function write(target, text, mtime, revision) {
    if (Buffer.byteLength(text, 'utf8') > MAX_BYTES) return {ok: false, error: 'content_too_large'};
    const physical = await io.realpath(target);
    const key = process.platform === 'win32' ? physical.toLowerCase() : physical;
    const previous = pending.get(key) || Promise.resolve();
    const job = previous.catch(() => {}).then(() => save(target, physical, text, mtime, revision));
    pending.set(key, job);
    try { return await job; }
    finally { if (pending.get(key) === job) pending.delete(key); }
  };
}

module.exports = {isEditableText, readFileSnapshot, createEditorWriter};
