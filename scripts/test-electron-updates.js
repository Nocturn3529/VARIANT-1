'use strict';

// Update detection and the explicit install path (electron-updates.js), with a
// fake electron-updater: checks never download, and only a downloaded update
// installs, after the backend has been stopped.

const assert = require('assert');
const path = require('path');
const {EventEmitter} = require('events');
const {createUpdateService, installModeFor, resolveFeed, CHECK_INTERVAL_MS} = require('../electron-updates');

const root = path.join(__dirname, '..');
const app = (packaged = true) => ({isPackaged: packaged, getVersion: () => '0.1.1-preview.3'});

function fakeUpdater() {
  const updater = new EventEmitter();
  updater.calls = [];
  updater.next = {isUpdateAvailable: true, updateInfo: {version: '0.1.1-preview.4', releaseName: 'Preview 4'}};
  updater.setFeedURL = feed => updater.calls.push(['feed', feed]);
  updater.checkForUpdates = async () => {
    updater.calls.push(['check']);
    if (updater.next instanceof Error) throw updater.next;
    return updater.next;
  };
  updater.downloadUpdate = token => new Promise((resolve, reject) => {
    updater.calls.push(['download']);
    updater.finishDownload = resolve;
    token.onCancel = () => reject(new Error('cancelled'));
  });
  updater.quitAndInstall = (silent, runAfter) => updater.calls.push(['install', silent, runAfter]);
  return updater;
}

function token() {
  const t = {cancelled: false, onCancel: null, cancel() { t.cancelled = true; if (t.onCancel) t.onCancel(); }};
  return t;
}

function service(overrides = {}) {
  const updater = overrides.updater || fakeUpdater();
  const states = [], order = [];
  let clock = 1000;
  const timers = [];
  const svc = createUpdateService({
    app: overrides.app || app(),
    shell: {openExternal: async url => { order.push(['open', url]); }},
    appRoot: root,
    env: overrides.env || {},
    platform: overrides.platform || 'win32',
    resourcesPath: '',
    log: () => {},
    broadcast: state => states.push(state),
    prepareInstall: async () => { order.push(['prepare']); },
    now: () => (clock += 1000),
    every: (fn, ms) => { timers.push({fn, ms}); return {unref() {}}; },
    stopEvery: () => {},
    loadUpdater: () => updater,
    newCancellation: token,
  });
  return {svc, updater, states, order, timers};
}

