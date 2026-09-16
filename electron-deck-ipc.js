'use strict';

/**
 * Main Deck + Settings + Monitor IPC handlers for the Electron main process.
 * Backend discovery remains in the main-process composition root.
 */

const path = require('path');
const fs = require('fs');
const { execFile } = require('child_process');
const { BrowserWindow, ipcMain, dialog, shell, clipboard } = require('electron');
const { isAllowedExternalUrl } = require('./electron-security');
const {createWorkbenchWatchers, createReadCache, limitReadConcurrency, sharePendingRead} = require('./electron-workbench-watchers');
const { isEditableText } = require('./electron-workbench-files');

function normalizeAbsoluteLocalPath(rawPath) {
  if (typeof rawPath !== 'string' || !rawPath.trim() || rawPath.includes('\0')) {
    return null;
  }
  if (!path.isAbsolute(rawPath)) return null;
  const normalized = path.normalize(rawPath);
  return path.isAbsolute(normalized) ? normalized : null;
}

function repositoryFile(root, raw) {
  if (typeof raw !== 'string' || !raw || raw.includes('\0') || path.isAbsolute(raw)
      || path.win32.isAbsolute(raw) || /^[a-z]:/i.test(raw) || raw.split(/[\\/]/).includes('..')) {
    throw new Error('invalid_repository_file');
  }
  const absolute = path.resolve(root, raw);
  const relative = path.relative(root, absolute);
  if (!relative || relative === '..' || relative.startsWith('..' + path.sep) || path.isAbsolute(relative)) throw new Error('invalid_repository_file');
  return {absolute, relative: relative.split(path.sep).join('/')};
}

const WORKBENCH_HIDDEN_NAMES = new Set([
  '.git', '.hg', '.svn', '.DS_Store', '__pycache__', '.pytest_cache',
  '.mypy_cache', '.ruff_cache', '.next', '.turbo', '.venv', 'node_modules',
  'coverage', 'dist', 'build', 'release',
]);

function mediaTypeFor(filePath) {
  const ext = path.extname(filePath).toLowerCase();
  return ({
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
    '.svg': 'image/svg+xml', '.pdf': 'application/pdf', '.html': 'text/html',
    '.htm': 'text/html', '.md': 'text/markdown', '.json': 'application/json',
    '.js': 'text/javascript', '.mjs': 'text/javascript', '.cjs': 'text/javascript',
    '.ts': 'text/typescript', '.tsx': 'text/tsx', '.jsx': 'text/jsx',
    '.css': 'text/css', '.csv': 'text/csv', '.xml': 'application/xml',
  })[ext] || 'text/plain';
}

function gitHooksPath() {
  return process.platform === 'win32' ? 'NUL' : '/dev/null';
}

function gitArgs(args) {
  return ['-c', `core.hooksPath=${gitHooksPath()}`, ...args];
}

function gitCommandEnv(extra) {
  return {
    ...process.env,
    GIT_OPTIONAL_LOCKS: '0',
    GIT_TERMINAL_PROMPT: '0',
    GIT_LITERAL_PATHSPECS: '1',
    ...(extra || {}),
  };
}

function runFile(executable, args, options = {}) {
  return new Promise((resolve, reject) => {
    execFile(executable, args, {
      cwd: options.cwd,
      windowsHide: true,
      timeout: options.timeout || 30000,
      maxBuffer: options.maxBuffer || 16 * 1024 * 1024,
      encoding: 'utf8',
      env: options.env,
    }, (error, stdout, stderr) => {
      if (error) {
        error.stdout = stdout;
        error.stderr = stderr;
        reject(error);
        return;
      }
      resolve({ stdout: String(stdout || ''), stderr: String(stderr || '') });
    });
  });
}

const runGitRead = limitReadConcurrency(2);
function readGit(args, options = {}) {
  return runGitRead(() => runFile('git', gitArgs(args), {...options, env: gitCommandEnv()}));
}

async function gitRoot(startPath) {
  const base = fs.existsSync(startPath) && fs.statSync(startPath).isDirectory()
    ? startPath : path.dirname(startPath);
  const result = await readGit(['rev-parse', '--show-toplevel'], { cwd: base });
  return path.normalize(result.stdout.trim());
}

