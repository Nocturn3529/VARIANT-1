"use strict";
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
