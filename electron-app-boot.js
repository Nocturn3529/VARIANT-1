'use strict';

/**
 * App boot helpers for VARIANT-1's Electron main process:
 * GPU flags, variant1:// protocol, data-dir seed, window hardening / permissions,
 * and the whenReady lifecycle (Deck, tray, backend, updater, hotkey).
 */

const path = require('path');
const fs = require('fs');
const { app, BrowserWindow, globalShortcut, session, shell } = require('electron');
const {
  isAllowedExternalUrl,
  isAllowedGuestUrl,
  isTrustedAppUrl,
} = require('./electron-security');

/** Call before app is ready. */
function applyGpuFlags(app) {
  const gpu = (process.env.VARIANT1_GPU || '').toLowerCase();
  if (gpu === '1' || gpu === 'on' || gpu === 'true') {
    // leave hardware acceleration on
  } else if (gpu.startsWith('angle-')) {
    app.commandLine.appendSwitch('use-angle', gpu.slice('angle-'.length));
  } else {
    app.disableHardwareAcceleration();
  }
}

/** Call before app is ready. */
function registerVariant1Scheme(protocol) {
  protocol.registerSchemesAsPrivileged([
    {
      scheme: 'variant1',
      privileges: {
        standard: true,
        secure: true,
        supportFetchAPI: true,
        stream: true,
      },
    },
  ]);
}

function registerVariant1Protocol(protocol, appRoot) {
  protocol.registerFileProtocol('variant1', (request, callback) => {
    let parsed;
    try {
      parsed = new URL(request.url);
    } catch (_) {
      callback({ error: -6 });
      return;
    }
    if (
      parsed.protocol !== 'variant1:'
      || parsed.hostname !== 'app'
      || parsed.username
      || parsed.password
    ) {
      callback({ error: -6 });
      return;
    }
    let relative;
    try {
      relative = decodeURIComponent(parsed.pathname.replace(/^\/+/, '')).replace(/\\/g, '/');
    } catch (_) {
      callback({ error: -6 });
      return;
    }
    const first = relative.split('/')[0];
    if (
      !relative
      || relative.includes('\0')
      || relative.includes('..')
      || path.isAbsolute(relative)
      || (first !== 'frontend' && first !== 'assets')
    ) {
      callback({ error: -6 });
      return;
    }
    const resolved = path.normalize(path.join(appRoot, relative));
    const rootPrefix = appRoot.endsWith(path.sep) ? appRoot : appRoot + path.sep;
    if (resolved !== appRoot && !resolved.startsWith(rootPrefix)) {
      callback({ error: -6 });
      return;
    }
    callback({ path: resolved });
  });
}

function isAudioOnlyMediaDetails(details) {
  if (!details || typeof details !== 'object') return false;
  // Permission checks expose one `mediaType`; permission requests expose the
  // requested `mediaTypes` array. Treat missing/unknown metadata as unsafe.
  if (typeof details.mediaType === 'string') {
    return details.mediaType === 'audio';
  }
  if (!Array.isArray(details.mediaTypes) || details.mediaTypes.length === 0) {
    return false;
  }
  return details.mediaTypes.every((mediaType) => mediaType === 'audio');
}

/**
 * @param {object} deps
 * @param {() => import('electron').BrowserWindow|null} deps.getDeckWindow
 * @param {() => import('electron').BrowserWindow|null} deps.getMonitorWindow
 * @param {(msg: string) => void} deps.log
 */