async function workbenchGitStatus(startPath) {
  const root = await gitRoot(startPath);
  const branchResult = await readGit(['status', '--porcelain=v1', '-z', '-b'], { cwd: root });
  const records = branchResult.stdout.split('\0');
  const header = records.shift() || '';
  const branchMatch = /^##\s+([^\.\s]+)(?:\.\.\.([^\s]+))?(?:\s+\[ahead\s+(\d+)(?:,\s+behind\s+(\d+))?\])?/.exec(header);
  const files = [];
  for (let index = 0; index < records.length; index += 1) {
    const row = records[index];
    if (!row || row.length < 3) continue;
    const x = row[0];
    const y = row[1];
    const filePath = row.slice(3);
    let originalPath = '';
    if (x === 'R' || x === 'C' || y === 'R' || y === 'C') {
      originalPath = records[index + 1] || '';
      index += 1;
    }
    files.push({ path: filePath, originalPath, status: `${x}${y}`, staged: x !== ' ' && x !== '?' });
  }
  const counts = new Map();
  const parseNumstat = (text) => {
    for (const line of String(text || '').split(/\r?\n/)) {
      if (!line) continue;
      const [added, removed, ...nameParts] = line.split('\t');
      const name = nameParts.join('\t').replace(/^.* => /, '').replace(/[{}]/g, '');
      if (!name) continue;
      const prior = counts.get(name) || { added: 0, removed: 0 };
      prior.added += /^\d+$/.test(added) ? Number(added) : 0;
      prior.removed += /^\d+$/.test(removed) ? Number(removed) : 0;
      counts.set(name, prior);
    }
  };
  const [workingCounts, stagedCounts] = await Promise.all([
    readGit(['diff', '--numstat', '--no-renames'], { cwd: root }).catch(() => ({ stdout: '' })),
    readGit(['diff', '--cached', '--numstat', '--no-renames'], { cwd: root }).catch(() => ({ stdout: '' })),
  ]);
  parseNumstat(workingCounts.stdout);
  parseNumstat(stagedCounts.stdout);
  for (const file of files) Object.assign(file, counts.get(file.path) || { added: 0, removed: 0 });
  return {
    ok: true,
    root,
    branch: branchMatch ? branchMatch[1] : header.replace(/^##\s*/, ''),
    ahead: Number(/\bahead (\d+)/.exec(header)?.[1] || 0),
    behind: Number(/\bbehind (\d+)/.exec(header)?.[1] || 0),
    files,
  };
}

/**
 * @param {object} deps
 * @param {import('electron').App} deps.app
 * @param {string} deps.appRoot
 * @param {() => string} deps.getDataDir
 * @param {() => string} deps.getLogDir
 * @param {() => string|null} deps.getPortFile
 * @param {() => import('electron').BrowserWindow|null} deps.getDeckWindow
 * @param {() => import('electron').BrowserWindow|null} deps.getMonitorWindow
 * @param {() => void} [deps.openMonitorWindow]
 * @param {(event: any, win?: import('electron').BrowserWindow|null) => boolean} deps.isTrustedIpcSender
 * @param {() => object} deps.readSettings
 * @param {(s: object) => void} deps.writeSettings
 * @param {(msg: string) => void} deps.log
 * @param {() => string[]} [deps.getLogBuffer]
 * @param {() => string[]} [deps.clearLogBuffer]
 * @param {() => boolean} deps.updateFeedConfigured
 * @param {() => string} deps.updateFeedUrl
 * @param {() => any} deps.getAutoUpdater
 */
function registerDeckIpc(deps) {
  const {
    app,
    appRoot,
    getDataDir,
    getLogDir,
    getPortFile,
    getDeckWindow,
    getMonitorWindow,
    openMonitorWindow,
    isTrustedIpcSender,
    readSettings,
    writeSettings,
    log,
    getLogBuffer,
    clearLogBuffer,
    updateFeedConfigured,
    updateFeedUrl,
    getAutoUpdater,
  } = deps;

  const deckWin = () => getDeckWindow();
  const monWin = () => getMonitorWindow();
  ipcMain.handle('deck:openMonitor', (event) => {
    if (!isTrustedIpcSender(event, deckWin())) return { ok: false, reason: 'untrusted' };
    if (typeof openMonitorWindow !== 'function') return { ok: false, reason: 'Log monitor unavailable' };
    openMonitorWindow();
    return { ok: true };
  });
  const applyLaunchAtLogin = (openAtLogin, startHidden) => app.setLoginItemSettings({
    openAtLogin: !!openAtLogin,
    args: openAtLogin && startHidden ? ['--hidden'] : [],
  });
  const gitStatus = createReadCache(target => workbenchGitStatus(target).catch(error => ({ok:false,files:[],error:String(error?.stderr || error?.message || error)})));
  const watchers = createWorkbenchWatchers({hiddenNames: WORKBENCH_HIDDEN_NAMES, changed: () => gitStatus.invalidate()});

  ipcMain.handle('settings:get', (event) => (
    deckWin() && isTrustedIpcSender(event, deckWin()) ? readSettings() : null
  ));

  ipcMain.on('log', (event, msg) => {
    if (isTrustedIpcSender(event)) log('[renderer] ' + String(msg || ''));
  });

  ipcMain.handle('dialog:pickFolder', async (event) => {
    if (!deckWin() || !isTrustedIpcSender(event, deckWin())) return null;
    try {
      const owner = BrowserWindow.fromWebContents(event.sender);
      const res = await dialog.showOpenDialog(owner, {
        title: 'Allow VARIANT-1 to access a folder',
        properties: ['openDirectory', 'createDirectory'],
      });
      if (res.canceled || !res.filePaths || !res.filePaths.length) return null;
      return res.filePaths[0];
    } catch (e) {
      log('[main] pickFolder failed: ' + e);
      return null;
    }
  });

  ipcMain.handle('settings:getLaunchAtLogin', (event) => {
    if (!isTrustedIpcSender(event, deckWin())) return false;
    try { return app.getLoginItemSettings().openAtLogin; } catch (_) { return false; }
  });

  function setStartupPreference(event, key, on) {
    if (!isTrustedIpcSender(event, deckWin())) return { ok: false, value: false, reason: 'untrusted_sender' };
    let previous;
    try {
      const settings = readSettings();
      const general = settings.general || {};
      previous = {openAtLogin: !!app.getLoginItemSettings().openAtLogin, startHidden: !!general.startHidden};
      const requested = !!on;
      const next = {...previous, [key === 'autoStart' ? 'openAtLogin' : 'startHidden']: requested};
      applyLaunchAtLogin(next.openAtLogin, next.startHidden);
      if (!!app.getLoginItemSettings().openAtLogin !== next.openAtLogin) throw new Error('login_item_not_applied');
      settings.general = {...general, [key]: requested};
      writeSettings(settings); // Atomic storage throws; success means both operations completed.
      return { ok: true, value: requested };
    } catch (err) {
      let reason = String(err && err.message || err);
      if (previous) {
        try { applyLaunchAtLogin(previous.openAtLogin, previous.startHidden); }
        catch (rollbackError) { reason += '; login setting rollback failed: ' + String(rollbackError && rollbackError.message || rollbackError); }
      }
      log('setStartupPreference failed: ' + reason);
      let actual = previous?.startHidden || false;
      if (key === 'autoStart') {
        try { actual = !!app.getLoginItemSettings().openAtLogin; } catch (_) { actual = previous?.openAtLogin || false; }
      }
      return { ok: false, value: actual, reason };
    }
  }
  ipcMain.handle('settings:setLaunchAtLogin', (event, on) => setStartupPreference(event, 'autoStart', on));
  ipcMain.handle('settings:setStartHidden', (event, on) => setStartupPreference(event, 'startHidden', on));

  ipcMain.handle('app:getInfo', (event) => {
    if (!isTrustedIpcSender(event, deckWin())) return null;
    const dataDir = getDataDir();
    return {
      version: app.getVersion(),
      electron: process.versions.electron,
      node: process.versions.node,
      platform: process.platform,
      arch: process.arch,
      packaged: app.isPackaged,
      updateFeedConfigured: updateFeedConfigured(),
      updateFeedUrl: updateFeedConfigured() ? updateFeedUrl() : '',
      paths: {
        appRoot,
        userData: app.getPath('userData'),
        dataDir,
        config: path.join(dataDir, 'config'),
        logs: getLogDir(),
        models: path.join(dataDir, 'models'),
        modelsUser: path.join(dataDir, 'models', 'user'),
        plugins: path.join(dataDir, 'config', 'plugins'),
        portFile: getPortFile(),
      },
      updaterAvailable: !!getAutoUpdater(),
    };
  });

  ipcMain.handle('app:openPath', async (event, key) => {
    if (!isTrustedIpcSender(event, deckWin())) {
      return { ok: false, reason: 'untrusted_sender' };
    }
    const dataDir = getDataDir();
    const allowed = {
      config: path.join(dataDir, 'config'),
      dataDir,
      logs: getLogDir(),
      models: path.join(dataDir, 'models'),
      modelsUser: path.join(dataDir, 'models', 'user'),
      plugins: path.join(dataDir, 'config', 'plugins'),
    };
    const target = allowed[String(key || '')];
    if (!target) return { ok: false, reason: 'path_not_allowed' };
    try {
      fs.mkdirSync(target, { recursive: true });
      const error = await shell.openPath(target);
      return error ? { ok: false, reason: error } : { ok: true };
    } catch (err) {
      return { ok: false, reason: String(err && err.message || err) };
    }
  });

  ipcMain.handle('localPath:open', async (event, rawPath) => {
    if (!isTrustedIpcSender(event, deckWin())) {
      return { ok: false, reason: 'untrusted_sender' };
    }
    const target = normalizeAbsoluteLocalPath(rawPath);
    if (!target) return { ok: false, reason: 'invalid_path' };
    try {
      const stat = await fs.promises.stat(target);
      if (!stat.isDirectory()) {
        shell.showItemInFolder(target);
        return { ok: true };
      }
      const error = await shell.openPath(target);
      return error ? { ok: false, reason: error } : { ok: true };
    } catch (err) {
      return { ok: false, reason: String(err && err.message || err) };
    }
  });

  // Hermes-style workbench filesystem. These calls deliberately use the
  // signed-in user's filesystem authority; normalization rejects malformed
  // payloads but does not impose a product-level path sandbox.
  ipcMain.handle('workbench:root', async (event) => {
    if (!isTrustedIpcSender(event, deckWin())) return { ok: false, error: 'untrusted_sender' };
    const explicit = String(process.env.VARIANT1_PROJECT_ROOT || '').trim();
    const candidate = explicit || (app.isPackaged ? getDataDir() : appRoot);
    try {
      const resolved = path.resolve(candidate);
      const stat = await fs.promises.stat(resolved);
      return stat.isDirectory() ? { ok: true, path: resolved } : { ok: false, error: 'not_a_directory' };
    } catch (error) {
      return { ok: false, error: String(error && error.message || error) };
    }
  });

  ipcMain.handle('workbench:fs:readDir', async (event, rawPath) => {
    if (!isTrustedIpcSender(event, deckWin())) return { ok: false, error: 'untrusted_sender', entries: [] };
    const target = normalizeAbsoluteLocalPath(rawPath);
    if (!target) return { ok: false, error: 'invalid_path', entries: [] };
    try {
      const rows = await fs.promises.readdir(target, { withFileTypes: true });
      const entries = await Promise.all(rows.filter(row => !WORKBENCH_HIDDEN_NAMES.has(row.name)).map(async row => {
        const fullPath = path.join(target, row.name);
        let stat = null;
        try { stat = await fs.promises.stat(fullPath); } catch (_) { /* preserve the visible dirent */ }
        return {
          name: row.name,
          path: fullPath,
          directory: row.isDirectory() || !!(stat && stat.isDirectory()),
          symlink: row.isSymbolicLink(),
          size: stat ? stat.size : 0,
          mtimeMs: stat ? stat.mtimeMs : 0,
        };
      }));
      entries.sort((a, b) => Number(b.directory) - Number(a.directory)
        || a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' }));
      return { ok: true, path: target, entries };
    } catch (error) {
      return { ok: false, error: String(error && error.message || error), path: target, entries: [] };
    }
  });

  ipcMain.handle('workbench:fs:readFile', async (event, rawPath) => {
    if (!isTrustedIpcSender(event, deckWin())) return { ok: false, error: 'untrusted_sender' };
    const target = normalizeAbsoluteLocalPath(rawPath);
    if (!target) return { ok: false, error: 'invalid_path' };
    try {
      const stat = await fs.promises.stat(target);
      if (!stat.isFile()) return { ok: false, error: 'not_a_file' };
      const maxBytes = 16 * 1024 * 1024;
      if (stat.size > maxBytes) {
        return { ok: false, error: 'file_too_large', path: target, size: stat.size, mtimeMs: stat.mtimeMs, truncated: true };
      }
      const bytes = await fs.promises.readFile(target);
      const mediaType = mediaTypeFor(target);
      const visual = mediaType.startsWith('image/') || mediaType === 'application/pdf' || mediaType === 'text/html';
      const editable = isEditableText(bytes);
      const binary = !editable;
      return {
        ok: true,
        path: target,
        size: stat.size,
        mtimeMs: stat.mtimeMs,
        mediaType,
        binary,
        editable,
        text: editable ? bytes.toString('utf8') : '',
        dataUrl: visual ? `data:${mediaType};base64,${bytes.toString('base64')}` : '',
      };
    } catch (error) {
      return { ok: false, error: String(error && error.message || error), path: target };
    }
  });

  ipcMain.handle('workbench:fs:writeFile', async (event, rawPath, content, expectedMtimeMs) => {
    if (!isTrustedIpcSender(event, deckWin())) return { ok: false, error: 'untrusted_sender' };
    const target = normalizeAbsoluteLocalPath(rawPath);
    if (!target) return { ok: false, error: 'invalid_path' };
    const text = String(content == null ? '' : content);
    if (Buffer.byteLength(text, 'utf8') > 16 * 1024 * 1024) return { ok: false, error: 'content_too_large' };
    try {
      const before = await fs.promises.stat(target);
      if (!before.isFile()) return { ok: false, error: 'not_a_file' };
      if (before.size > 16 * 1024 * 1024) return { ok: false, error: 'file_too_large' };
      // Recheck on write: the file may have changed since it was previewed,
      // and even a forced conflict overwrite must never erase binary bytes.
      if (!isEditableText(await fs.promises.readFile(target))) {
        return { ok: false, error: 'binary_file_not_editable' };
      }
      if (Number.isFinite(Number(expectedMtimeMs)) && Number(expectedMtimeMs) > 0
          && Math.abs(before.mtimeMs - Number(expectedMtimeMs)) > 1) {
        return { ok: false, conflict: true, mtimeMs: before.mtimeMs };
      }
      await fs.promises.writeFile(target, text, 'utf8');
      const after = await fs.promises.stat(target);
      return { ok: true, mtimeMs: after.mtimeMs };
    } catch (error) {
      return { ok: false, error: String(error && error.message || error) };
    }
  });

  ipcMain.handle('workbench:fs:rename', async (event, rawPath, rawName) => {
    if (!isTrustedIpcSender(event, deckWin())) return { ok: false, error: 'untrusted_sender' };
    const source = normalizeAbsoluteLocalPath(rawPath);
    const name = String(rawName || '').trim();
    if (!source || !name || name === '.' || name === '..' || /[\\/\0]/.test(name)) {
      return { ok: false, error: 'invalid_rename' };
    }
    const destination = path.join(path.dirname(source), name);
    try {
      if (fs.existsSync(destination)) return { ok: false, error: 'destination_exists' };
      await fs.promises.rename(source, destination);
      return { ok: true, path: destination };
    } catch (error) {
      return { ok: false, error: String(error && error.message || error) };
    }
  });

  ipcMain.handle('workbench:fs:trash', async (event, rawPath) => {
    if (!isTrustedIpcSender(event, deckWin())) return { ok: false, error: 'untrusted_sender' };
    const target = normalizeAbsoluteLocalPath(rawPath);
    if (!target) return { ok: false, error: 'invalid_path' };
    try { await shell.trashItem(target); return { ok: true }; }
    catch (error) { return { ok: false, error: String(error && error.message || error) }; }
  });

  ipcMain.handle('workbench:fs:reveal', async (event, rawPath) => {
    if (!isTrustedIpcSender(event, deckWin())) return { ok: false, error: 'untrusted_sender' };
    const target = normalizeAbsoluteLocalPath(rawPath);
    if (!target) return { ok: false, error: 'invalid_path' };
    try { shell.showItemInFolder(target); return { ok: true }; }
    catch (error) { return { ok: false, error: String(error && error.message || error) }; }
  });

  ipcMain.handle('workbench:fs:watch', async (event, rawPath, options) => {
    if (!deckWin() || !isTrustedIpcSender(event, deckWin())) return {ok:false,error:'untrusted_sender'};
    const target = normalizeAbsoluteLocalPath(rawPath);
    if (!target) return {ok:false,error:'invalid_path'};
    try { return {ok:true,id:watchers.start(event.sender,target,options?.scope === 'workspace' ? 'workspace' : 'directory')}; }
    catch (error) { return {ok:false,error:String(error?.message || error)}; }
  });
  ipcMain.handle('workbench:fs:unwatch', async (event, rawId) => {
    if (!deckWin() || !isTrustedIpcSender(event, deckWin())) return {ok:false};
    watchers.stop(event.sender,String(rawId || '')); return {ok:true};
  });

  ipcMain.handle('workbench:git:status', async (event, rawPath) => {
    if (!isTrustedIpcSender(event, deckWin())) return { ok: false, error: 'untrusted_sender', files: [] };
    const target = normalizeAbsoluteLocalPath(rawPath);
    if (!target) return { ok: false, error: 'invalid_path', files: [] };
    try { return await gitStatus.get(process.platform === 'win32' ? target.toLowerCase() : target); }
    catch (error) { return { ok: false, error: String(error && error.stderr || error && error.message || error), files: [] }; }
  });

  const readDiff = sharePendingRead(async key => {
    const [target,filePath,staged] = JSON.parse(key);
    try {
      const root = await gitRoot(target);
      const args = ['diff', '--no-ext-diff', '--no-color', '--unified=3'];
      if (staged) args.push('--cached');
      if (filePath) args.push('--', String(filePath));
      const result = await readGit(args, { cwd: root, timeout: 60000, maxBuffer: 32 * 1024 * 1024 });
      return { ok: true, root, diff: result.stdout };
    } catch (error) {
      return { ok: false, error: String(error && error.stderr || error && error.message || error) };
    }
  });
  ipcMain.handle('workbench:git:diff', async (event, rawPath, filePath, staged) => {
    if (!isTrustedIpcSender(event, deckWin())) return { ok: false, error: 'untrusted_sender' };
    const target = normalizeAbsoluteLocalPath(rawPath);
    if (!target) return { ok: false, error: 'invalid_path' };
    return readDiff(JSON.stringify([target,String(filePath || ''),!!staged]));
  });

  ipcMain.handle('workbench:git:run', async (event, action, rawPath, options) => {
    if (!isTrustedIpcSender(event, deckWin())) return { ok: false, error: 'untrusted_sender' };
    const target = normalizeAbsoluteLocalPath(rawPath);
    if (!target) return { ok: false, error: 'invalid_path' };
    let committed = false;
    try {
      const root = await gitRoot(target);
      const input = options && typeof options === 'object' ? options : {};
      const file = input.file == null || input.file === '' ? null : repositoryFile(root, input.file);
      const selected = file?.relative || null;
      let executable = 'git';
      let args = [];
      if (action === 'stage') args = ['add', '--', ...(selected ? [selected] : ['.'])];
      else if (action === 'unstage') args = ['restore', '--staged', '--', ...(selected ? [selected] : ['.'])];
      else if (action === 'revert') {
        if (!selected) throw new Error('revert requires a file');
        const absolute = file.absolute;
        if (!fs.existsSync(absolute)) return { ok: true, root };
        const status = await workbenchGitStatus(root);
        const row = (status.files || []).find(item => item.path === selected);
        if (row && row.status === '??') {
          // A repository-relative item must not traverse a junction/symlink parent.
          const realRoot = await fs.promises.realpath(root);
          const realParent = await fs.promises.realpath(path.dirname(absolute));
          const parentRelative = path.relative(realRoot, realParent);
          if (parentRelative === '..' || parentRelative.startsWith('..' + path.sep) || path.isAbsolute(parentRelative)) throw new Error('invalid_repository_file');
          await shell.trashItem(absolute);
          gitStatus.invalidate();
          return { ok: true, root };
        }
        args = ['restore', '--worktree', '--', selected];
      } else if (action === 'commit') {
        const message = String(input.message || '').trim();
        if (!message) throw new Error('commit message is required');
        args = ['commit', '-m', message];
      } else if (action === 'push') args = ['push'];
      else if (action === 'commit_push') {
        const message = String(input.message || '').trim();
        if (!message) throw new Error('commit message is required');
        await runFile('git', gitArgs(['commit', '-m', message]), {
          cwd: root, timeout: 120000, env: gitCommandEnv(),
        });
        committed = true;
        gitStatus.invalidate();
        args = ['push'];
      } else if (action === 'create_pr') {
        executable = 'gh';
        args = ['pr', 'create', '--fill'];
      } else throw new Error(`unknown git action: ${action}`);
      const runArgs = executable === 'git' ? gitArgs(args) : args;
      const result = await runFile(executable, runArgs, {
        cwd: root, timeout: 120000, maxBuffer: 32 * 1024 * 1024,
        env: executable === 'git' ? gitCommandEnv() : process.env,
      });
      gitStatus.invalidate();
      return { ok: true, root, committed: committed || action === 'commit', stdout: result.stdout, stderr: result.stderr };
    } catch (error) {
      return { ok: false, committed, error: String(error && error.stderr || error && error.message || error) };
    }
  });

  ipcMain.handle('external:open', async (event, rawUrl) => {
    if (!isTrustedIpcSender(event, deckWin())) {
      return { ok: false, reason: 'untrusted_sender' };
    }
    const target = String(rawUrl || '').trim();
    if (!isAllowedExternalUrl(target)) return { ok: false, reason: 'url_not_allowed' };
    try {
      await shell.openExternal(target);
      return { ok: true };
    } catch (err) {
      log('[main] openExternal failed: ' + err);
      return { ok: false, reason: String(err && err.message || err) };
    }
  });

  ipcMain.handle('update:check', async (event) => {
    if (!isTrustedIpcSender(event, deckWin())) {
      return { ok: false, reason: 'untrusted_sender' };
    }
    if (!updateFeedConfigured()) return { ok: false, reason: 'updater_unconfigured' };
    const up = getAutoUpdater();
    if (!up) return { ok: false, reason: 'updater_unavailable' };
    if (!app.isPackaged) return { ok: false, reason: 'dev_mode' };
    try {
      const r = await up.checkForUpdates();
      return {
        ok: true,
        available: !!(r && r.isUpdateAvailable),
        version: r && r.updateInfo ? r.updateInfo.version : null,
      };
    } catch (err) {
      return { ok: false, reason: String(err && err.message || err) };
    }
  });

  ipcMain.on('deck:minimize', (event) => {
    const win = deckWin();
    if (isTrustedIpcSender(event, win) && win) win.minimize();
  });

  ipcMain.on('deck:toggleMaximize', (event) => {
    const win = deckWin();
    if (!isTrustedIpcSender(event, win) || !win) return;
    if (win.isMaximized()) win.unmaximize();
    else win.maximize();
  });

  ipcMain.on('deck:close', (event) => {
    const win = deckWin();
    if (isTrustedIpcSender(event, win) && win) win.close();
  });

  ipcMain.on('monitor:close', (event) => {
    const win = monWin();
    if (!isTrustedIpcSender(event, win)) return;
    if (win && !win.isDestroyed()) win.close();
  });

  ipcMain.handle('monitor:window', (event, action) => {
    const win = monWin();
    if (!isTrustedIpcSender(event, win) || !win || win.isDestroyed()) return {ok: false};
    if (action === 'minimize') win.minimize();
    else if (action === 'maximize') win.isMaximized() ? win.unmaximize() : win.maximize();
    else if (action === 'pin') win.setAlwaysOnTop(!win.isAlwaysOnTop());
    else if (action !== 'state') return {ok: false};
    return {ok: true, pinned: win.isAlwaysOnTop()};
  });

  // Live Logs pop-out (session main0 buffer)
  ipcMain.handle('logs:getHistory', (event) => {
    if (!isTrustedIpcSender(event, monWin())) return [];
    return typeof getLogBuffer === 'function' ? getLogBuffer() : [];
  });

  ipcMain.handle('logs:clear', (event) => {
    if (!isTrustedIpcSender(event, monWin())) return [];
    // clearLogBuffer resets main0 + ring; do not logToFile here — a live
    // line event would race the returned snapshot and duplicate in the UI.
    if (typeof clearLogBuffer === 'function') clearLogBuffer();
    return typeof getLogBuffer === 'function' ? getLogBuffer() : [];
  });

  ipcMain.handle('logs:openFolder', async (event) => {
    if (!isTrustedIpcSender(event, monWin())) {
      return false;
    }
    try {
      const folder = getLogDir();
      if (!folder) return false;
      const err = await shell.openPath(folder);
      return !err;
    } catch (e) {
      log('[main] open log folder failed: ' + e);
      return false;
    }
  });

  ipcMain.handle('logs:copy', (event) => {
    if (!isTrustedIpcSender(event, monWin())) return false;
    try {
      const buf = typeof getLogBuffer === 'function' ? getLogBuffer() : [];
      clipboard.writeText(Array.isArray(buf) ? buf.join('\n') : '');
      return true;
    } catch (e) {
      log('[main] copy logs failed: ' + e);
      return false;
    }
  });

}

module.exports = { registerDeckIpc, normalizeAbsoluteLocalPath };
