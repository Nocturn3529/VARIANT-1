'use strict';

/**
 * VARIANT-1 — Main Deck preload bridge.
 *
 * The Main Deck is a separate normal BrowserWindow. It talks to the same
 * Python backend over its own WebSocket, so it needs the backend
 * connection details; it also needs a few main-process-only capabilities
 * (launch-at-login, app version/paths, manual update check). All exposed
 * through a minimal, explicit API — contextIsolation on, nodeIntegration off.
 */

const { contextBridge, ipcRenderer, webUtils } = require('electron');

contextBridge.exposeInMainWorld('variant1Deck', {
  supportsNativeWindows: true,
  workbenchBrowser: command => ipcRenderer.invoke('workbench:browser:view', command),
  onWorkbenchBrowserEvent: callback => {
    const listener = (_event, value) => callback(value);
    ipcRenderer.on('workbench:browser:event', listener);
    return () => ipcRenderer.removeListener('workbench:browser:event', listener);
  },
  listChatWindows:()=>ipcRenderer.invoke('chat-window:list'),
  manageChatWindow:(id,action)=>ipcRenderer.invoke('chat-window:manage',id,action),
  onChatWindowsChanged:callback=>{const listener=(_event,value)=>callback(value);ipcRenderer.on('chat-window:changed',listener);return()=>ipcRenderer.removeListener('chat-window:changed',listener);},
  openChatWindow:(id,title)=>ipcRenderer.invoke("chat-window:open",id,title),
  captureWorkbenchPreview: id => ipcRenderer.invoke('workbench:browser:capture', id),
  controlNativeWindow: (id, action, size) => ipcRenderer.invoke('workbench:window:control', String(id), String(action), size),
  bindWorkbenchBrowser: (tabId, guestId, operationId) => ipcRenderer.invoke('workbench:browser:bind', tabId, guestId, operationId),
  workbenchDownloads: command => ipcRenderer.invoke('workbench:browser:downloads', command),
  onWorkbenchDownloads: callback => {
    const handler = (_event, rows) => callback(rows);
    ipcRenderer.on('workbench:browser:downloads', handler);
    return () => ipcRenderer.removeListener('workbench:browser:downloads', handler);
  },
  onNativeWindowClosed: (cb) => {
    const handler = (_event, id) => cb(String(id));
    ipcRenderer.on('workbench:window:closed', handler);
    return () => ipcRenderer.removeListener('workbench:window:closed', handler);
  },
  openMonitor: () => ipcRenderer.invoke('deck:openMonitor'),
  // Backend (Python FastAPI) connection details for the WS.
  getBackendInfo: () => ipcRenderer.invoke('backend:getInfo'),
  onBackendStatus: cb => {const listener=(_event,payload)=>cb(payload);ipcRenderer.on('backend:status',listener);return()=>ipcRenderer.removeListener('backend:status',listener);},

  // General settings persisted by the main process (settings.json).
  getSettings: () => ipcRenderer.invoke('settings:get'),
  getLaunchAtLogin: () => ipcRenderer.invoke('settings:getLaunchAtLogin'),
  setLaunchAtLogin: (on) => ipcRenderer.invoke('settings:setLaunchAtLogin', !!on),
  setStartHidden: (on) => ipcRenderer.invoke('settings:setStartHidden', !!on),

  // Native folder picker used by settings workflows.
  pickFolder: () => ipcRenderer.invoke('dialog:pickFolder'),
  // Electron 32+ removed File.path. Resolve a real disk-backed Web File inside
  // the isolated preload without exposing general filesystem APIs.
  getPathForFile: (file) => {
    try { return webUtils.getPathForFile(file); } catch (_) { return ''; }
  },

  // About: app version, key paths, and the update channel.
  getAppInfo: () => ipcRenderer.invoke('app:getInfo'),
  openAppPath: (key) => ipcRenderer.invoke('app:openPath', String(key || '')),
  openLocalPath: (localPath) => ipcRenderer.invoke('localPath:open', localPath),
  openExternal: (url) => ipcRenderer.invoke('external:open', String(url || '')),
  checkForUpdates: () => ipcRenderer.invoke('update:check'),

  // IDE-style workbench filesystem and Git surfaces.
  getWorkbenchRoot: () => ipcRenderer.invoke('workbench:root'),
  readWorkbenchDirectory: (localPath) => ipcRenderer.invoke('workbench:fs:readDir', localPath),
  readWorkbenchFile: (localPath) => ipcRenderer.invoke('workbench:fs:readFile', localPath),
  writeWorkbenchFile: (localPath, text, expectedMtimeMs) => (
    ipcRenderer.invoke('workbench:fs:writeFile', localPath, text, expectedMtimeMs)
  ),
  renameWorkbenchPath: (localPath, name) => ipcRenderer.invoke('workbench:fs:rename', localPath, name),
  trashWorkbenchPath: (localPath) => ipcRenderer.invoke('workbench:fs:trash', localPath),
  revealWorkbenchPath: (localPath) => ipcRenderer.invoke('workbench:fs:reveal', localPath),
  watchWorkbenchPath: (localPath, options) => ipcRenderer.invoke('workbench:fs:watch', localPath, options),
  stopWorkbenchWatch: (id) => ipcRenderer.invoke('workbench:fs:unwatch', id),
  onWorkbenchPathChanged: (cb) => {
    const listener = (_event, payload) => cb(payload || {});
    ipcRenderer.on('workbench:fs:changed', listener);
    return () => ipcRenderer.removeListener('workbench:fs:changed', listener);
  },
  getWorkbenchGitStatus: (localPath) => ipcRenderer.invoke('workbench:git:status', localPath),
  getWorkbenchGitDiff: (localPath, filePath, staged) => (
    ipcRenderer.invoke('workbench:git:diff', localPath, filePath, !!staged)
  ),
  runWorkbenchGit: (action, localPath, options) => (
    ipcRenderer.invoke('workbench:git:run', action, localPath, options || {})
  ),


  // Main Deck navigation and frameless window controls.
  onNavigate: (cb) =>
    ipcRenderer.on('deck:navigate', (_e, view) => cb(view)),
  onVoiceToggle: (cb) => {
    const listener = () => cb();
    ipcRenderer.on('voice:toggle', listener);
    return () => ipcRenderer.removeListener('voice:toggle', listener);
  },
  minimize: () => ipcRenderer.send('deck:minimize'),
  toggleMaximize: () => ipcRenderer.send('deck:toggleMaximize'),
  close: () => ipcRenderer.send('deck:close'),

  log: (msg) => ipcRenderer.send('log', String(msg)),
});
