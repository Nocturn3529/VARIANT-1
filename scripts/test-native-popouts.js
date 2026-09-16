'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {EventEmitter} = require('node:events');
const {createRequire} = require('node:module');
const root = path.resolve(__dirname, '..');
const file = path.join(root, 'electron-native-popouts.js');
const localRequire = createRequire(file);
const handlers = new Map();
const exported = {exports: {}};
const external = [];
vm.runInNewContext(fs.readFileSync(file, 'utf8'), {
  module: exported, exports: exported.exports, URL,
  require: id => id === 'electron' ? {
    ipcMain: {handle: (name, callback) => handlers.set(name, callback)},
    screen: {getDisplayMatching: () => ({workArea: {x: -1920, y: 0, width: 1920, height: 1080}})},
    shell: {openExternal: async url => external.push(url)},
  } : localRequire(id),
}, {filename: file});
const {popoutId, createNativePopoutManager} = exported.exports;
const url = id => `variant1://app/frontend/main-deck/popout.html?surface=${encodeURIComponent(id)}`;
assert.equal(popoutId(url('utility:runtime'), 'variant1-panel:utility:runtime'), 'utility:runtime');
assert.equal(popoutId(url('pane:group-files'), 'variant1-panel:pane:group-files'), 'pane:group-files');
for (const [value, name] of [
  ['https://example.com/?surface=utility:runtime', 'variant1-panel:utility:runtime'],
  ['variant1://app.example/frontend/main-deck/popout.html?surface=utility:runtime', 'variant1-panel:utility:runtime'],
  [url('utility:runtime') + '&surface=utility:overview', 'variant1-panel:utility:runtime'],
  [url('utility:runtime') + '#inject', 'variant1-panel:utility:runtime'],
  [url('utility:runtime'), 'arbitrary-frame'],
  [url('pane:../../other'), 'variant1-panel:pane:../../other'],
  ['variant1://app/frontend/main-deck/index.html?surface=utility:runtime', 'variant1-panel:utility:runtime'],
]) assert.equal(popoutId(value, name), null, `reject ${value}`);

class FakeWindow extends EventEmitter {
  constructor() {
    super(); this.destroyed = false; this.pinned = false; this.minimized = false; this.maximized = false;
    this.webContents = new EventEmitter();
    this.webContents.getURL = () => 'variant1://app/frontend/main-deck/index.html';
    this.webContents.setWindowOpenHandler = callback => { this.openHandler = callback; };
    this.webContents.send = () => {};
  }
  setBounds(bounds) { this.requestedBounds = bounds; }
  getBounds() { return {x: -1820, y: 40, width: 1200, height: 760}; }
  isDestroyed() { return this.destroyed; }
  isMinimized() { return this.minimized; }
  isMaximized() { return this.maximized; }
  isAlwaysOnTop() { return this.pinned; }
  setAlwaysOnTop(value) { this.pinned = value; }
  setMenuBarVisibility() {}
  show() { this.shown = true; }
  focus() { this.focused = true; }
  restore() { this.minimized = false; }
  minimize() { this.minimized = true; }
  maximize() { this.maximized = true; }
  unmaximize() { this.maximized = false; }
  close() { this.destroyed = true; this.emit('closed'); }
  destroy() { this.close(); }
}
const owner = new FakeWindow();
const manager = createNativePopoutManager({appRoot: root, getDeckWindow: () => owner,
  isTrustedIpcSender: (event, expected) => event.trusted === true && expected === owner,
  hardenAppWindow: child => { child.hardened = true; }, log: () => {}});
manager.attach(owner);
const details = {url: url('pane:group-files'), frameName: 'variant1-panel:pane:group-files',
  features: 'width=100000,height=1,left=99999,top=-9999,nodeIntegration=yes,sandbox=no'};
const response = owner.openHandler(details);
assert.equal(response.action, 'allow');
assert.equal(response.outlivesOpener, false);
const options = response.overrideBrowserWindowOptions;
assert.equal(options.parent, undefined, 'native panels are not modal child windows constrained to the Deck');
assert.equal(options.modal, false);
assert.equal(options.movable, true);
assert.equal(options.resizable, true);
assert.equal(options.webPreferences.contextIsolation, true);
assert.equal(options.webPreferences.nodeIntegration, false);
assert.equal(options.webPreferences.sandbox, true);
assert.equal(path.basename(options.webPreferences.preload), 'popout-preload.js');
assert.ok(options.x >= -1920 && options.x + options.width <= 0, 'initial bounds must fit the owner display, including negative coordinates');
assert.ok(options.height >= 240 && options.height <= 1080);
const child = new FakeWindow();
owner.webContents.emit('did-create-window', child, details);
assert.equal(child.hardened, true);
const control = handlers.get('workbench:window:control');
assert.equal(control({trusted: false}, 'pane:group-files', 'close').ok, false);
assert.equal(child.destroyed, false);
assert.equal(control({trusted: true}, 'pane:group-files', 'pin').pinned, true);
assert.equal(control({trusted: true}, 'missing', 'focus').ok, false);
assert.equal(control({trusted: true}, 'pane:group-files', 'arbitrary-method').ok, false);
assert.equal(control({trusted: false}, 'pane:group-files', 'resize', {width:1312,height:880}).ok, false);
assert.equal(control({trusted: true}, 'pane:group-files', 'resize', {width:100000,height:880}).ok, false);
assert.equal(control({trusted: true}, 'pane:group-files', 'resize', {width:1312,height:880}).ok, true);
assert.ok(child.requestedBounds.x >= -1920 && child.requestedBounds.x + child.requestedBounds.width <= 0, 'resizing keeps the browser host on its display');
assert.equal(child.requestedBounds.width,1312);
assert.equal(owner.openHandler(details).action, 'deny', 'reopening a panel focuses the existing native window');
assert.equal(child.focused, true);
owner.webContents.emit('did-navigate');
assert.equal(child.destroyed, true, 'owner navigation cannot strand a stale native rendering host');
assert.equal(control({trusted: true}, 'pane:group-files', 'focus').ok, false);
const html = fs.readFileSync(path.join(root, 'frontend/main-deck/popout.html'), 'utf8');
assert.doesNotMatch(html, /<script\b/i);
assert.match(html, /connect-src 'none'/);
assert.match(html, /script-src 'none'/);
assert.doesNotMatch(fs.readFileSync(path.join(root, 'popout-preload.js'), 'utf8'), /exposeInMainWorld|ipcRenderer/);
console.log('native popouts: URL allowlist, window ownership, display bounds, bridge isolation, controls, and cleanup passed');