function createWindowSecurity(deps) {
  const { getDeckWindow, getMonitorWindow, log } = deps;

  function isTrustedIpcSender(event, expectedWindow = null) {
    if (!event || !event.sender || !event.senderFrame) return false;
    const win = BrowserWindow.fromWebContents(event.sender);
    if (!win || win.isDestroyed()) return false;
    const deckWindow = getDeckWindow();
    const monitorWindow = getMonitorWindow();
    if (![deckWindow, monitorWindow].includes(win)) return false;
    if (expectedWindow && win !== expectedWindow) return false;
    return isTrustedAppUrl(event.senderFrame.url);
  }

  function hardenAppWindow(win) {
    win.webContents.on('will-navigate', (event, targetUrl) => {
      if (isTrustedAppUrl(targetUrl)) return;
      event.preventDefault();
      if (isAllowedExternalUrl(targetUrl)) {
        shell.openExternal(targetUrl).catch(
          err => log('[main] external navigation failed: ' + err)
        );
      }
    });
    win.webContents.setWindowOpenHandler(({ url }) => {
      if (isAllowedExternalUrl(url)) {
        shell.openExternal(url).catch(
          err => log('[main] external window failed: ' + err)
        );
      }
      return { action: 'deny' };
    });
  }

  function hardenGuestContents(contents) {
    if (!contents || contents.isDestroyed?.()) return;
    contents.setWindowOpenHandler(({ url }) => {
      if (isAllowedExternalUrl(url)) {
        shell.openExternal(url).catch(
          err => log('[main] guest external window failed: ' + err)
        );
      }
      return { action: 'deny' };
    });
    const block = (event, url) => {
      if (isAllowedGuestUrl(url)) return;
      event.preventDefault();
    };
    contents.on('will-navigate', block);
    contents.on('will-redirect', block);
  }

  function registerGuestWebviewPolicy() {
    app.on('web-contents-created', (_event, contents) => {
      contents.on('will-attach-webview', (event, webPreferences, params) => {
        webPreferences.nodeIntegration = false;
        webPreferences.contextIsolation = true;
        webPreferences.sandbox = true;
        webPreferences.webSecurity = true;
        webPreferences.allowRunningInsecureContent = false;
        delete webPreferences.preload;
        delete webPreferences.preloadURL;
        params.allowpopups = false;
        const partition = String(params.partition || webPreferences.partition || '');
        if (partition !== 'persist:variant1-preview') {
          event.preventDefault();
          return;
        }
        webPreferences.partition = 'persist:variant1-preview';
        params.partition = 'persist:variant1-preview';
      });
      contents.on('did-attach-webview', (_event, guest) => {
        hardenGuestContents(guest);
      });
      if (typeof contents.getType === 'function' && contents.getType() === 'webview') {
        hardenGuestContents(contents);
      }
    });
  }

  function isTrustedMediaRequest(webContents, origin, details) {
    if (!webContents || ![getMainWindow(), getDeckWindow(), ...(deps.getChatWindows?.() || [])].includes(
      BrowserWindow.fromWebContents(webContents))) return false;
    const requestUrl = (details && (
      details.requestingUrl || details.securityOrigin || details.requestingOrigin
    )) || origin || webContents.getURL();
    if (!isTrustedAppUrl(requestUrl)) return false;
    return isAudioOnlyMediaDetails(details);
  }

  function configurePermissionHandlers() {
    const appSession = session.defaultSession;
    appSession.setPermissionRequestHandler((webContents, permission, callback, details) => {
      const mediaRequest = permission === 'media' || permission === 'audioCapture';
      callback(mediaRequest && isTrustedMediaRequest(webContents, '', details));
    });
    appSession.setPermissionCheckHandler((webContents, permission, requestingOrigin, details) => {
      const mediaRequest = permission === 'media' || permission === 'audioCapture';
      return mediaRequest && isTrustedMediaRequest(webContents, requestingOrigin, details);
    });

    const previewSession = session.fromPartition('persist:variant1-preview');
    const previewAllowed = new Set(['clipboard-sanitized-write', 'fullscreen']);
    previewSession.setPermissionRequestHandler((_webContents, permission, callback) => {
      callback(previewAllowed.has(permission));
    });
    previewSession.setPermissionCheckHandler((_webContents, permission) => (
      previewAllowed.has(permission)
    ));

  }

  return {
    isTrustedIpcSender,
    hardenAppWindow,
    hardenGuestContents,
    configurePermissionHandlers,
    registerGuestWebviewPolicy,
  };
}

/**
 * Writable data dir + config seed. Mutates paths via callbacks.
 *
 * @param {object} deps
 * @param {import('electron').App} deps.app
 * @param {string} deps.appRoot
 * @param {(dir: string) => void} deps.setDataDir
 * @param {(p: string) => void} deps.setConfigPath
 * @param {(dir: string) => void} deps.setLogDir
 * @param {() => void} deps.rebindSettingsStore
 * @param {() => void} deps.beginSessionLog
 * @param {(msg: string) => void} deps.log
 */
