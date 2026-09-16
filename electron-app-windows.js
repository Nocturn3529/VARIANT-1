'use strict';

/**
 * Main Deck + Live Logs pop-out BrowserWindow factories for Electron main.
 */

const path = require('path');
const { BrowserWindow } = require('electron');
const DECK_ROUTES = require('./deck-routes.json');

const DECK_VIEWS = new Set(DECK_ROUTES.primary);

/**
 * @param {object} deps
 * @param {string} deps.appRoot
 * @param {(win: import('electron').BrowserWindow) => void} deps.hardenAppWindow
 * @param {(win: import('electron').BrowserWindow) => void} [deps.onDeckCreated]
 * @param {() => void} [deps.onDeckClosed]
 * @param {() => void} [deps.onMonitorClosed]
 */
function createAppWindows(deps) {
  const { appRoot, hardenAppWindow, onDeckCreated, onDeckClosed, onMonitorClosed } = deps;
  let deckWindow = null;
  let monitorWindow = null;
  let pendingNavigation = null;
  function clearPendingNavigation(win) {
    if (!pendingNavigation || (win && pendingNavigation.win !== win)) return;
    pendingNavigation.win.webContents.removeListener('did-finish-load', pendingNavigation.flush);
    pendingNavigation = null;
  }

  function getDeckWindow() { return deckWindow; }
  function getMonitorWindow() { return monitorWindow; }

  function openDeckWindow(initialView = 'chat') {
    const targetView = DECK_VIEWS.has(initialView) ? initialView : 'chat';

    if (deckWindow && !deckWindow.isDestroyed()) {
      deckWindow.show();
      deckWindow.focus();
      const current = deckWindow;
      if (current.webContents.isLoading()) {
        if (pendingNavigation?.win === current) pendingNavigation.view = targetView;
        else {
          clearPendingNavigation();
          const pending = {win: current, view: targetView, flush: () => {
            if (pendingNavigation !== pending) return;
            pendingNavigation = null;
            if (!current.isDestroyed()) current.webContents.send('deck:navigate', pending.view);
          }};
          pendingNavigation = pending;
          current.webContents.once('did-finish-load', pending.flush);
        }
      } else {
        clearPendingNavigation();
        current.webContents.send('deck:navigate', targetView);
      }
      return deckWindow;
    }
    deckWindow = new BrowserWindow({
      width: 1200,
      height: 760,
      minWidth: 760,
      minHeight: 520,
      title: 'VARIANT-1',
      backgroundColor: '#101112',
      frame: false,
      autoHideMenuBar: true,
      show: false,
      webPreferences: {
        preload: path.join(appRoot, 'deck-preload.js'),
        contextIsolation: true,
        webviewTag: true,
        nodeIntegration: false,
        sandbox: true,
        backgroundThrottling: false,
      },
    });
    const created = deckWindow;
    hardenAppWindow(deckWindow);
    if (typeof onDeckCreated === 'function') onDeckCreated(deckWindow);
    deckWindow.setMenuBarVisibility(false);
    deckWindow.maximize();
    deckWindow.loadURL(
      `variant1://app/frontend/main-deck/index.html?view=${encodeURIComponent(targetView)}`
    );
    deckWindow.once('ready-to-show', () => { if (!created.isDestroyed()) created.show(); });
    deckWindow.on('closed', () => {
      clearPendingNavigation(created);
      if (deckWindow !== created) return;
      deckWindow = null;
      if (typeof onDeckClosed === 'function') onDeckClosed();
    });
    return deckWindow;
  }

  function openMonitorWindow() {
    if (monitorWindow && !monitorWindow.isDestroyed()) {
      if (monitorWindow.isMinimized()) monitorWindow.restore();
      monitorWindow.show();
      monitorWindow.focus();
      return monitorWindow;
    }
    monitorWindow = new BrowserWindow({
      width: 920,
      height: 480,
      minWidth: 480,
      minHeight: 280,
      title: 'VARIANT-1 — Logs',
      backgroundColor: '#03070a',
      frame: false,
      transparent: false,
      alwaysOnTop: true,
      minimizable: true,
      maximizable: true,
      movable: true,
      resizable: true,
      fullscreenable: false,
      skipTaskbar: false,
      autoHideMenuBar: true,
      show: false,
      webPreferences: {
        preload: path.join(appRoot, 'monitor-preload.js'),
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
      },
    });
    hardenAppWindow(monitorWindow);
    monitorWindow.setMenuBarVisibility(false);
    monitorWindow.setAlwaysOnTop(true);
    monitorWindow.loadURL('variant1://app/frontend/monitor.html');
    monitorWindow.once('ready-to-show', () => monitorWindow.show());
    monitorWindow.on('closed', () => {
      monitorWindow = null;
      if (typeof onMonitorClosed === 'function') onMonitorClosed();
    });
    return monitorWindow;
  }

  return {
    openDeckWindow,
    openMonitorWindow,
    getDeckWindow,
    getMonitorWindow,
  };
}

module.exports = { createAppWindows, DECK_VIEWS };
