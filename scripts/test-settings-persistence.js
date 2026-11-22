'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const {createRequire} = require('node:module');
const root = path.resolve(__dirname, '..');
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'variant1-settings-test-'));
const configPath = path.join(directory, 'settings.json');
let failDisk = false;
const fileSystem = {...fs, renameSync: (...args) => {
  if (failDisk) throw Object.assign(new Error('ENOSPC'), {code: 'ENOSPC'});
  return fs.renameSync(...args);
}};
function load(file, dependencies) {
  const filename = path.join(root, file); const localRequire = createRequire(filename); const loaded = {exports: {}};
  vm.runInNewContext(fs.readFileSync(filename, 'utf8'), {module: loaded, exports: loaded.exports,
    require: name => dependencies[name] || localRequire(name), __dirname: root, process, Buffer, console, setTimeout, clearTimeout}, {filename});
  return loaded.exports;
}
try {
  const settings = load('electron-settings-store.js', {fs: fileSystem}).createSettingsStore({configPath, log() {}});
  settings.writeSettings({general: {autoStart: false, startHidden: false}});
  const original = fs.readFileSync(configPath, 'utf8');
  failDisk = true;
  assert.throws(() => settings.writeSettings({general: {startHidden: true}}), /ENOSPC/);
  assert.equal(fs.readFileSync(configPath, 'utf8'), original, 'failed replacement keeps the original complete document');
  assert.deepEqual(fs.readdirSync(directory), ['settings.json'], 'failed writes clean their temporary file');
  const handlers = new Map();
  let registeredArgs = null; let failOs = false;
  const app = {getLoginItemSettings: ({args = []} = {}) => ({openAtLogin: args.join(' ') === registeredArgs}), setLoginItemSettings: value => {
    if (failOs) throw new Error('OS preference rejected');
    registeredArgs = value.openAtLogin ? value.args.join(' ') : null;
  }};
  load('electron-deck-ipc.js', {electron: {ipcMain: {handle: (name, handler) => handlers.set(name, handler), on() {}}}})
    .registerDeckIpc({app, appRoot: root, getDeckWindow: () => ({}), getMonitorWindow: () => null,
      isTrustedIpcSender: () => true, ...settings, log() {}});
  for (const name of ['settings:setStartHidden', 'settings:setLaunchAtLogin']) {
    const result = handlers.get(name)({}, true);
    assert.equal(result.ok, false); assert.match(result.reason, /ENOSPC/);
    assert.equal(registeredArgs, null, 'disk failure rolls the OS preference back');
    assert.equal(fs.readFileSync(configPath, 'utf8'), original);
  }
  failDisk = false; failOs = true;
  assert.equal(handlers.get('settings:setLaunchAtLogin')({}, true).ok, false);
  assert.equal(fs.readFileSync(configPath, 'utf8'), original);
  failOs = false;
  assert.equal(handlers.get('settings:setLaunchAtLogin')({}, true).ok, true);
  assert.equal(settings.readSettings().general.autoStart, true);
  assert.equal(handlers.get('settings:setStartHidden')({}, true).ok, true);
  assert.equal(settings.readSettings().general.startHidden, true);
  function checkLogin(autoStart, startHidden) {
    assert.equal(handlers.get('settings:getLaunchAtLogin')({}), autoStart, 'reopening reads the registered arguments');
    assert.equal(settings.readSettings().general.startHidden, startHidden);
    assert.equal(registeredArgs, autoStart ? (startHidden ? '--hidden' : '') : null);
  }
  checkLogin(true, true);
  const change = (key, value) => assert.equal(handlers.get(key)({}, value).ok, true);
  change('settings:setLaunchAtLogin', false); checkLogin(false, true);
  change('settings:setStartHidden', false); checkLogin(false, false);
  change('settings:setStartHidden', true); checkLogin(false, true);
  change('settings:setLaunchAtLogin', true); checkLogin(true, true);
  change('settings:setStartHidden', false); checkLogin(true, false);
  change('settings:setLaunchAtLogin', false); checkLogin(false, false);
  console.log('M07: atomic replacement, truthful IPC failures, OS rollback, and successful persistence passed');
} finally {
  const resolved = path.resolve(directory);
  if (!resolved.startsWith(path.resolve(os.tmpdir()) + path.sep) || !path.basename(resolved).startsWith('variant1-settings-test-')) throw new Error('Invalid test cleanup path');
  fs.rmSync(resolved, {recursive: true, force: true});
}
