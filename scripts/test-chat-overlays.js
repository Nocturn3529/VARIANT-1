"use strict";
const assert = require("node:assert/strict");
const path = require("node:path");
const Module = require("node:module");
const esbuild = require("esbuild");
const storage = new Map();
global.window = {location: {search: ""}, innerWidth: 1280, innerHeight: 720, setTimeout, clearTimeout,
  localStorage: {getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value)}};
global.CSS = {escape: value => value};
global.document = {querySelector: selector => ({getBoundingClientRect: () => selector === ".workbench"
  ? {left: 0, top: 32, right: 1280, bottom: 692, width: 1280, height: 660}
  : {left: 1020, top: 32, right: 1280, bottom: 692, width: 260, height: 660}})};
const result = esbuild.buildSync({entryPoints: [path.join(__dirname, "test-chat-overlays-entry.ts")], bundle: true,
  platform: "node", format: "cjs", target: "node20", write: false, logLevel: "silent"});
const compiled = new Module(path.join(__dirname, ".generated-chat-overlays.cjs"), module);
compiled.filename = path.join(__dirname, ".generated-chat-overlays.cjs");
compiled.paths = Module._nodeModulePaths(__dirname);
compiled._compile(result.outputFiles[0].text, compiled.filename);

// The app state is decided when its module loads, so a deep link needs a
// fresh copy of the store evaluated under that URL.
function storeAt(search) {
  window.location.search = search;
  const build = esbuild.buildSync({stdin: {contents: 'export {getAppState} from "../frontend/main-deck/src/state/appStore";',
    resolveDir: __dirname, loader: "ts"}, bundle: true, platform: "node", format: "cjs", target: "node20", write: false, logLevel: "silent"});
  const route = new Module(path.join(__dirname, ".generated-deep-link.cjs"), module);
  route.filename = path.join(__dirname, ".generated-deep-link.cjs");
  route.paths = Module._nodeModulePaths(__dirname);
  route._compile(build.outputFiles[0].text, route.filename);
  window.location.search = "";
  return route.exports.getAppState();
}
assert.equal(storeAt("?view=runtime").view, "overview", "a legacy runtime link opens Overview › Python, not the removed runtime page");
const memoryLink = storeAt("?view=memory");
assert.deepEqual([memoryLink.view, memoryLink.settingsCategory], ["settings", "session-context"]);
assert.equal(storeAt("?view=nonsense").view, "chat");
console.log("deep links: legacy runtime and memory routes open their current homes");
