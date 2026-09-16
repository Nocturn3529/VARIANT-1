"use strict";

const path = require("node:path");
const Module = require("node:module");
const esbuild = require("esbuild");

const root = path.join(__dirname, "..");
// A few chat ingest dependencies read initial responsive navigation state at
// module load. Keep the runtime suite DOM-free while supplying that boundary.
global.window = {
  location: {search: ""},
  innerWidth: 1440,
  setTimeout,
  clearTimeout,
};
const result = esbuild.buildSync({
  entryPoints: [path.join(__dirname, "test-deck-runtime-entry.ts")],
  bundle: true,
  platform: "node",
  format: "cjs",
  target: ["node20"],
  write: false,
  logLevel: "silent",
});

const compiled = new Module(
  path.join(__dirname, ".generated-test-deck-runtime.cjs"),
  module,
);
compiled.filename = path.join(__dirname, ".generated-test-deck-runtime.cjs");
compiled.paths = Module._nodeModulePaths(__dirname);
compiled._compile(result.outputFiles[0].text, compiled.filename);

Promise.resolve(compiled.exports.run()).catch(error => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
