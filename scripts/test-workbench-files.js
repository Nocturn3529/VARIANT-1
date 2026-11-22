'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const {createRequire} = require('node:module');
const root = path.resolve(__dirname, '..');
const file = path.join(root, 'electron-deck-ipc.js');
const localRequire = createRequire(file);
const handlers = new Map();
let onTempWrite;
const mockFs = {...fs, promises: {...fs.promises, open: async (...args) => {
  const handle = await fs.promises.open(...args);
  if (args[1] === 'wx' && onTempWrite) {
    const write = handle.writeFile.bind(handle), hook = onTempWrite;
    onTempWrite = null;
    handle.writeFile = bytes => hook(write, bytes);
  }
  return handle;
}}};
const loaded = {exports: {}};
vm.runInNewContext(fs.readFileSync(file, 'utf8'), {module: loaded, exports: loaded.exports,
  require: id => id === 'fs' ? mockFs : id === 'electron' ? {ipcMain: {handle: (name, callback) => handlers.set(name, callback), on: () => {}}} : localRequire(id),
  __dirname: root, process, Buffer, console, setTimeout, clearTimeout}, {filename: file});
const deck = {};
loaded.exports.registerDeckIpc({app: {}, appRoot: root, getDeckWindow: () => deck,
  getMonitorWindow: () => null, isTrustedIpcSender: () => true});
(async () => {
  const read = handlers.get('workbench:fs:readFile');
  const write = handlers.get('workbench:fs:writeFile');
  const dir = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'variant-editor-'));
  const documents = path.join(dir, 'documents'), target = path.join(documents, 'source.txt');
  try {
    await fs.promises.mkdir(documents);
    for (const payload of [Buffer.from([80, 75, 0, 4]), Buffer.from([0xff, 0xfe, 65, 0]), Buffer.from([0xc3, 0x28])]) {
      await fs.promises.writeFile(target, payload);
      const receipt = await read({}, target);
      assert.equal(receipt.binary, true, 'bytes override even a text extension');
      assert.equal(receipt.editable, false);
      assert.equal((await write({}, target, '')).ok, false);
      assert.deepEqual(await fs.promises.readFile(target), payload);
    }
    await fs.promises.writeFile(target, '');
    assert.equal((await read({}, target)).editable, true);
    assert.equal((await write({}, target, 'plain UTF-8 ✓', (await read({}, target)).mtimeMs)).ok, true);
    assert.equal(await fs.promises.readFile(target, 'utf8'), 'plain UTF-8 ✓');
    for (const original of ['a\r\nb\r\n', 'a\nb\n', 'a\r\nb', 'a\rb\r', 'a\r\nb\nc\r\n']) {
      await fs.promises.writeFile(target, original);
      const snapshot = await read({}, target);
      assert.ok((await write({}, target, original.replace(/\r\n?/g, '\n').replace('b', 'B'), snapshot.mtimeMs, snapshot.revision)).ok);
      assert.equal(await fs.promises.readFile(target, 'utf8'), original.replace('b', 'B'));
    }
    const original = await read({}, target);
    onTempWrite = async write => {await write('incomplete'); throw new Error('injected write failure');};
    assert.equal((await write({}, target, 'replacement', original.mtimeMs, original.revision)).ok, false);
    assert.equal(await fs.promises.readFile(target, 'utf8'), original.text);
    assert.deepEqual(await fs.promises.readdir(documents), ['source.txt']);

    let entered, resume;
    const writing = new Promise(resolve => {entered = resolve;});
    const gate = new Promise(resolve => {resume = resolve;});
    onTempWrite = async (write, bytes) => {entered(); await gate; await write(bytes);};
    const shortcut = path.join(dir,'linked-documents');
    await fs.promises.symlink(documents, shortcut, process.platform === 'win32' ? 'junction' : 'dir');
    const first = write({}, target, 'first', original.mtimeMs, original.revision);
    await writing;
    const second = write({}, path.join(shortcut,'source.txt'), 'second', original.mtimeMs, original.revision);
    resume();
    assert.equal((await first).ok, true);assert.equal((await second).conflict, true);
    assert.equal(await fs.promises.readFile(target, 'utf8'), 'first');
    onTempWrite = async (write, bytes) => {await write(bytes); await fs.promises.writeFile(target, 'external edit');};
    assert.equal((await write({}, target, 'stale')).conflict, true);
    assert.equal(await fs.promises.readFile(target, 'utf8'), 'external edit');
    console.log('Workbench files: UTF-8, atomic failure, concurrent conflict, external edits and line endings passed');
  } finally {
    await fs.promises.rm(dir, {recursive: true, force: true});
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
