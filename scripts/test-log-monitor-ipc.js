"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {createRequire} = require("node:module");
const root = path.resolve(__dirname, "..");
const file = path.join(root, "electron-deck-ipc.js");
const localRequire = createRequire(file);
const handlers = new Map();
const moduleResult = {exports: {}};
vm.runInNewContext(fs.readFileSync(file, "utf8"), {module: moduleResult, exports: moduleResult.exports,
  require: id => id === "electron" ? {ipcMain: {handle: (name, callback) => handlers.set(name, callback), on: () => {}}} : localRequire(id),
  __dirname: root, process, Buffer, console, setTimeout, clearTimeout}, {filename: file});
const deckWindow = {};
let opens = 0;
moduleResult.exports.registerDeckIpc({app: {}, appRoot: root,
  getDeckWindow: () => deckWindow, getMonitorWindow: () => null,
  isTrustedIpcSender: (event, window) => event.trusted === true && window === deckWindow,
  openMonitorWindow: () => { opens++; }});
const open = handlers.get("deck:openMonitor");
assert.equal(typeof open, "function");
assert.equal(open({trusted: false}).ok, false);
assert.equal(opens, 0, "untrusted callers cannot open privileged app windows");
assert.equal(open({trusted: true}).ok, true);
assert.equal(opens, 1, "the footer bridge delegates to the existing monitor window owner");
const main = fs.readFileSync(path.join(root, "main.js"), "utf8");
assert.match(main, /registerDeckIpc\(\{[\s\S]*getMonitorWindow:[\s\S]*openMonitorWindow,/,
  "the actual main process must provide the monitor opener to the IPC bridge");
console.log("log monitor IPC: trusted opening, rejected untrusted calls, and main-process wiring passed");
