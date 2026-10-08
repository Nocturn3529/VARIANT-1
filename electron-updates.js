'use strict';

/**
 * Update detection and the user-driven install path.
 *
 * The installed app checks GitHub releases once at start and every 24 hours.
 * Nothing downloads or installs until the user asks: "Download update" fetches
 * it, and "Restart and install" stops the backend before handing over to the
 * installer. Where a platform can't replace itself in place (an unsigned macOS
 * build, an AppImage we can't write, a deb without a graphical sudo) the same
 * button opens the release page instead.
 */

const fs = require('fs');
const path = require('path');

const CHECK_INTERVAL_MS = 24 * 60 * 60 * 1000;
const RELEASES_URL = 'https://github.com/Nocturn3529/VARIANT-1/releases';
const LINUX_SUDO = ['gksudo', 'kdesudo', 'pkexec', 'beesu'];
const PROGRESS_INTERVAL_MS = 250;

function errorText(error) {
  const text = String((error && error.message) || error || 'Update failed');
  // electron-updater appends request dumps and stacks; the first line is the reason.
  return text.split('\n')[0].slice(0, 300);
}

/** The feed: a test override, else the GitHub release config in package.json. */
function resolveFeed({env, appRoot}) {
  const override = String(env.VARIANT1_UPDATE_URL || '').trim();
  if (override) {
    if (/REPLACE-ME|example\.com/i.test(override) || !/^https:\/\//i.test(override)) return null;
    return {provider: 'generic', url: override};
  }
  try {
    const pkg = require(path.join(appRoot, 'package.json'));
    const publish = [].concat(((pkg || {}).build || {}).publish || [])[0] || {};
    if (publish.provider === 'github' && publish.owner && publish.repo) {
      return {provider: 'github', owner: publish.owner, repo: publish.repo,
        ...(publish.releaseType ? {releaseType: publish.releaseType} : {})};
    }
  } catch (_) {}
  return null;
}

function onPath(command, env, fileSystem) {
  return String(env.PATH || '').split(path.delimiter).filter(Boolean).some(dir => {
    try { fileSystem.accessSync(path.join(dir, command), fs.constants.X_OK); return true; } catch (_) { return false; }
  });
}

/**
 * How an available update can be applied on this install.
 * 'in-app': download and restart into the installer.
 * 'release-page': open the release so the user installs it as they did first.
 */
function installModeFor({platform, env, resourcesPath, fileSystem}) {
  if (platform === 'win32') return 'in-app';
  // Squirrel.Mac only installs into a signed app; preview builds are unsigned.
  if (platform === 'darwin') return 'release-page';
  if (platform !== 'linux') return 'release-page';
  const appImage = String(env.APPIMAGE || '');
  if (appImage) {
    try {
      fileSystem.accessSync(appImage, fs.constants.W_OK);
      fileSystem.accessSync(path.dirname(appImage), fs.constants.W_OK);
      return 'in-app';
    } catch (_) {
      return 'release-page';
    }
  }
  let packageType = '';
  try { packageType = fileSystem.readFileSync(path.join(resourcesPath || '', 'package-type'), 'utf8').trim(); } catch (_) {}
  if (!packageType) return 'release-page';
  // deb/rpm/pacman installs ask for the admin password through a graphical sudo.
  return LINUX_SUDO.some(command => onPath(command, env, fileSystem)) ? 'in-app' : 'release-page';
}

/**
 * @param {object} deps
 * @param {import('electron').App} deps.app
 * @param {{openExternal: (url: string) => Promise<void>}} deps.shell
 * @param {string} deps.appRoot
 * @param {(state: object) => void} deps.broadcast  sends each state change to the Deck
 * @param {() => Promise<void>} deps.prepareInstall  flushes storage and stops the backend
 * @param {(msg: string) => void} deps.log
 */
function createUpdateService(deps) {
  const {
    app, shell, appRoot, broadcast, prepareInstall, log,
    platform = process.platform,
    env = process.env,
    resourcesPath = process.resourcesPath,
    fileSystem = fs,
    now = Date.now,
    every = setInterval,
    stopEvery = clearInterval,
    loadUpdater = () => require('electron-updater').autoUpdater,
    // Resolved through electron-updater so a nested copy is used if npm nests it.
    newCancellation = () => new (require(require.resolve('builder-util-runtime',
      {paths: [path.dirname(require.resolve('electron-updater'))]})).CancellationToken)(),
  } = deps;

  const feed = resolveFeed({env, appRoot});
  const reason = !app.isPackaged ? 'dev_mode' : !feed ? 'unconfigured' : '';
  let updater = null;
  let timer = null;
  let cancellation = null;
  let lastProgressAt = 0;
  let state = {
    status: reason ? 'unavailable' : 'idle',
    reason,
    currentVersion: app.getVersion(),
    platform,
    version: '',
    releaseName: '',
    releaseUrl: RELEASES_URL,
    installMode: installModeFor({platform, env, resourcesPath, fileSystem}),
    percent: 0,
    transferred: 0,
    total: 0,
    error: '',
    checkedAt: 0,
  };

  function set(patch) {
    state = {...state, ...patch};
    try { broadcast({...state}); } catch (_) {}
  }

  function getUpdater() {
    if (state.reason) return null;
    if (updater) return updater;
    try {
      updater = loadUpdater();
      updater.autoDownload = false;
      updater.autoInstallOnAppQuit = false;
      // The packaged app-update.yml would also work; setting it here keeps the
      // feed identical in every build and lets VARIANT1_UPDATE_URL override it.
      updater.setFeedURL(feed);
      updater.on('download-progress', progress => {
        const at = now();
        if (at - lastProgressAt < PROGRESS_INTERVAL_MS && progress.percent < 100) return;
        lastProgressAt = at;
        set({percent: Math.max(0, Math.min(100, Number(progress.percent) || 0)),
          transferred: Number(progress.transferred) || 0, total: Number(progress.total) || 0});
      });
    } catch (error) {
      log('[updates] updater unavailable: ' + errorText(error));
      updater = null;
      set({status: 'unavailable', reason: 'updater_unavailable'});
    }
    return updater;
  }

  async function check() {
    const up = getUpdater();
    if (!up) return {...state};
    // A fetched update, or one in flight, stays as it is until the user acts.
    if (['checking', 'downloading', 'downloaded', 'installing'].includes(state.status)) return {...state};
    // A known update stays on screen while it is re-checked.
    if (state.status !== 'available') set({status: 'checking', error: ''});
    try {
      const result = await up.checkForUpdates();
      const info = result && result.updateInfo;
      if (result && result.isUpdateAvailable && info && info.version) {
        set({status: 'available', checkedAt: now(), version: String(info.version),
          releaseName: String(info.releaseName || ''),
          releaseUrl: `${RELEASES_URL}/tag/v${String(info.version).replace(/^v/, '')}`});
      } else {
        set({status: 'up-to-date', checkedAt: now(), version: '', releaseName: '', releaseUrl: RELEASES_URL});
      }
    } catch (error) {
      log('[updates] check failed: ' + errorText(error));
      set({status: 'error', checkedAt: now(), error: errorText(error)});
    }
    return {...state};
  }

  async function download() {
    const up = getUpdater();
    if (!up || state.status !== 'available' || state.installMode !== 'in-app') {
      return {ok: false, reason: 'not_available'};
    }
    cancellation = newCancellation();
    set({status: 'downloading', percent: 0, transferred: 0, total: 0, error: ''});
    try {
      await up.downloadUpdate(cancellation);
      set({status: 'downloaded', percent: 100});
      return {ok: true};
    } catch (error) {
      if (cancellation && cancellation.cancelled) {
        set({status: 'available', percent: 0, transferred: 0, total: 0});
        return {ok: false, reason: 'cancelled'};
      }
      log('[updates] download failed: ' + errorText(error));
      set({status: 'error', error: errorText(error)});
      return {ok: false, reason: 'failed'};
    } finally {
      cancellation = null;
    }
  }

  function cancel() {
    if (state.status !== 'downloading' || !cancellation) return {ok: false};
    cancellation.cancel();
    return {ok: true};
  }

  async function install() {
    const up = getUpdater();
    if (!up || state.status !== 'downloaded') return {ok: false, reason: 'not_downloaded'};
    set({status: 'installing'});
    // The installer can't replace a running backend's files, and Chromium writes
    // tab lists and cookies lazily; both have to settle before we hand over.
    try { await prepareInstall(); } catch (error) { log('[updates] install preparation failed: ' + errorText(error)); }
    log(`[updates] installing ${state.version}`);
    up.quitAndInstall(false, true);
    return {ok: true};
  }

  async function openRelease() {
    await shell.openExternal(state.releaseUrl || RELEASES_URL);
    return {ok: true};
  }

  function start() {
    if (state.reason || timer) return;
    void check();
    timer = every(() => { void check(); }, CHECK_INTERVAL_MS);
    if (timer && typeof timer.unref === 'function') timer.unref();
  }

  function stop() {
    if (timer) stopEvery(timer);
    timer = null;
  }

  return {start, stop, check, download, cancel, install, openRelease, getState: () => ({...state})};
}

module.exports = {createUpdateService, installModeFor, resolveFeed, CHECK_INTERVAL_MS, RELEASES_URL};
