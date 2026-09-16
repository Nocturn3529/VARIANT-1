'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const {EventEmitter} = require('node:events');

const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'variant1-download-ipc-'));
const source = fs.readFileSync(path.join(__dirname, '../electron-browser-downloads.js'), 'utf8');
const owner = {}, nativeOwner = {}, outsider = {};
const guests = new Map([7, 8, 9].map(id => [id, {id, hostWebContents: id === 8 ? nativeOwner : id === 9 ? outsider : owner, getType: () => 'webview', isDestroyed: () => false}]));
const managers = [];
function harness(dataDir = temporary, getRetainedGuest) {
  const handlers = new Map(), partition = new EventEmitter(), module = {exports: {}};
  const electron = {app: {whenReady: () => Promise.resolve(), isReady: () => true},
    ipcMain: {handle: (name, fn) => handlers.set(name, fn)},
    session: {fromPartition: () => partition}, webContents: {fromId: id => guests.get(id)}};
  const deck = {webContents: owner, isDestroyed: () => false};
  owner.send = () => {};
  vm.runInNewContext(source, {module, require: id => id === 'electron' ? electron : require(id), setTimeout, clearTimeout});
  const manager = module.exports.registerBrowserDownloads({getDataDir: () => dataDir, getDeckWindow: () => deck,
    isTrustedIpcSender: event => event.trusted === true, isNativeHost: contents => contents === nativeOwner, getRetainedGuest});
  managers.push(manager);
  return {manager, partition,
    bind: (tab, guest, operation, trusted = true, sender=owner) => handlers.get('workbench:browser:bind')({trusted,sender}, tab, guest, operation),
    command: (command, trusted = true) => handlers.get('workbench:browser:downloads')({trusted}, command)};
}
function item(body = 'fixture bytes') {
  const value = new EventEmitter();
  Object.assign(value, {path: '', getFilename: () => '../../untrusted-name.bin', getURL: () => 'https://fixture.test/download',
    getURLChain: () => ['https://fixture.test/redirect', 'https://fixture.test/download'], getTotalBytes: () => Buffer.byteLength(body), getReceivedBytes: () => Buffer.byteLength(body),
    setSavePath(file) {this.path = file; fs.writeFileSync(file, body);}, cancel() {this.emit('done', {}, 'cancelled');}});
  return value;
}
async function until(predicate) {
  for (let i = 0; i < 100; i++) {if (await predicate()) return; await new Promise(resolve => setTimeout(resolve, 10));}
  throw new Error('Download lifecycle did not settle');
}
(async () => {
  if (process.argv.includes('--composition-only')) return testAppRootComposition();
  const managed={id:10,hostWebContents:null,getType:()=> 'window',isDestroyed:()=>false};guests.set(10,managed);
  const retained=harness(path.join(temporary,'retained'), id=>id===10?{contents:managed,owner,tabId:'retained'}:null);
  await retained.manager.ready;
  const earlyItem=item();retained.partition.emit('will-download',{},earlyItem,managed);earlyItem.emit('done',{},'completed');
  await until(async()=> (await retained.command({action:'drain_downloads',tab_id:'retained'})).downloads.length===1);
  assert.equal(retained.bind('retained',10,'retained-op',true,outsider).ok,false);
  assert.equal(retained.bind('different',10,'retained-op').ok,false);
  assert.equal(retained.bind('retained',10,'retained-op').ok,true);
  const managedItem=item();retained.partition.emit('will-download',{},managedItem,managed);managedItem.emit('done',{},'completed');
  await until(async()=> (await retained.command({action:'drain_downloads',tab_id:'retained'})).downloads.length===2);
  let host = harness(); await host.manager.ready;
  assert.equal(host.bind('A', 7, 'op-A', false).ok, false);
  assert.equal(host.bind('A', 9, 'op-A').ok, false);
  assert.equal(host.bind('A', 7, 'op-A').ok, true);
  assert.equal(host.bind('B', 7, 'op-B').ok, false, 'another tab cannot steal an existing guest binding');
  assert.equal(host.bind('B', 8, 'op-native').ok, true, 'detached owned guests use the same manager');
  const foreign = item(); host.partition.emit('will-download', {}, foreign, guests.get(9)); assert.equal(foreign.path, '');
  const first = item(); host.partition.emit('will-download', {}, first, guests.get(7));
  assert.equal(path.dirname(first.path), path.join(temporary, 'data/browser/download-staging'));
  assert.ok(!path.basename(first.path).includes('untrusted-name'));
  host.bind('A', 7, 'op-later'); first.emit('done', {}, 'completed');
  await until(async () => (await host.command({action: 'drain_downloads', tab_id: 'A'})).downloads.length === 1);
  const completed = (await host.command({action: 'drain_downloads', tab_id: 'A'})).downloads[0];
  assert.equal(completed.operation_id, 'op-A', 'attribution latches at will-download, not at drain time');
  assert.equal(completed.sha256, require('node:crypto').createHash('sha256').update('fixture bytes').digest('hex'));
  assert.equal((await host.command({action: 'list'})).downloads[0].path, undefined, 'public UI records omit local staging paths');
  assert.equal((await host.command({action: 'drain_downloads', tab_id: 'A'}, false)).ok, false);
  assert.equal((await host.command({action: 'drain_downloads', tab_id: 'B'})).downloads.length, 0);
  await host.command({action: 'ack_downloads', tab_id: 'B', download_ids: [completed.download_id]});
  assert.equal(fs.existsSync(first.path), true, 'wrong-tab acknowledgement cannot delete the staged file');
  assert.equal((await host.command({action: 'cancel_download', tab_id: 'A', download_id: completed.download_id})).error, 'download_already_completed');

  // Durable completed metadata survives a new native owner and reused guest ID.
  host.manager.dispose(); host = harness(); await host.manager.ready;
  const restored = (await host.command({action: 'drain_downloads', tab_id: 'A'})).downloads[0];
  assert.equal(restored.operation_id, 'op-A'); assert.equal(restored.sha256, completed.sha256);
  assert.equal(host.bind('B', 7, 'op-reused-guest').ok, true);
  assert.equal((await host.command({action: 'drain_downloads', tab_id: 'B'})).downloads.length, 0);
  await host.command({action: 'ack_downloads', tab_id: 'A', download_ids: [restored.download_id]});
  assert.equal(fs.existsSync(first.path), false);
  assert.equal((await host.command({action: 'ack_downloads', tab_id: 'A', download_ids: [restored.download_id]})).ok, true);
  assert.equal((await host.command({action: 'drain_downloads', tab_id: 'A'})).downloads.length, 0);

  const active = item('partial'); host.partition.emit('will-download', {}, active, guests.get(7));
  const activeRow = (await host.command({action: 'list', tab_id: 'B'})).downloads[0];
  assert.equal((await host.command({action: 'cancel_download', tab_id: 'A', download_id: activeRow.download_id})).ok, false);
  assert.equal(fs.existsSync(active.path), true);
  assert.equal((await host.command({action: 'cancel_download', tab_id: 'B', download_id: activeRow.download_id})).ok, true);
  await until(() => !fs.existsSync(active.path));
  assert.equal((await host.command({action: 'list', tab_id: 'B'})).downloads[0].status, 'cancelled');
  await testAppRootComposition();
  console.log('E11 download IPC: trusted owned guests, safe staging names, tab/operation ownership, retained completed files, restart handoff, idempotent acknowledgement and cancellation passed');
})().catch(error => {console.error(error); process.exitCode = 1;}).finally(() => {
  for (const manager of managers) manager.dispose();
  if (path.dirname(temporary) === path.resolve(os.tmpdir()) && path.basename(temporary).startsWith('variant1-download-ipc-')) fs.rmSync(temporary, {recursive: true, force: true});
});

