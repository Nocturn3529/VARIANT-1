'use strict';

/**
 * VARIANT-1 — Electron main process composition root.
 *
 * Bodies live in electron-* modules; this file only wires deps and starts the app.
 */

const { app, ipcMain, protocol, session, shell } = require('electron');
const path = require('path');
const { createBackendManager, registerBackendIpc } = require('./electron-backend');
const { createLogger } = require('./electron-logging');
const { createSettingsStore } = require('./electron-settings-store');
const { createTray } = require('./electron-tray');
const { createAppWindows } = require('./electron-app-windows');
const { registerDeckIpc } = require('./electron-deck-ipc');
const { registerBrowserCapture } = require('./electron-browser-capture');
const { registerBrowserDownloads } = require('./electron-browser-downloads');
const { createBrowserViewManager } = require('./electron-browser-views');
const { createNativePopoutManager } = require('./electron-native-popouts');
const { createUpdateService } = require('./electron-updates');
const {
  applyGpuFlags,
  registerVariant1Scheme,
  createWindowSecurity,
  createInitDataDir,
  registerAppLifecycle,
} = require('./electron-app-boot');

const APP_ROOT = __dirname;

// --- Pre-ready (must run before app.whenReady) ------------------------------
// Pin runtime naming before Electron derives the per-product userData path.
// This is a clean product identity; no retired-product data root is reused.
app.setName('VARIANT-1');
applyGpuFlags(app);
registerVariant1Scheme(protocol);

// --- Paths / logging / settings --------------------------------------------
let DATA_DIR = APP_ROOT;
let CONFIG_PATH = path.join(APP_ROOT, 'config', 'settings.json');
let LOG_DIR = path.join(APP_ROOT, 'logs');
let PORT_FILE = null;

const {
  logToFile,
  beginSessionLog,
  logBackendStream,
  flushLogStreams,
  getLogBuffer,
  clearLogBuffer,
  subscribeLog,
} = createLogger(() => LOG_DIR);

// Push every new log line into the Live Logs pop-out when it is open.
subscribeLog((line) => {
  try {
    if (monitorWindow && !monitorWindow.isDestroyed()) {
      monitorWindow.webContents.send('logs:line', line);
    }
  } catch (_) { /* never break logging */ }
});

let readSettings = null;
let writeSettings = null;
function rebindSettingsStore() {
  const store = createSettingsStore({
    configPath: () => CONFIG_PATH,
    log: logToFile,
  });
  readSettings = store.readSettings;
  writeSettings = store.writeSettings;
}
rebindSettingsStore();

// Updates: detected from GitHub releases, installed only on the user's request.
// Development runs and builds without a release feed stay off (no network calls).
const updates = createUpdateService({
  app,
  shell,
  appRoot: APP_ROOT,
  log: logToFile,
  broadcast: (state) => {
    if (deckWindow && !deckWindow.isDestroyed()) deckWindow.webContents.send('update:state', state);
  },
  prepareInstall: async () => {
    // Chromium persists the browser tab list and cookies lazily.
    await Promise.allSettled([session.defaultSession, session.fromPartition('persist:variant1-preview')]
      .flatMap((store) => [store.flushStorageData(), store.cookies.flushStore()]));
    await backend.stopBackend();
  },
});

// --- Window refs -----------------------------------------------------------
let deckWindow = null;
let monitorWindow = null;
let tray = null;
let isQuitting = false;
let appWindows = null;

function webContentsSource(webContents) {
  if (!webContents) return 'renderer';
  if (deckWindow && !deckWindow.isDestroyed() && deckWindow.webContents === webContents) {
    return 'renderer.deck';
  }
  if (monitorWindow && !monitorWindow.isDestroyed() && monitorWindow.webContents === webContents) {
    return 'renderer.monitor';
  }
  return 'renderer';
}

