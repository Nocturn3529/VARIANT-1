'use strict';

/**
 * System tray icon + context menu for VARIANT-1.
 */

const path = require('path');
const { Tray, Menu, nativeImage } = require('electron');

/**
 * @param {object} deps
 * @param {string} deps.appRoot
 * @param {() => void} deps.openDeckWindow
 * @param {() => void} deps.openMonitor
 * @param {() => void} deps.quitApp
 */
function createTray(deps) {
  const {
    appRoot,
    openDeckWindow,
    openMonitor,
    quitApp,
  } = deps;

  function buildTrayIcon() {
    const iconPath = path.join(appRoot, 'assets', 'tray', 'icon.png');
    let img = nativeImage.createFromPath(iconPath);
    if (img.isEmpty()) {
      img = nativeImage.createEmpty();
    } else {
      img = img.resize({ width: 18, height: 18 });
    }
    return img;
  }

  const tray = new Tray(buildTrayIcon());
  tray.setToolTip('VARIANT-1');

  const contextMenu = Menu.buildFromTemplate([
    {
      label: 'Open Main Deck',
      click: () => openDeckWindow('chat'),
    },
    {
      label: 'Settings…',
      click: () => openDeckWindow('settings'),
    },
    { type: 'separator' },
    {
      label: 'Live Logs…',
      click: () => openMonitor(),
    },
    { type: 'separator' },
    {
      label: 'Quit VARIANT-1',
      click: () => quitApp(),
    },
  ]);

  tray.setContextMenu(contextMenu);
  tray.on('click', () => openDeckWindow('chat'));
  return tray;
}

module.exports = { createTray };
