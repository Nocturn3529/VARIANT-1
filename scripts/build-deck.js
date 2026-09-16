"use strict";

const fs = require("node:fs");
const path = require("node:path");
const esbuild = require("esbuild");

const root = path.resolve(__dirname, "..");
const deckRoot = path.join(root, "frontend", "main-deck");
const outdir = path.join(deckRoot, "dist");
const sharedOptions = {
  bundle: true,
  format: "esm",
  platform: "browser",
  target: ["chrome136"],
  outdir,
  entryNames: "[name]",
  assetNames: "[name]",
  loader: {".woff2": "file"},
  sourcemap: true,
  minify: true,
  logLevel: "info",
};
const platformOptions = {
  ...sharedOptions,
  entryPoints: {
    platform: path.join(root, "frontend", "main-deck", "src", "main.tsx"),
  },
  splitting: true,
  chunkNames: "chunks/[name]-[hash]",
};
const fixtureOptions = {
  ...sharedOptions,
  entryPoints: {
    fixture: path.join(root, "frontend", "main-deck", "src", "fixture.ts"),
  },
  splitting: false,
};

function cleanOutput() {
  const target = path.resolve(outdir);
  if (path.dirname(target) !== path.resolve(deckRoot)) {
    throw new Error(`Refusing to clean unexpected Deck output: ${target}`);
  }
  fs.rmSync(target, {recursive: true, force: true});
}

async function main() {
  cleanOutput();
  if (process.argv.includes("--watch")) {
    const contexts = await Promise.all([
      esbuild.context(platformOptions),
      esbuild.context(fixtureOptions),
    ]);
    await Promise.all(contexts.map(context => context.watch()));
    console.log("[deck] watching React/TypeScript sources");
    return;
  }
  await esbuild.build(platformOptions);
  await esbuild.build(fixtureOptions);
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
