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
  metafile: true,
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
  const result = await esbuild.build(platformOptions);
  const packages = new Set();
  for (const input of Object.keys(result.metafile.inputs)) {
    if (!input.replaceAll('\\', '/').includes('node_modules/')) continue;
    let folder = path.dirname(path.resolve(root, input));
    while (folder !== root && path.dirname(folder) !== folder) {
      if (fs.existsSync(path.join(folder, 'package.json'))) {packages.add(folder); break;}
      folder = path.dirname(folder);
    }
  }
  const notices = ['Bundled renderer dependency notices. Each component retains its own license.'];
  for (const folder of [...packages].sort()) {
    const metadata = JSON.parse(fs.readFileSync(path.join(folder, 'package.json'), 'utf8'));
    const licenses = fs.readdirSync(folder).filter(name => /^(license|copying|notice)([.-]|$)/i.test(name));
    const supplied = path.join(root, 'assets/licenses', `${metadata.name.replace(/^@/, '').replaceAll('/', '-')}-${metadata.version}.txt`);
    if (!licenses.length && !fs.existsSync(supplied)) throw new Error(`Missing renderer dependency license: ${metadata.name}`);
    notices.push(`\n${metadata.name} ${metadata.version}\n${'-'.repeat(60)}`);
    for (const name of licenses) notices.push(fs.readFileSync(path.join(folder, name), 'utf8'));
    if (!licenses.length) notices.push(fs.readFileSync(supplied, 'utf8'));
  }
  fs.writeFileSync(path.join(outdir, 'THIRD_PARTY_LICENSES.txt'), notices.join('\n'), 'utf8');
  await esbuild.build(fixtureOptions);
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