async function run() {
  // The feed comes from package.json's GitHub release config; an override must be https.
  assert.deepStrictEqual(resolveFeed({env: {}, appRoot: root}),
    {provider: 'github', owner: 'Nocturn3529', repo: 'VARIANT-1', releaseType: 'prerelease'});
  assert.deepStrictEqual(resolveFeed({env: {VARIANT1_UPDATE_URL: 'https://updates.test/feed'}, appRoot: root}),
    {provider: 'generic', url: 'https://updates.test/feed'});
  assert.strictEqual(resolveFeed({env: {VARIANT1_UPDATE_URL: 'http://updates.test/feed'}, appRoot: root}), null);

  // Development runs never touch the network.
  let t = service({app: app(false)});
  t.svc.start();
  assert.strictEqual(t.svc.getState().status, 'unavailable');
  assert.strictEqual(t.svc.getState().reason, 'dev_mode');
  assert.deepStrictEqual(t.updater.calls, []);
  assert.strictEqual(t.timers.length, 0);

  // Start checks once and schedules a check every 24 hours; nothing downloads.
  t = service();
  t.svc.start();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepStrictEqual(t.updater.calls.map(call => call[0]), ['feed', 'check']);
  assert.strictEqual(t.updater.autoDownload, false);
  assert.strictEqual(t.updater.autoInstallOnAppQuit, false);
  assert.deepStrictEqual(t.timers.map(timer => timer.ms), [CHECK_INTERVAL_MS]);
  let state = t.svc.getState();
  assert.strictEqual(state.status, 'available');
  assert.strictEqual(state.version, '0.1.1-preview.4');
  assert.strictEqual(state.releaseUrl, 'https://github.com/Nocturn3529/VARIANT-1/releases/tag/v0.1.1-preview.4');
  assert.ok(t.states.some(s => s.status === 'checking'), 'the first check is shown as checking');

  // Installing before downloading is refused.
  assert.deepStrictEqual(await t.svc.install(), {ok: false, reason: 'not_downloaded'});

  // A download can be cancelled back to "available".
  let pending = t.svc.download();
  assert.strictEqual(t.svc.getState().status, 'downloading');
  t.updater.emit('download-progress', {percent: 40, transferred: 40, total: 100});
  assert.strictEqual(t.svc.getState().percent, 40);
  assert.deepStrictEqual(t.svc.cancel(), {ok: true});
  assert.deepStrictEqual(await pending, {ok: false, reason: 'cancelled'});
  assert.strictEqual(t.svc.getState().status, 'available');

  // The user downloads, then installs: the backend stops before the installer runs.
  pending = t.svc.download();
  t.updater.finishDownload();
  assert.deepStrictEqual(await pending, {ok: true});
  assert.strictEqual(t.svc.getState().status, 'downloaded');
  const checks = t.updater.calls.filter(call => call[0] === 'check').length;
  await t.timers[0].fn();
  assert.strictEqual(t.updater.calls.filter(call => call[0] === 'check').length, checks,
    'a downloaded update is not re-checked away');
  t.updater.calls.length = 0;
  const installed = await t.svc.install();
  assert.deepStrictEqual(installed, {ok: true});
  assert.deepStrictEqual(t.order, [['prepare']]);
  assert.deepStrictEqual(t.updater.calls, [['install', false, true]], 'the installer is shown, not run silently');

  // Up to date and failed checks.
  t = service();
  t.updater.next = {isUpdateAvailable: false, updateInfo: {version: '0.1.1-preview.3'}};
  state = await t.svc.check();
  assert.strictEqual(state.status, 'up-to-date');
  t.updater.next = new Error('net::ERR_INTERNET_DISCONNECTED\nstack here');
  state = await t.svc.check();
  assert.strictEqual(state.status, 'error');
  assert.strictEqual(state.error, 'net::ERR_INTERNET_DISCONNECTED');

  // macOS previews are unsigned: the update is offered as its release page.
  t = service({platform: 'darwin'});
  await t.svc.check();
  assert.strictEqual(t.svc.getState().installMode, 'release-page');
  assert.deepStrictEqual(await t.svc.download(), {ok: false, reason: 'not_available'});
  await t.svc.openRelease();
  assert.deepStrictEqual(t.order, [['open', 'https://github.com/Nocturn3529/VARIANT-1/releases/tag/v0.1.1-preview.4']]);

  // Install modes per platform.
  const files = (writable, packageType, sudo) => ({
    constants: require('fs').constants,
    accessSync(file, mode) {
      if (mode === require('fs').constants.X_OK && sudo && file.endsWith(sudo)) return;
      if (mode === require('fs').constants.W_OK && writable) return;
      throw new Error('denied');
    },
    readFileSync() { if (packageType) return packageType; throw new Error('missing'); },
  });
  const mode = (platform, env, fileSystem) => installModeFor({platform, env, resourcesPath: '/opt/VARIANT-1/resources', fileSystem});
  assert.strictEqual(mode('win32', {}, files(false)), 'in-app');
  assert.strictEqual(mode('darwin', {}, files(true)), 'release-page');
  assert.strictEqual(mode('linux', {APPIMAGE: '/home/u/VARIANT-1.AppImage'}, files(true)), 'in-app');
  assert.strictEqual(mode('linux', {APPIMAGE: '/opt/VARIANT-1.AppImage'}, files(false)), 'release-page');
  assert.strictEqual(mode('linux', {PATH: '/usr/bin'}, files(false, 'deb', 'pkexec')), 'in-app');
  assert.strictEqual(mode('linux', {PATH: '/usr/bin'}, files(false, 'deb', '')), 'release-page');
  assert.strictEqual(mode('linux', {PATH: '/usr/bin'}, files(false, '', 'pkexec')), 'release-page');

  console.log('electron updates: start + 24 h checks, button-only download/cancel/install, backend stopped before install, platform install modes passed');
}

run().catch(error => { console.error(error); process.exitCode = 1; });