function createInitDataDir(deps) {
  const {
    app, appRoot, setDataDir, setConfigPath, setLogDir,
    rebindSettingsStore, beginSessionLog, log,
  } = deps;

  return function initDataDir() {
    let dataDir;
    const smokeDataDir = process.env.VARIANT1_E2E_SMOKE === '1'
      ? String(process.env.VARIANT1_E2E_DATA_DIR || '').trim()
      : '';
    if (smokeDataDir) {
      dataDir = path.resolve(smokeDataDir);
    } else if (!app.isPackaged) {
      dataDir = appRoot;
    } else {
      dataDir = app.getPath('userData');
    }
    setDataDir(dataDir);
    setConfigPath(path.join(dataDir, 'config', 'settings.json'));
    const logDir = path.join(dataDir, 'logs');
    setLogDir(logDir);
    const speechDir = path.join(dataDir, 'models', 'speech');
    for (const directory of [path.join(dataDir, 'config'), path.join(dataDir, 'data'), logDir,
      path.join(dataDir, 'models', 'user'), path.join(speechDir, 'whisper'), path.join(speechDir, 'kokoro'),
      path.join(dataDir, 'runtimes', 'playwright')]) {
      try { fs.mkdirSync(directory, {recursive: true}); }
      catch (error) { log('[boot] could not create data directory ' + directory + ': ' + String(error?.message || error)); }
    }
    try {
      const speechReadme = path.join(speechDir, 'README.txt');
      if (!fs.existsSync(speechReadme)) {
        fs.writeFileSync(speechReadme, [
          'VARIANT-1 user-supplied speech models',
          '',
          'STT: drop a complete Windows whisper.cpp server distribution into whisper/.',
          'It must include whisper-server.exe, its adjacent DLLs, and a compatible ggml *.bin model.',
          'VARIANT-1 uses the configured model filename when present, otherwise the first *.bin.',
          '',
          'TTS: install/start your own Kokoro-compatible speech server separately.',
          'In Settings > Voice > Kokoro set its API base URL (including /v1), model and voice.',
          'For example: http://127.0.0.1:8880/v1. Use Preview to verify the connection.',
          'Model files alone do not install a speech engine. No offline speech runtime is bundled.',
          '',
          'Use Settings > General > Voice to refresh availability.',
          '',
        ].join('\n'), {encoding: 'utf8', flag: 'wx'});
      }
    } catch (error) { if (error?.code !== 'EEXIST') log('[boot] could not seed speech instructions: ' + String(error?.message || error)); }
    for (const legacyName of ['SOUL.md', 'soul.md', 'system_prompt.txt']) {
      try { fs.rmSync(path.join(dataDir, 'config', legacyName), { force: true }); }
      catch (e) { log('remove legacy ' + legacyName + ' failed: ' + e.message); }
    }
    for (const legacyPrompt of ['chat.txt', 'conversation.txt', 'task.txt']) {
      try {
        fs.rmSync(path.join(dataDir, 'config', 'prompts', legacyPrompt), { force: true });
      } catch (e) {
        log('remove legacy prompt ' + legacyPrompt + ' failed: ' + e.message);
      }
    }
    try {
      fs.rmSync(path.join(dataDir, 'config', 'models_catalog.json'), { force: true });
    } catch (e) {
      log('remove retired model catalog failed: ' + e.message);
    }
    rebindSettingsStore();
    beginSessionLog();
    if (app.isPackaged) {
      const bundledConfig = path.join(process.resourcesPath, 'config');
      const seeds = [
        ['llm_config.json', 'llm_config.default.json'],
        ['tools.json', 'tools.default.json'],
        ['messaging.json', 'messaging.default.json'],
      ];
      for (const [dstName, srcName] of seeds) {
        try {
          const dst = path.join(dataDir, 'config', dstName);
          const src = path.join(bundledConfig, srcName);
          if (!fs.existsSync(dst) && fs.existsSync(src)) fs.copyFileSync(src, dst);
        } catch (e) { log('seed config ' + dstName + ' failed: ' + e.message); }
      }
    }
  };
}

function focusDeckForSecondInstance(app, openDeckWindow) {
  const open = () => openDeckWindow('chat');
  if (typeof app.isReady === 'function' && !app.isReady()) {
    app.whenReady().then(open);
    return null;
  }
  return open();
}

function createBeforeQuitHandler({
  setQuitting,
  stopBackend,
  unregisterShortcuts,
  quit,
  log,
}) {
  let cleanupStarted = false;
  let cleanupComplete = false;
  return function beforeQuit(event) {
    setQuitting(true);
    try { unregisterShortcuts(); } catch (_) {}
    if (cleanupComplete) return;
    if (event && typeof event.preventDefault === 'function') event.preventDefault();
    if (cleanupStarted) return;
    cleanupStarted = true;
    Promise.resolve()
      .then(() => stopBackend())
      .catch((error) => {
        log('backend shutdown failed: ' + String(error && error.message || error));
      })
      .finally(() => {
        cleanupComplete = true;
        try { quit(); } catch (error) {
          log('final app quit failed: ' + String(error && error.message || error));
        }
      });
  };
}