function recoverRenderer(webContents, reason) {
  if (isQuitting || !webContents) return;
  let label = '';
  let getWindow = null;
  let recreate = null;
  if (deckWindow && deckWindow.webContents === webContents) {
    label = 'deck';
    getWindow = () => deckWindow;
    recreate = () => openDeckWindow('chat');
  } else if (monitorWindow && monitorWindow.webContents === webContents) {
    label = 'monitor';
    getWindow = () => monitorWindow;
    recreate = () => openMonitorWindow();
  }
  if (!getWindow || !recreate) return;
  setTimeout(() => {
    if (isQuitting) return;
    const win = getWindow();
    if (win && !win.isDestroyed()) {
      try {
        win.webContents.reload();
        logToFile(`[renderer.${label}] reload requested after ${reason}`);
        return;
      } catch (error) {
        logToFile(
          `[renderer.${label}] reload failed after ${reason}: `
          + String((error && error.message) || error || 'unknown'),
        );
        try { win.destroy(); } catch (_) {}
      }
    }
    try {
      recreate();
      logToFile(`[renderer.${label}] window recreated after ${reason}`);
    } catch (error) {
      logToFile(
        `[renderer.${label}] recreation failed after ${reason}: `
        + String((error && error.message) || error || 'unknown'),
      );
    }
  }, 250);
}

// Process failures are operational evidence. Normal renderer shutdown during
// app quit is omitted, while crashes and abnormal utility/GPU exits stay in
// main0 and main1 with an explicit source.
process.on('uncaughtExceptionMonitor', (error, origin) => {
  const name = error && error.name ? error.name : 'Error';
  const message = error && error.message ? error.message : String(error || 'unknown');
  logToFile(`[main] uncaught exception origin=${origin || 'unknown'} type=${name} message=${message}`);
});

app.on('render-process-gone', (_event, webContents, details) => {
  const reason = String((details && details.reason) || 'unknown');
  if (isQuitting && (reason === 'clean-exit' || reason === 'killed')) return;
  logToFile(
    `[${webContentsSource(webContents)}] process gone reason=${reason}`
    + ` exit_code=${Number((details && details.exitCode) || 0)}`,
  );
  recoverRenderer(webContents, reason);
});

app.on('child-process-gone', (_event, details) => {
  const reason = String((details && details.reason) || 'unknown');
  if (isQuitting && (reason === 'clean-exit' || reason === 'killed')) return;
  const type = String((details && details.type) || 'utility');
  const name = String((details && details.name) || '');
  const service = String((details && details.serviceName) || '');
  logToFile(
    `[electron.${type}] process gone reason=${reason}`
    + ` exit_code=${Number((details && details.exitCode) || 0)}`
    + (name ? ` name=${name}` : '')
    + (service ? ` service=${service}` : ''),
  );
});

// --- Security + data dir ---------------------------------------------------
let chatWindows = null;
const security = createWindowSecurity({
  getChatWindows: () => chatWindows?.list() || [],
  getDeckWindow: () => deckWindow,
  getMonitorWindow: () => monitorWindow,
  log: logToFile,
});
const { isTrustedIpcSender, hardenAppWindow, configurePermissionHandlers, registerGuestWebviewPolicy } = security;
const nativePopouts = createNativePopoutManager({appRoot: APP_ROOT, getDeckWindow: () => deckWindow,
  isTrustedIpcSender, hardenAppWindow, log: logToFile});
const browserViews = createBrowserViewManager({getDeckWindow: () => deckWindow,
  getNativeWindow:nativePopouts.getWindow, isTrustedIpcSender, hardenGuestContents:security.hardenGuestContents, log:logToFile});
registerBrowserCapture({getDeckWindow: () => deckWindow, isTrustedIpcSender, isNativeHost: nativePopouts.isHost,
  getRetainedGuest:browserViews.getGuest, log: logToFile});
const browserDownloads = registerBrowserDownloads({getDataDir: () => DATA_DIR, getDeckWindow: () => deckWindow,
  isTrustedIpcSender, isNativeHost: nativePopouts.isHost, getRetainedGuest:browserViews.getGuest, log: logToFile});
app.once('will-quit', () => { browserViews.dispose(); browserDownloads.dispose(); });

