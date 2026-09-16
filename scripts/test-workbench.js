"use strict";
const path = require("node:path");
const Module = require("node:module");
const esbuild = require("esbuild");
const storage = new Map();
global.window = {
  location: {search: ""}, innerWidth: 1280, innerHeight: 720, setTimeout, clearTimeout,
  confirm: () => false,
  localStorage: {getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value)},
};
const result = esbuild.buildSync({
  entryPoints: [path.join(__dirname, "test-workbench-entry.ts")],
  bundle: true, platform: "node", format: "cjs", target: "node20", write: false, logLevel: "silent",
});
const compiled = new Module(path.join(__dirname, ".generated-test-workbench.cjs"), module);
compiled.filename = path.join(__dirname, ".generated-test-workbench.cjs");
compiled.paths = Module._nodeModulePaths(__dirname);
compiled._compile(result.outputFiles[0].text, compiled.filename);
Promise.resolve(compiled.exports.run()).catch(error => { console.error(error); process.exitCode = 1; });
