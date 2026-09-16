'use strict';
// Exercise real registered IPC handlers with native side effects replaced.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const vm = require('node:vm');
const {createRequire} = require('node:module');
const root = path.resolve(__dirname, '..');
const file = path.join(root, 'electron-deck-ipc.js');
const localRequire = createRequire(file);
const handlers = new Map();
const opens = [], reveals = [];
const deck = {}, monitor = {}, overlay = {};
let picks = 0;
const moduleResult = {exports: {}};
vm.runInNewContext(fs.readFileSync(file, 'utf8'), {
  module: moduleResult, exports: moduleResult.exports,
  require: id => id === 'electron' ? {
    ipcMain: {handle: (name, fn) => handlers.set(name, fn), on: () => {}},
    BrowserWindow: {fromWebContents: sender => sender},
    dialog: {showOpenDialog: async () => {picks++; return {canceled: true};}},
    shell: {openPath: async value => {opens.push(value); return '';}, showItemInFolder: value => reveals.push(value)},
  } : localRequire(id),
  __dirname: root, process, Buffer, console, setTimeout, clearTimeout,
}, {filename: file});
moduleResult.exports.registerDeckIpc({app: {}, appRoot: root,
  getDeckWindow: () => deck, getMonitorWindow: () => monitor,
  isTrustedIpcSender: (event, expected) => !!expected && event.sender === expected,
  readSettings: () => ({language: 'en'}),
});

(async () => {
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'variant1-ipc-review-'));
  try {
    const executable = path.join(scratch, 'sample.cmd');
    fs.writeFileSync(executable, 'This fixture must never be executed.');
    for (const sender of [overlay, monitor]) {
      assert.equal(handlers.get('settings:get')({sender}), null);
      assert.equal(await handlers.get('dialog:pickFolder')({sender}), null);
      assert.equal((await handlers.get('localPath:open')({sender}, executable)).ok, false);
    }
    assert.equal(picks, 0);
    assert.equal(handlers.get('settings:get')({sender: deck}).language, 'en');
    await handlers.get('dialog:pickFolder')({sender: deck});
    assert.equal(picks, 1);
    assert.equal((await handlers.get('localPath:open')({sender: deck}, executable)).ok, true);
    assert.deepEqual(reveals, [executable]);
    assert.equal(opens.length, 0, 'file reveal must never launch its file association');
    assert.equal((await handlers.get('localPath:open')({sender: deck}, scratch)).ok, true);
    assert.deepEqual(opens, [scratch]);
    assert.equal((await handlers.get('localPath:open')({sender: deck}, path.join(scratch, 'missing'))).ok, false);
    console.log('Grok review IPC: file reveal, directory opening, missing path, and Deck-only settings/folder access passed');
  } finally {
    // This directory is created and owned by this fixture only.
    assert.equal(path.dirname(path.resolve(scratch)), path.resolve(os.tmpdir()));
    assert.ok(path.basename(scratch).startsWith('variant1-ipc-review-'));
    fs.rmSync(scratch, {recursive: true, force: true});
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
