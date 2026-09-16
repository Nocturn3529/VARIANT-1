'use strict';
const {app, ipcMain, session, webContents} = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const MAX_BYTES = 256 * 1024 * 1024;
const LIVE = new Set(['in_progress', 'finalizing']);
const ID = /^download-[0-9a-f-]{36}$/;

/** Owned staging files are handed to Browser Fabric; no automatic OS dialog. */
function registerBrowserDownloads({getDataDir, getDeckWindow, isTrustedIpcSender, isNativeHost, getRetainedGuest, log = () => {}}) {
  const records = new Map(), bindings = new Map(), items = new Map();
  const instance = crypto.randomUUID();
  let directory, loaded = false, disposed = false, notifyTimer;
  function staging() {
    // DATA_DIR is the app/userData root; HostRuntime gives Browser Fabric its
    // `data` child. Keep native staging inside that same validated boundary.
    if (!directory) directory = path.resolve(getDataDir(), 'data', 'browser', 'download-staging');
    fs.mkdirSync(directory, {recursive:true});
    return directory;
  }
  const fileFor = id => path.join(staging(), `${id}.download`);
  function load() {
    if (loaded) return;
    loaded = true;
    try {
      const rows = JSON.parse(fs.readFileSync(path.join(staging(), 'index.json'), 'utf8'));
      if (!Array.isArray(rows)) return;
      for (const row of rows) {
        if (!row || !ID.test(String(row.download_id)) || typeof row.tab_id !== 'string'
          || row.path !== fileFor(row.download_id)) continue;
        if (LIVE.has(row.status)) { row.status = 'interrupted'; row.error = 'App restarted before the download finished.'; }
        records.set(row.download_id, row);
      }
    } catch (error) { if (error.code !== 'ENOENT') log('[browser-download] metadata: ' + error.message); }
  }
  function persist() {
    try {
      const target = path.join(staging(), 'index.json'), temporary = target + '.new';
      fs.writeFileSync(temporary, JSON.stringify([...records.values()]));fs.renameSync(temporary, target);
    } catch (error) { log('[browser-download] could not retain metadata: ' + error.message); }
  }
  function publish() {
    if (disposed || notifyTimer) return;
    notifyTimer = setTimeout(() => {
      notifyTimer = undefined;
      const deck = getDeckWindow();
      if (deck && !deck.isDestroyed()) {
        try { deck.webContents.send('workbench:browser:downloads', [...records.values()].map(publicRow)); }
        catch (error) { log('[browser-download] notification: '+error.message); }
      }
    }, 80);
  }
  function publicRow(row) {
    const {path: _path, ...value} = row;
    return {...value, dialog_pending:false};
  }
  function ownedGuest(id) {
    const retained = getRetainedGuest?.(id);
    if (retained && !retained.contents.isDestroyed()) return retained.contents;
    const guest = webContents.fromId(id), owner = guest?.hostWebContents;
    const deck = getDeckWindow();
    return guest && !guest.isDestroyed() && guest.getType() === 'webview' && owner
      && (owner === deck?.webContents || isNativeHost(owner)) ? guest : null;
  }
  async function removeFile(row) {
    // The record can never supply a deletion path: derive it from a valid ID.
    if (!ID.test(row.download_id) || row.path !== fileFor(row.download_id)) throw new Error('invalid_download_path');
    try { await fs.promises.unlink(fileFor(row.download_id)); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
  async function digest(file) {
    const hash = crypto.createHash('sha256');
    for await (const chunk of fs.createReadStream(file)) hash.update(chunk);
    return hash.digest('hex');
  }
  function download(_event, item, guest) {
    if (!guest || !ownedGuest(guest.id)) return;
    load();
    const id = `download-${crypto.randomUUID()}`;
    const row = {download_id:id, tab_id:bindings.get(guest.id)?.tab_id || getRetainedGuest?.(guest.id)?.tabId || '', operation_id:bindings.get(guest.id)?.operation_id || '', guest_id:guest.id, host_instance:instance,
      status:'in_progress', path:fileFor(id), suggested_filename:item.getFilename() || 'download',
      url:item.getURL(), url_chain:item.getURLChain?.() || [item.getURL()], bytes:0,
      total_bytes:item.getTotalBytes() || 0, sha256:'', started_at:Date.now(), error:''};
    records.set(id, row);items.set(id, item);
    try { item.setSavePath(row.path); }
    catch (error) { row.status='error';row.error='Could not stage the download.';item.cancel();log('[browser-download] '+error.message); }
    persist();publish();
    item.on('updated', (_event, state) => {
      if (!LIVE.has(row.status)) return;
      row.bytes = item.getReceivedBytes();row.total_bytes = item.getTotalBytes();
      if (row.bytes > MAX_BYTES || row.total_bytes > MAX_BYTES) {
        row.status='error';row.error='Download exceeds 256 MiB.';item.cancel();persist();
      } else if (state === 'interrupted') { row.status='interrupted';row.error='The download was interrupted.';persist(); }
      publish();
    });
    item.once('done', async (_event, state) => {
      items.delete(id);
      if (state === 'completed' && row.status !== 'cancelled' && row.status !== 'error') {
        row.status='finalizing';publish();
        try {
          const stat = await fs.promises.stat(row.path);
          if (stat.size > MAX_BYTES) throw new Error('Download exceeds 256 MiB.');
          const sha256 = await digest(row.path);
          if (row.status !== 'cancelled') { row.bytes=stat.size;row.sha256=sha256;row.status='completed'; }
        } catch (error) { if (row.status !== 'cancelled') { row.status='error';row.error=error.message; } }
      } else if (row.status !== 'error') row.status = state === 'cancelled' ? 'cancelled' : 'interrupted';
      if (row.status !== 'completed') await removeFile(row).catch(error => log('[browser-download] cleanup: '+error.message));
      persist();publish();
    });
  }
  ipcMain.handle('workbench:browser:bind', (event, tabId, guestId, operationId) => {
    const retained = getRetainedGuest?.(guestId);
    if (retained && (retained.owner !== event.sender || retained.tabId !== tabId)) return {ok:false};
    if (!isTrustedIpcSender(event, getDeckWindow()) || typeof tabId !== 'string' || !tabId || tabId.length > 300
      || !Number.isSafeInteger(guestId) || !ownedGuest(guestId)
      || (operationId !== undefined && (typeof operationId !== 'string' || operationId.length > 512))) return {ok:false};
    const prior = bindings.get(guestId);
    if (prior && prior.tab_id !== tabId) return {ok:false};
    bindings.set(guestId, {tab_id:tabId, operation_id:operationId === undefined ? prior?.operation_id || '' : operationId});load();
    for (const row of records.values()) if (row.host_instance === instance && row.guest_id === guestId && !row.tab_id) row.tab_id=tabId;
    persist();publish();return {ok:true};
  });
  ipcMain.handle('workbench:browser:downloads', async (event, command = {}) => {
    if (!isTrustedIpcSender(event, getDeckWindow())) return {ok:false,error:'untrusted_download_request'};
    load();
    const action = String(command.action || 'list'), tabId = String(command.tab_id || '');
    const matching = () => [...records.values()].filter(row => !tabId || row.tab_id === tabId);
    if (action === 'list') return {ok:true,downloads:matching().map(publicRow),dialog_pending:false};
    if (!tabId) return {ok:false,error:'download_tab_required'};
    if (action === 'drain_downloads') return {ok:true,downloads:matching().filter(row => row.status === 'completed').map(row => ({...row})),dialog_pending:false};
    if (action === 'ack_downloads') {
      if (!Array.isArray(command.download_ids)) return {ok:false,error:'download_ids_required'};
      const acknowledged = [];
      for (const id of command.download_ids) {
        const row=records.get(id);
        if (!row || row.tab_id !== tabId || row.status !== 'completed') continue;
        await removeFile(row);row.status='stored';acknowledged.push(id);
      }
      persist();publish();return {ok:true,download_ids:acknowledged};
    }
    if (action === 'cancel_download') {
      const row = records.get(String(command.download_id || ''));
      if (!row || row.tab_id !== tabId) return {ok:false,error:'download_not_found'};
      if (row.status === 'stored' || row.status === 'completed') return {ok:false,error:'download_already_completed'};
      const prior = row.status, item = items.get(row.download_id);
      row.status='cancelled';item?.cancel();
      if (!item && prior !== 'finalizing') await removeFile(row);
      persist();publish();return {ok:true,download:publicRow(row)};
    }
    return {ok:false,error:'unknown_download_action'};
  });
  const ready = app.whenReady().then(() => { if (!disposed) session.fromPartition('persist:variant1-preview').on('will-download', download); });
  return {ready, dispose() {
    disposed=true;clearTimeout(notifyTimer);
    if (app.isReady()) session.fromPartition('persist:variant1-preview').removeListener('will-download', download);
    for (const item of items.values()) item.cancel();
  }};
}
module.exports = {registerBrowserDownloads, MAX_BYTES};
