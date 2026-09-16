'use strict';

/**
 * Minimal bridge for the read-only Live2D overlay.
 * Main Deck has its own product/runtime bridge in deck-preload.js.
 */

const {contextBridge, ipcRenderer} = require('electron');

contextBridge.exposeInMainWorld('variant1', {
  dragStart: (mouseX, mouseY) => ipcRenderer.send('drag:start', {mouseX, mouseY}),
  dragMove: (mouseX, mouseY) => ipcRenderer.send('drag:move', {mouseX, mouseY}),
  dragEnd: () => ipcRenderer.send('drag:end'),
  setMouseIgnore: (ignore) => ipcRenderer.send('mouse:setIgnore', ignore),

  // Avatar size remains a userData setting even though product configuration
  // and onboarding belong exclusively to Main Deck.
  getSettings: () => ipcRenderer.invoke('avatar:settings:get'),

  log: (message) => ipcRenderer.send('log', String(message)),
  onActivityStatus: (listener) =>
    ipcRenderer.on('activity:status', (_event, payload) => {
      const status = payload && typeof payload.status === 'string'
        ? payload.status : 'starting';
      const rawInfo = payload && payload.info;
      const port = Number(rawInfo && rawInfo.port);
      const activityToken = rawInfo && rawInfo.activityToken != null
        ? String(rawInfo.activityToken) : '';
      listener({
        status,
        info: Number.isFinite(port) && activityToken
          ? {port, activityToken} : null,
      });
    }),
});
