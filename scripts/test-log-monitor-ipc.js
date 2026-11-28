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
const listeners = new Map();
const moduleResult = {exports: {}};
vm.runInNewContext(fs.readFileSync(file, "utf8"), {module: moduleResult, exports: moduleResult.exports,
  require: id => id === "electron" ? {ipcMain: {handle: (name, callback) => handlers.set(name, callback), on: (name, callback) => listeners.set(name, callback)}} : localRequire(id),
  __dirname: root, process, Buffer, console, setTimeout, clearTimeout}, {filename: file});
function testWindow(page) {
  const win = {closed:false, isDestroyed: () => win.closed};
  win.webContents = {mainFrame:{url:"variant1://app/" + page}, isDestroyed: () => win.closed};
  return win;
}
const from = win => ({sender:win.webContents, senderFrame:win.webContents.mainFrame});
const deck = testWindow("frontend/main-deck/index.html");
const monitor = testWindow("frontend/monitor.html");
let deckWindow = deck, monitorWindow = monitor;
const roles = {getDeckWindow: () => deckWindow, getMonitorWindow: () => monitorWindow};
const {isTrustedIpcSender} = require("../electron-app-boot").createWindowSecurity(roles);
const logLines = [];
let opens = 0;
moduleResult.exports.registerDeckIpc({app: {}, appRoot: root,
  ...roles, isTrustedIpcSender, log: value => logLines.push(value), getLogBuffer: () => ["fixture entry"],
  openMonitorWindow: () => { opens++; }});
const open = handlers.get("deck:openMonitor");
const history = handlers.get("logs:getHistory");
const log = listeners.get("log");
assert.equal(open(from(deck)).ok, true);
assert.deepEqual(history(from(monitor)), ["fixture entry"]);
assert.equal(open(from(monitor)).ok, false);
assert.equal(history(from(deck)).length, 0);
log(from(deck), "Deck"); log(from(monitor), "Monitor");

const subframe = {...from(deck), senderFrame:{url: deck.webContents.mainFrame.url}};
assert.equal(open(subframe).ok, false);
assert.equal(open({sender:deck.webContents, senderFrame:null}).ok, false);
log(subframe, "frame");
const other = testWindow("frontend/main-deck/index.html");
assert.equal(isTrustedIpcSender(from(other), other), false);
log(from(other), "unknown window");
const originalUrl = deck.webContents.mainFrame.url;
deck.webContents.mainFrame.url = "about:blank";
assert.equal(open(from(deck)).ok, false);
deck.webContents.mainFrame.url = originalUrl;

deckWindow = null;
assert.equal(open(from(monitor)).ok, false);
assert.equal(isTrustedIpcSender(from(monitor)), false, "shared calls must name their allowed roles");
log(from(monitor), "Monitor alone");
deckWindow = deck; monitorWindow = null;
assert.equal(history(from(deck)).length, 0);
deck.closed = true;
assert.equal(open(from(deck)).ok, false);
log(from(deck), "closed");
assert.equal(opens, 1);
assert.deepEqual(logLines, ["[renderer] Deck", "[renderer] Monitor", "[renderer] Monitor alone"]);
const main = fs.readFileSync(path.join(root, "main.js"), "utf8");
assert.match(main, /registerDeckIpc\(\{[\s\S]*getMonitorWindow:[\s\S]*openMonitorWindow,/,
  "the actual main process must provide the monitor opener to the IPC bridge");
console.log("Deck and Monitor IPC: exact live window, main frame, named shared logging and main-process wiring passed");