/**
 * Wire whenReady + quit hooks.
 *
 * @param {object} deps
 * @param {import('electron').App} deps.app
 * @param {import('electron').Protocol} deps.protocol
 * @param {string} deps.appRoot
 * @param {() => void} deps.initDataDir
 * @param {(path: string) => void} deps.setPortFile
 * @param {() => void} deps.configurePermissionHandlers
 * @param {() => object} deps.readSettings
 * @param {(view?: string) => import('electron').BrowserWindow|null} deps.openDeckWindow
 * @param {() => void} deps.installTray
  * @param {() => void} deps.startBackend
  * @param {() => void|Promise<void>} deps.stopBackend
 * @param {() => any} deps.getAutoUpdater
 * @param {() => import('electron').BrowserWindow|null} deps.getDeckWindow
 * @param {(v: boolean) => void} deps.setQuitting
 * @param {(msg: string) => void} deps.log
 * @param {() => string} deps.getUserDataBackendJsonPath
 */
function registerAppLifecycle(deps) {
  const {
    app,
    protocol,
    appRoot,
    initDataDir,
    setPortFile,
    configurePermissionHandlers,
    registerGuestWebviewPolicy,
    readSettings,
    openDeckWindow,
    installTray,
    startBackend,
    stopBackend,
    getAutoUpdater,
    getDeckWindow,
    setQuitting,
    log,
    getUserDataBackendJsonPath,
  } = deps;

  function sendDeckWhenReady(win, channel) {
    if (!win || win.isDestroyed() || !win.webContents) return;
    const send = () => {
      if (!win.isDestroyed()) win.webContents.send(channel);
    };
    if (win.webContents.isLoading()) win.webContents.once('did-finish-load', send);
    else send();
  }

  // Single instance
  const gotLock = app.requestSingleInstanceLock();
  if (!gotLock) {
    app.quit();
    return { gotLock: false };
  }
  app.on('second-instance', () => {
    focusDeckForSecondInstance(app, openDeckWindow);
  });

  try {
    if (typeof registerGuestWebviewPolicy === 'function') registerGuestWebviewPolicy();
  } catch (e) { log('webview policy failed: ' + e); }

  app.whenReady().then(() => {
    registerVariant1Protocol(protocol, appRoot);
    initDataDir();
    setPortFile(getUserDataBackendJsonPath());

    try {
      configurePermissionHandlers();
    } catch (e) { log('permission handler failed: ' + e); }

    const startHidden = process.argv.includes('--hidden')
      || !!(readSettings().general && readSettings().general.startHidden);

    if (!startHidden) openDeckWindow('chat');
    installTray();
    startBackend();
    log('VARIANT-1 started.');

    try {
      const up = getAutoUpdater();
      if (up && app.isPackaged) {
        up.checkForUpdatesAndNotify().catch((e) => log('update check failed: ' + e.message));
      }
    } catch (e) { log('auto-update init skipped: ' + e); }

    try {
      const ok = globalShortcut.register('CommandOrControl+Space', () => {
        const deckWindow = openDeckWindow('chat') || getDeckWindow();
        sendDeckWhenReady(deckWindow, 'voice:toggle');
      });
      log('Mic hotkey Ctrl+Space ' + (ok ? 'registered (Main Deck)' : 'NOT registered (in use)'));
    } catch (e) { log('globalShortcut failed: ' + e); }

    app.on('activate', () => {
      const deckWindow = getDeckWindow();
        if (!deckWindow || deckWindow.isDestroyed()) openDeckWindow('chat');
    });
  });

  app.on('window-all-closed', () => {
    // Intentionally do not quit on Windows/Linux — VARIANT-1 lives in the tray.
  });

  app.on('before-quit', createBeforeQuitHandler({
    setQuitting,
    stopBackend,
    unregisterShortcuts: () => globalShortcut.unregisterAll(),
    quit: () => app.quit(),
    log,
  }));

  return { gotLock: true };
}

module.exports = {
  applyGpuFlags,
  registerVariant1Scheme,
  registerVariant1Protocol,
  isAudioOnlyMediaDetails,
  createWindowSecurity,
  createInitDataDir,
  focusDeckForSecondInstance,
  createBeforeQuitHandler,
  registerAppLifecycle,
};
