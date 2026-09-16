'use strict';
// Exercise startup/tray behavior without creating native windows.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {EventEmitter} = require('node:events');
const {createRequire} = require('node:module');
const root = path.resolve(__dirname, '..');
function load(name, electron) {
  const file = path.join(root, name), local = createRequire(file), module = {exports: {}};
  vm.runInNewContext(fs.readFileSync(file, 'utf8'), {
    module, exports: module.exports, require: id => id === 'electron' ? electron : local(id),
    __dirname: root, process: {...process, argv: []}, console, Buffer, URL, setTimeout, clearTimeout,
  }, {filename: file});
  return module.exports;
}
(async () => {
  for (const startHidden of [false, true]) {
    const app = new EventEmitter(), calls = [];
    Object.assign(app, {requestSingleInstanceLock: () => true, whenReady: async () => {}, quit() {}, isPackaged: false});
    const boot = load('electron-app-boot.js', {globalShortcut: {register: () => true, unregisterAll() {}}});
    boot.registerAppLifecycle({app, protocol: {registerFileProtocol() {}}, appRoot: root,
      initDataDir() {}, setPortFile() {}, configurePermissionHandlers() {},
      readSettings: () => ({general: {startHidden}}), openDeckWindow: () => {calls.push('deck');},
      installTray: () => calls.push('tray'), startBackend: () => calls.push('backend'), stopBackend() {},
      getAutoUpdater: () => null, getDeckWindow: () => null, setQuitting() {}, log() {},
      getUserDataBackendJsonPath: () => 'fixture.json'});
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(calls, startHidden ? ['tray', 'backend'] : ['deck', 'tray', 'backend']);
    app.emit('activate');
    assert.equal(calls.at(-1), 'deck', 'activate opens the workbench, never an avatar');
  }
  let menu;
  class Tray {setToolTip() {} setContextMenu(value) {menu = value;} on() {}}
  load('electron-tray.js', {Tray, Menu: {buildFromTemplate: rows => rows},
    nativeImage: {createFromPath: () => ({isEmpty: () => true}), createEmpty: () => ({})},
  }).createTray({appRoot: root, openDeckWindow() {}, openMonitor() {}, quitApp() {}});
  assert.ok(menu.some(row => row.label === 'Quit VARIANT-1'));
  assert.ok(menu.some(row => row.label === 'Open Main Deck'));
  assert.ok(!menu.some(row => /overlay|avatar|cat/i.test(row.label || '')));
  const handlers = {}, preview = {setPermissionRequestHandler() {}, setPermissionCheckHandler() {}};
  const deck = {}, chat = {}, foreign = {};
  const security = load('electron-app-boot.js', {BrowserWindow: {fromWebContents: contents => contents.owner},
    session: {defaultSession: {setPermissionRequestHandler: fn => {handlers.request = fn;},
      setPermissionCheckHandler: fn => {handlers.check = fn;}}, fromPartition: () => preview},
  }).createWindowSecurity({getDeckWindow: () => deck, getMonitorWindow: () => null,
    getChatWindows: () => [chat], log() {}});
  security.configurePermissionHandlers();
  const url = 'variant1://app/frontend/main-deck/index.html';
  for (const owner of [deck, chat, foreign]) {
    const contents = {owner, getURL: () => url};
    const details = {requestingUrl: url, mediaTypes: ['audio']};
    let accepted;
    handlers.request(contents, 'media', value => {accepted = value;}, details);
    assert.equal(accepted, owner !== foreign, 'microphone request must work without a retired avatar getter');
    assert.equal(handlers.check(contents, 'media', url, details), owner !== foreign);
    assert.equal(handlers.check(contents, 'media', url, {...details, mediaTypes: ['video']}), false);
  }
  const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
  for (const name of ['react', 'react-dom', 'p5', '@xterm/xterm']) {
    assert.ok(pkg.devDependencies[name], `${name} must remain available to esbuild`);
    assert.equal(pkg.dependencies[name], undefined, `${name} must not be copied a second time into app.asar`);
  }
  for (const file of ['electron-overlay.js', 'preload.js', 'frontend/renderer.js', 'frontend/index.html', 'config/animations.json']) {
    assert.ok(!fs.existsSync(path.join(root, file)), `retired overlay file remains: ${file}`);
  }
  console.log('Overlay removal: workbench/tray startup, hidden launch, activation and package dependencies passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