const initDataDir = createInitDataDir({
  app,
  appRoot: APP_ROOT,
  setDataDir: (d) => { DATA_DIR = d; },
  setConfigPath: (p) => { CONFIG_PATH = p; },
  setLogDir: (d) => { LOG_DIR = d; },
  rebindSettingsStore,
  beginSessionLog,
  log: logToFile,
});

// --- Backend ---------------------------------------------------------------
const backend = createBackendManager({
  app,
  appRoot: APP_ROOT,
  getDataDir: () => DATA_DIR,
  log: logToFile,
  logStream: logBackendStream,
  flushLogStreams,
  getStatusWindows: () => [deckWindow, ...(chatWindows?.list() || [])],
});

registerBackendIpc({
  ipcMain,
  isTrustedIpcSender: (event) => !!deckWindow && isTrustedIpcSender(event, deckWindow),
  getInfo: () => backend.getInfo(),
});

chatWindows = require('./electron-chat-windows').createChatWindows({appRoot:APP_ROOT,getDeckWindow:()=>deckWindow,
  getBackendInfo:()=>backend.getInfo(),isTrustedIpcSender,hardenAppWindow});
app.on('before-quit',()=>chatWindows.closeAll());

// --- Deck / Monitor windows ------------------------------------------------
function ensureAppWindows() {
  if (appWindows) return appWindows;
  appWindows = createAppWindows({
    appRoot: APP_ROOT,
    hardenAppWindow,
    onDeckCreated: win => nativePopouts.attach(win),
    onDeckClosed: () => {
      deckWindow = null;
      if (process.env.VARIANT1_E2E_SMOKE === '1') {
        isQuitting = true;
        app.quit();
      }
    },
    onMonitorClosed: () => {
      monitorWindow = null;
    },
  });
  return appWindows;
}

function openDeckWindow(initialView = 'chat') {
  ensureAppWindows().openDeckWindow(initialView);
  deckWindow = ensureAppWindows().getDeckWindow();
  return deckWindow;
}

function openMonitorWindow() {
  ensureAppWindows().openMonitorWindow();
  monitorWindow = ensureAppWindows().getMonitorWindow();
  return monitorWindow;
}

// --- Tray ------------------------------------------------------------------
function installTray() {
  tray = createTray({
    appRoot: APP_ROOT,
    openDeckWindow: (view) => openDeckWindow(view || 'chat'),
    openMonitor: () => openMonitorWindow(),
    quitApp: () => {
      isQuitting = true;
      app.quit();
    },
  });
}

// --- Settings / Deck / Monitor IPC -----------------------------------------
registerDeckIpc({
  app,
  appRoot: APP_ROOT,
  getDataDir: () => DATA_DIR,
  getLogDir: () => LOG_DIR,
  getPortFile: () => PORT_FILE,
  getDeckWindow: () => deckWindow,
  getMonitorWindow: () => monitorWindow,
  openMonitorWindow,
  isTrustedIpcSender,
  readSettings: () => readSettings(),
  writeSettings: (s) => writeSettings(s),
  log: logToFile,
  getLogBuffer,
  clearLogBuffer,
  updates,
});

// --- Lifecycle (protocol ready path, tray, backend, hotkey, quit) ----------
registerAppLifecycle({
  app,
  protocol,
  appRoot: APP_ROOT,
  initDataDir,
  setPortFile: (p) => {
    PORT_FILE = p;
    backend.setPortFile(p);
  },
  configurePermissionHandlers,
  registerGuestWebviewPolicy,
  readSettings: () => readSettings(),
  openDeckWindow: (view) => openDeckWindow(view || 'chat'),
  installTray,
  startBackend: () => backend.startBackend(),
  stopBackend: () => backend.stopBackend(),
  startUpdates: () => updates.start(),
  getDeckWindow: () => deckWindow,
  setQuitting: (v) => { isQuitting = !!v; },
  log: logToFile,
  getUserDataBackendJsonPath: () => path.join(app.getPath('userData'), 'backend.json'),
});
