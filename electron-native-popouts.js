'use strict';

const path = require('node:path');
const {ipcMain, screen, shell} = require('electron');
const {isAllowedExternalUrl, isTrustedAppUrl} = require('./electron-security');

const POPOUT_URL = 'variant1://app/frontend/main-deck/popout.html';
const FRAME_PREFIX = 'variant1-panel:';

function popoutId(url, frameName) {
  try {
    const parsed = new URL(url);
    const id = parsed.searchParams.get('surface') || '';
    if (parsed.origin !== new URL(POPOUT_URL).origin || parsed.protocol !== 'variant1:'
      || parsed.host !== 'app' || parsed.pathname !== '/frontend/main-deck/popout.html'
      || parsed.username || parsed.password || parsed.hash || parsed.searchParams.getAll('surface').length !== 1
      || [...parsed.searchParams.keys()].some(key => key !== 'surface')) return null;
    return /^(pane:[\w.-]+|utility:(runtime|overview|automations))$/.test(id)
      && id.length <= 160 && frameName === FRAME_PREFIX + id ? id : null;
  } catch { return null; }
}

/** Only the Deck can open an inert, same-origin rendering host. No second runtime. */
function createNativePopoutManager({appRoot, getDeckWindow, isTrustedIpcSender, hardenAppWindow, log}) {
  const windows = new Map();
  const windowOwners = new Map();
  const prepared = new Map();
  function notifyClosed(id) {
    const owner = getDeckWindow();
    if (owner && !owner.isDestroyed()) {
      try { owner.webContents.send('workbench:window:closed', id); } catch { /* owner is reloading or has crashed */ }
    }
  }
  function control(id, action, size) {
    if (id === 'deck' && action === 'focus') {
      const deck = getDeckWindow();
      if (!deck || deck.isDestroyed()) return {ok: false};
      if (deck.isMinimized()) deck.restore();
      deck.show(); deck.focus(); return {ok: true};
    }
    const win = windows.get(id);
    if (!win || win.isDestroyed()) return {ok: false};
    if (action === 'focus') { if (win.isMinimized()) win.restore(); win.show(); win.focus(); }
    else if (action === 'minimize') win.minimize();
    else if (action === 'maximize') win.isMaximized() ? win.unmaximize() : win.maximize();
    else if (action === 'pin') win.setAlwaysOnTop(!win.isAlwaysOnTop());
    else if (action === 'close') win.close();
    else if (action === 'bounds') return {ok: true, bounds: win.getBounds(), pinned: win.isAlwaysOnTop()};
    else if (action === 'resize') {
      if (!size || !Number.isInteger(size.width) || !Number.isInteger(size.height)
        || size.width < 300 || size.width > 4096 || size.height < 240 || size.height > 2400) return {ok: false};
      const prior = win.getBounds(), area = screen.getDisplayMatching(prior).workArea;
      const width = Math.min(size.width, area.width), height = Math.min(size.height, area.height);
      if (win.isMaximized()) win.unmaximize();
      win.setBounds({x: Math.max(area.x, Math.min(prior.x, area.x + area.width - width)),
        y: Math.max(area.y, Math.min(prior.y, area.y + area.height - height)), width, height});
      return {ok: true, bounds: win.getBounds(), pinned: win.isAlwaysOnTop()};
    }
    else if (action === 'ready') { win.show(); win.focus(); }
    else return {ok: false};
    return {ok: true, pinned: !win.isDestroyed() && win.isAlwaysOnTop()};
  }
  ipcMain.handle('workbench:window:control', (event, id, action, size) => {
    if (!isTrustedIpcSender(event, getDeckWindow())) return {ok: false};
    return control(String(id), String(action), size);
  });
  function attach(owner) {
    owner.webContents.setWindowOpenHandler(({url, frameName, features}) => {
      const id = popoutId(url, frameName);
      if (!id || !isTrustedAppUrl(owner.webContents.getURL())) {
        if (isAllowedExternalUrl(url)) shell.openExternal(url).catch(error => log('[main] external window failed: ' + error));
        return {action: 'deny'};
      }
      if (windows.has(id)) { control(id, 'focus'); return {action: 'deny'}; }
      const options = Object.fromEntries(String(features || '').split(',').map(part => part.split('=')));
      const ownerBounds = owner.getBounds();
      const requested = {x: Number(options.left), y: Number(options.top), width: Number(options.width), height: Number(options.height)};
      const placement = Object.values(requested).every(Number.isFinite) && requested.width > 0 && requested.height > 0 ? requested : ownerBounds;
      // Match the saved display when present; Electron chooses the nearest
      // remaining display after a monitor disconnect, then we clamp into it.
      const workArea = screen.getDisplayMatching(placement).workArea;
      const dimension = (key, fallback, min, max) => {
        const value = Number(options[key]);
        return Math.round(Math.max(min, Math.min(max, Number.isFinite(value) ? value : fallback)));
      };
      const width = dimension('width', 640, 300, Math.max(300, workArea.width));
      const height = dimension('height', 600, 240, Math.max(240, workArea.height));
      const x = dimension('left', owner.getBounds().x + 80, workArea.x, workArea.x + workArea.width - width);
      const y = dimension('top', owner.getBounds().y + 80, workArea.y, workArea.y + workArea.height - height);
      prepared.set(frameName, {id, bounds: {x, y, width, height}});
      return {action: 'allow', outlivesOpener: false, overrideBrowserWindowOptions: {
        width, height, x, y, useContentSize: false, minWidth: 300, minHeight: 240,
        frame: false, modal: false, resizable: true, movable: true, minimizable: true,
        maximizable: true, fullscreenable: false, skipTaskbar: false, alwaysOnTop: options.pin === 'true',
        backgroundColor: '#0e0e0e', autoHideMenuBar: true, show: false,
        webPreferences: {
          preload: path.join(appRoot, 'popout-preload.js'), contextIsolation: true,
          nodeIntegration: false, sandbox: true, webSecurity: true, webviewTag: true,
          backgroundThrottling: false,
        },
      }};
    });
    owner.webContents.on('did-create-window', (child, details) => {
      const request = prepared.get(details.frameName);
      const id = request?.id;
      if (!id || popoutId(details.url, details.frameName) !== id) { child.close(); return; }
      prepared.delete(details.frameName);
      windows.set(id, child);
      windowOwners.set(id, owner.webContents);
      // Chromium window.open may apply content-size/frame adjustments after
      // creation. Set the final native bounds explicitly so presets round-trip.
      child.setBounds(request.bounds);
      // A rendering host cannot navigate to another app document or a remote page.
      hardenAppWindow(child);
      child.webContents.on('will-navigate', (event, url) => { if (url !== details.url) event.preventDefault(); });
      child.webContents.on('will-redirect', (event, url) => { if (url !== details.url) event.preventDefault(); });
      child.webContents.on('render-process-gone', () => { if (!child.isDestroyed()) child.destroy(); });
      child.once('closed', () => { windows.delete(id); windowOwners.delete(id); notifyClosed(id); });
      child.setMenuBarVisibility(false);
    });
    const closeChildren = () => {
      for (const child of windows.values()) if (!child.isDestroyed()) child.close();
      windows.clear(); windowOwners.clear(); prepared.clear();
    };
    owner.webContents.on('render-process-gone', closeChildren);
    owner.webContents.on('did-navigate', closeChildren);
    owner.webContents.on('did-start-navigation', (_event, _url, inPlace, mainFrame) => {
      if (mainFrame && !inPlace) closeChildren();
    });
    owner.once('closed', closeChildren);
  }
  return {attach, getWindow:(id,owner)=>windowOwners.get(id) === owner ? windows.get(id) : null,
    isHost: contents => [...windows.values()].some(win => !win.isDestroyed() && win.webContents === contents)};
}

module.exports = {createNativePopoutManager, popoutId, POPOUT_URL, FRAME_PREFIX};