async function testAppRootComposition() {
  const root = path.resolve(__dirname, '..');
  const bootSource = fs.readFileSync(path.join(root, 'electron-app-boot.js'), 'utf8');
  const python = path.join(root, 'backend/.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  for (const mode of ['development', 'packaged', 'smoke', 'ignored-smoke-override']) {
    const appRoot = path.join(temporary, mode, 'app');
    const userData = path.join(temporary, mode, 'electron-user-data');
    const override = path.join(temporary, mode, 'smoke-root');
    const env = {VARIANT1_E2E_SMOKE: mode === 'smoke' ? '1' : '0', VARIANT1_E2E_DATA_DIR: override};
    const app = {isPackaged: mode === 'packaged', getPath: name => {assert.equal(name, 'userData'); return userData;}};
    const module = {exports: {}};
    // Execute the production boot root selection, suppressing unrelated config
    // seeding/deletion. Download staging itself uses real disposable files.
    const bootFs = {mkdirSync() {}, existsSync: () => true, rmSync() {}};
    vm.runInNewContext(bootSource, {module, process: {env, resourcesPath: path.join(temporary, 'resources')},
      require: id => id === 'electron' ? {app} : id === 'fs' ? bootFs : id === './electron-security' ? {} : require(id)});
    let dataDir;
    module.exports.createInitDataDir({app, appRoot, setDataDir: value => {dataDir = value;},
      setConfigPath() {}, setLogDir() {}, rebindSettingsStore() {}, beginSessionLog() {}, log() {}})();
    assert.equal(dataDir, mode === 'packaged' ? userData : mode === 'smoke' ? override : appRoot);
    const host = harness(dataDir); await host.manager.ready;
    host.bind('composition', 7, `op-${mode}`);
    const staged = item(); host.partition.emit('will-download', {}, staged, guests.get(7));
    staged.emit('done', {}, 'completed');
    await until(async () => (await host.command({action: 'drain_downloads', tab_id: 'composition'})).downloads.length === 1);
    const row = (await host.command({action: 'drain_downloads', tab_id: 'composition'})).downloads[0];
    // Python receives only the Electron app root and real staged path, never a
    // pre-agreed staging root. It executes HostRuntime's composition statements
    // and the real Browser Fabric factory to derive the independent boundary.
    const result = require('node:child_process').execFileSync(python,
      ['-B', path.join(__dirname, 'test-browser-downloads-composition.py'), dataDir, row.path],
      {cwd: root, encoding: 'utf8', timeout: 30000});
    const expected = JSON.parse(result);
    assert.equal(path.dirname(row.path), expected.staging_root, mode);
    assert.equal(row.operation_id, `op-${mode}`);
    await host.command({action: 'ack_downloads', tab_id: 'composition', download_ids: [row.download_id]});
    assert.equal(fs.existsSync(row.path), false);
    host.manager.dispose();
  }
  console.log('Download root composition: production Electron boot + HostRuntime data_root + Browser Fabric factory agree for dev, packaged, smoke, and ignored override; completed drain/ack passed');
}
