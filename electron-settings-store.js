'use strict';

/**
 * Overlay/settings JSON persistence for the Electron main process.
 */

const path = require('path');
const fs = require('fs');
const {randomUUID} = require('node:crypto');

/**
 * @param {object} opts
 * @param {string|(() => string)} opts.configPath fixed path or live getter
 * @param {(msg: string) => void} [opts.log]
 */
function createSettingsStore({ configPath, log }) {
  const cfg = () => (typeof configPath === 'function' ? configPath() : configPath);

  function readSettings() {
    try {
      const raw = fs.readFileSync(cfg(), 'utf-8');
      const settings = JSON.parse(raw);
      if (settings && settings.general && typeof settings.general === 'object') {
        delete settings.general.projectRoot;
      }
      return settings;
    } catch (err) {
      return {
        avatar: { size: 160, position: null },
        voice: { muted: false },
        general: { autoStart: false },
      };
    }
  }

  function writeSettings(settings) {
    const configPathNow = cfg();
    const temporary = `${configPathNow}.${randomUUID()}.tmp`;
    let fd;
    let created = false;
    try {
      fs.mkdirSync(path.dirname(configPathNow), { recursive: true });
      const contents = JSON.stringify(settings, null, 2);
      fd = fs.openSync(temporary, 'wx', 0o600);
      created = true;
      fs.writeFileSync(fd, contents, 'utf-8');
      fs.fsyncSync(fd);
      fs.closeSync(fd); fd = undefined;
      fs.renameSync(temporary, configPathNow);
      created = false;
    } catch (err) {
      if (typeof log === 'function') log('Failed to write settings: ' + err.message);
      throw err;
    } finally {
      if (fd !== undefined) { try { fs.closeSync(fd); } catch (_) { /* retain original failure */ } }
      if (created) { try { fs.unlinkSync(temporary); } catch (_) { /* retain original failure */ } }
    }
  }

  return { readSettings, writeSettings };
}

module.exports = { createSettingsStore };
