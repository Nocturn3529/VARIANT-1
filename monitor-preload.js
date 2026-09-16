'use strict';

/**
 * VARIANT-1 — Live Logs pop-out preload bridge.
 *
 * Framed always-on-top window that streams the concise operational records in
 * logs/main0.log. Display + clear/copy only.
 */

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('variant1Monitor', {
  controlWindow: (action) => ipcRenderer.invoke('monitor:window', String(action)),
  /** Full session buffer snapshot (matches main0 content retained in memory). */
  getLogHistory: () => ipcRenderer.invoke('logs:getHistory'),

  /** Clear session log view + main0; returns the new banner lines. */
  clearLogs: () => ipcRenderer.invoke('logs:clear'),

  /** Copy the full live buffer to the system clipboard (main process). */
  copyLogs: () => ipcRenderer.invoke('logs:copy'),

  /** Open the logs directory in the OS file manager. */
  openLogFolder: () => ipcRenderer.invoke('logs:openFolder'),

  /** Live line push from main (after history load). */
  onLogLine: (cb) => {
    const handler = (_e, line) => {
      if (typeof cb === 'function' && line != null) cb(String(line));
    };
    ipcRenderer.on('logs:line', handler);
    return () => ipcRenderer.removeListener('logs:line', handler);
  },

  close: () => ipcRenderer.send('monitor:close'),
});
