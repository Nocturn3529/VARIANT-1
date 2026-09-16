'use strict';

/**
 * Transparent always-on-top Live2D window + drag / mouse-ignore IPC.
 * Main Deck owns every product, session, and configuration surface.
 */

const path = require('path');
const { BrowserWindow, ipcMain, screen } = require('electron');

const DEFAULT_WIN = { width: 860, height: 540 };

/**
 * @param {object} deps
 * @param {string} deps.appRoot
 * @param {() => object} deps.readSettings
 * @param {(s: object) => void} deps.writeSettings
 * @param {(msg: string) => void} deps.log
 * @param {(win: import('electron').BrowserWindow) => void} deps.hardenAppWindow
 * @param {(event: any, win?: import('electron').BrowserWindow|null) => boolean} deps.isTrustedIpcSender
 * @param {() => boolean} deps.isQuitting
 * @param {() => object|null|Promise<object|null>} deps.getBackendInfo
 * @param {(x: number, y: number) => void} [deps.onPositionSaved]
 */
function createOverlayController(deps) {
  const {
    appRoot,
    readSettings,
    writeSettings,
    log,
    hardenAppWindow,
    isTrustedIpcSender,
    isQuitting,
    getBackendInfo,
    onPositionSaved,
  } = deps;

  /** Strip to plain JSON so webContents.send never hits "Failed to serialize". */
  function plainActivityInfo(info) {
    if (!info || typeof info !== 'object') return null;
    // Reject Promises / thenables accidentally passed as info.
    if (typeof info.then === 'function') return null;
    const port = Number(info.port);
    const activityToken = info.activityToken != null
      ? String(info.activityToken)
      : '';
    if (!Number.isFinite(port) || !activityToken) return null;
    return {port, activityToken};
  }

  let mainWindow = null;
  let dragState = null;
  let lastIgnore = null;
  let ipcRegistered = false;

  function getMainWindow() {
    return mainWindow;
  }

  function setMainWindow(win) {
    mainWindow = win;
  }

  function computeInitialBounds() {
    const settings = readSettings();
    const primary = screen.getPrimaryDisplay();
    const wa = primary.workArea;
    const width = DEFAULT_WIN.width;
    const height = DEFAULT_WIN.height;

    const saved = settings.avatar && settings.avatar.position;
    if (saved && Number.isFinite(saved.x) && Number.isFinite(saved.y)) {
      return { x: saved.x, y: saved.y, width, height };
    }

    const margin = 12;
    const x = wa.x + wa.width - width - margin;
    const y = wa.y + wa.height - height - margin;
    return { x, y, width, height };
  }

  function saveWindowPosition() {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    const [x, y] = mainWindow.getPosition();
    const settings = readSettings();
    settings.avatar = settings.avatar || {};
    settings.avatar.position = { x, y };
    try { writeSettings(settings); } catch (_) { return false; } // The settings owner already logged the persistence failure.
    if (typeof onPositionSaved === 'function') onPositionSaved(x, y);
  }

  function createWindow() {
    const bounds = computeInitialBounds();

    mainWindow = new BrowserWindow({
      x: bounds.x,
      y: bounds.y,
      width: bounds.width,
      height: bounds.height,
      transparent: true,
      frame: false,
      alwaysOnTop: true,
      skipTaskbar: true,
      hasShadow: false,
      resizable: false,
      maximizable: false,
      minimizable: false,
      fullscreenable: false,
      focusable: true,
      show: false,
      webPreferences: {
        preload: path.join(appRoot, 'preload.js'),
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
      },
    });
    hardenAppWindow(mainWindow);

    mainWindow.setAlwaysOnTop(true, 'screen-saver');
    mainWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
    mainWindow.setIgnoreMouseEvents(true, { forward: true });
    mainWindow.loadURL('variant1://app/frontend/index.html');

    mainWindow.once('ready-to-show', () => {
      const startHidden = process.argv.includes('--hidden')
        || !!(readSettings().general && readSettings().general.startHidden);
      if (!startHidden) mainWindow.showInactive();
    });

    mainWindow.webContents.on('did-finish-load', async () => {
      // getBackendInfo is async (health re-attach). Must await — sending a
      // Promise over IPC throws "Failed to serialize arguments".
      let info = null;
      try {
        if (typeof getBackendInfo === 'function') info = await getBackendInfo();
      } catch (err) {
        log('backend info for overlay failed: ' + (err && err.message ? err.message : err));
      }
      const plain = plainActivityInfo(info);
      if (!mainWindow || mainWindow.isDestroyed()) return;
      try {
        mainWindow.webContents.send('activity:status', {
          status: plain ? 'ready' : 'starting',
          info: plain,
        });
      } catch (err) {
        log('activity:status send failed: ' + (err && err.message ? err.message : err));
      }
    });

    mainWindow.on('close', (e) => {
      if (!isQuitting()) {
        e.preventDefault();
        mainWindow.hide();
      }
    });

    return mainWindow;
  }

  function registerIpc() {
    if (ipcRegistered) return;
    ipcRegistered = true;
    ipcMain.handle('avatar:settings:get', event => {
      if (!mainWindow || !isTrustedIpcSender(event, mainWindow)) return null;
      const size = Number(readSettings()?.avatar?.size);
      return {avatar: {size: Number.isFinite(size) && size > 0 ? size : 160}};
    });

    ipcMain.on('drag:start', (event, payload) => {
      if (!isTrustedIpcSender(event, mainWindow)) return;
      const { mouseX, mouseY } = payload || {};
      if (!Number.isFinite(mouseX) || !Number.isFinite(mouseY)) return;
      if (!mainWindow) return;
      const [winX, winY] = mainWindow.getPosition();
      dragState = { startMouseX: mouseX, startMouseY: mouseY, startWinX: winX, startWinY: winY };
    });

    ipcMain.on('drag:move', (event, payload) => {
      if (!isTrustedIpcSender(event, mainWindow)) return;
      const { mouseX, mouseY } = payload || {};
      if (!Number.isFinite(mouseX) || !Number.isFinite(mouseY)) return;
      if (!mainWindow || !dragState) return;
      const dx = mouseX - dragState.startMouseX;
      const dy = mouseY - dragState.startMouseY;
      mainWindow.setPosition(dragState.startWinX + dx, dragState.startWinY + dy);
    });

    ipcMain.on('drag:end', (event) => {
      if (!isTrustedIpcSender(event, mainWindow)) return;
      if (!dragState) return;
      dragState = null;
      saveWindowPosition();
    });

    ipcMain.on('mouse:setIgnore', (event, ignore) => {
      if (!isTrustedIpcSender(event, mainWindow)) return;
      if (!mainWindow || mainWindow.isDestroyed()) return;
      if (ignore === lastIgnore) return;
      lastIgnore = ignore;
      mainWindow.setIgnoreMouseEvents(!!ignore, { forward: true });
    });
  }

  return {
    DEFAULT_WIN,
    getMainWindow,
    setMainWindow,
    createWindow,
    registerIpc,
    saveWindowPosition,
    computeInitialBounds,
  };
}

module.exports = { createOverlayController, DEFAULT_WIN };
