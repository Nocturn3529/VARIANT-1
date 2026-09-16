'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {createRequire} = require('node:module');
const root = path.resolve(__dirname, '..');
const file = path.join(root, 'electron-deck-ipc.js');
const localRequire = createRequire(file);
const handlers = new Map();
let bytes = Buffer.from([80, 75, 0, 4]);
let writes = 0;
const mockFs = {...fs, promises: {...fs.promises,
  stat: async () => ({isFile: () => true, size: bytes.length, mtimeMs: 10}),
  readFile: async () => bytes,
  writeFile: async (_path, text) => { writes++; bytes = Buffer.from(text); },
}};
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
  const target = path.join(root, 'virtual-file.txt');
  for (const payload of [Buffer.from([80, 75, 0, 4]), Buffer.from([0xff, 0xfe, 65, 0]), Buffer.from([0xc3, 0x28])]) {
    bytes = payload;
    const receipt = await read({}, target);
    assert.equal(receipt.binary, true, 'bytes override even a text extension');
    assert.equal(receipt.editable, false);
    assert.equal((await write({}, target, '')).ok, false);
    assert.equal(writes, 0, 'a direct IPC write cannot erase binary bytes');
    assert.deepEqual(bytes, payload);
  }
  bytes = Buffer.alloc(0);
  assert.equal((await read({}, target)).editable, true);
  assert.equal((await write({}, target, 'plain UTF-8 ✓', 10)).ok, true);
  assert.equal(bytes.toString(), 'plain UTF-8 ✓');
  console.log('M18: binary detection, direct-write rejection, and empty UTF-8 editing passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
