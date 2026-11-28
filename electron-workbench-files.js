'use strict';

const {isUtf8} = require('node:buffer');
const fs = require('node:fs');
const path = require('node:path');
const {createHash, randomUUID} = require('node:crypto');
const {execFile} = require('node:child_process');
const {promisify} = require('node:util');

const MAX_BYTES = 16 * 1024 * 1024;
const runFile = promisify(execFile);
const linkedFileError = 'This file has multiple hard links. Use an editor that updates all linked names together.';

// File.Replace uses ReplaceFileW, which transfers the destination's named streams
// and security metadata. Keep a same-directory recovery copy until it succeeds.
const replaceWindowsFile = [
  "$ErrorActionPreference = 'Stop'",
  '$dst = $env:VARIANT1_EDITOR_TARGET',
  '$src = $env:VARIANT1_EDITOR_STAGE',
  '$backup = $src + ".original"',
  'try {',
  '  $item = Get-Item -Force -LiteralPath $dst',
  "  if ($item.LinkType -eq 'HardLink') { exit 31 }",
  '  if (([DateTimeOffset]$item.LastWriteTimeUtc).ToUnixTimeMilliseconds() -ne [long]$env:VARIANT1_EDITOR_MTIME -or',
  '      ([DateTimeOffset]$item.CreationTimeUtc).ToUnixTimeMilliseconds() -ne [long]$env:VARIANT1_EDITOR_BIRTH) { exit 32 }',
  '  $file = [IO.File]::OpenRead($dst)',
  '  $sha = [Security.Cryptography.SHA256]::Create()',
  '  try { $hash = [BitConverter]::ToString($sha.ComputeHash($file)).Replace("-", "") } finally { $sha.Dispose(); $file.Dispose() }',
  '  if ($hash -ne $env:VARIANT1_EDITOR_REVISION) { exit 32 }',
  '  [IO.File]::Replace($src, $dst, $backup, $false)',
  '} catch {',
  '  if ([IO.File]::Exists($backup) -and -not [IO.File]::Exists($dst)) { [IO.File]::Move($backup, $dst) }',
  '  throw',
  '}',
].join('\n');

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
    if (before.stat.nlink > 1) return {ok: false, error: linkedFileError};
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
      if (latest.stat.nlink > 1) return {ok: false, error: linkedFileError};
      const committed = await io.stat(temp);
      if (process.platform === 'win32') {
        try {
          const powershell = path.join(process.env.SystemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
          await runFile(powershell, ['-NoProfile', '-NonInteractive', '-Command', replaceWindowsFile], {
            windowsHide: true,
            env: {...process.env, VARIANT1_EDITOR_TARGET: physical, VARIANT1_EDITOR_STAGE: temp,
              VARIANT1_EDITOR_REVISION: before.revision, VARIANT1_EDITOR_MTIME: String(Math.floor(before.stat.mtimeMs)),
              VARIANT1_EDITOR_BIRTH: String(Math.floor(before.stat.birthtimeMs))},
          });
        } catch (error) {
          if (error.code === 31) return {ok: false, error: linkedFileError};
          if (error.code === 32) return {ok: false, conflict: true};
          const backup = temp + '.original';
          const retained = await io.stat(backup).then(() => true, () => false);
          return {ok:false, error: 'Windows could not safely replace the file. Close other editors and check write permissions.'
            + (retained ? ' The previous version is at ' + backup : '')};
        }
      } else {
        await io.rename(temp, physical);
      }
      published = true;
      const receipt = {ok: true, revision: createHash('sha256').update(bytes).digest('hex'), mtimeMs: committed.mtimeMs};
      if (process.platform === 'win32') {
        // Merging named streams can advance the published file's last-write time.
        // A concurrent replacement still leaves our committed revision authoritative.
        const actual = await io.stat(physical).catch(() => null);
        receipt.mtimeMs = actual && actual.dev === committed.dev && actual.ino === committed.ino ? actual.mtimeMs : 0;
        try { await io.unlink(temp + '.original'); }
        catch { receipt.warning = 'File saved. The previous version was retained at ' + temp + '.original'; }
      }
      return receipt;
    } finally {
      if (!published) await io.unlink(temp).catch(error => { if (error.code !== 'ENOENT') throw error; });
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
