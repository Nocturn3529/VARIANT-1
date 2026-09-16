'use strict';

/**
 * Static cross-language smoke test for the Deck boundary.
 *
 * This intentionally does not import Python. It checks that literal commands
 * sent by production frontend source have registered @on(...) handlers and
 * pins the single-root/domain-routing architecture.
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');

function walk(dir, accept) {
  const out = [];
  for (const entry of fs.readdirSync(dir, {withFileTypes: true})) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walk(full, accept));
    else if (accept(full)) out.push(full);
  }
  return out;
}

const frontendFiles = [
  ...walk(path.join(root, 'frontend', 'main-deck', 'src'),
    file => /\.(?:ts|tsx)$/.test(file)),
].filter(file => !file.includes(`${path.sep}react${path.sep}`));

const outbound = new Set();
const literalSend = /\b(?:send[A-Z]\w*|send|st|context\.send)\s*\(\s*\{\s*type\s*:\s*["']([^"']+)["']/g;
for (const file of frontendFiles) {
  const source = fs.readFileSync(file, 'utf8');
  let match;
  while ((match = literalSend.exec(source)) !== null) outbound.add(match[1]);
}

const hydration = read(path.join(
  'frontend', 'main-deck', 'src', 'runtime', 'initialHydration.ts',
));
const startup = hydration.match(/INITIAL_DECK_COMMANDS\s*=\s*\[([^\]]+)\]/m);
assert.ok(startup, 'typed runtime startup command burst must remain statically visible');
for (const match of startup[1].matchAll(/["']([^"']+)["']/g)) outbound.add(match[1]);

const backendFiles = [
  ...walk(path.join(root, 'backend'),
    file => /[\\/]ws_[^\\/]+\.py$/.test(file)),
  path.join(root, 'backend', 'ws_dispatch.py'),
];
const handlers = new Set();
for (const file of backendFiles) {
  const source = fs.readFileSync(file, 'utf8');
  for (const decorator of source.matchAll(/^\s*@on\(([\s\S]*?)\)\s*$/gm)) {
    for (const type of decorator[1].matchAll(/["']([^"']+)["']/g)) {
      handlers.add(type[1]);
    }
  }
}

const missing = [...outbound].filter(type => !handlers.has(type)).sort();
assert.deepStrictEqual(missing, [],
  `frontend literal commands missing backend handlers: ${missing.join(', ')}`);

const main = read(path.join('frontend', 'main-deck', 'src', 'main.tsx'));
const deckApp = read(path.join('frontend', 'main-deck', 'src', 'DeckApp.tsx'));
const workbench = read(path.join(
  'frontend', 'main-deck', 'src', 'workbench', 'Workbench.tsx',
));
const browserBridge = read(path.join(
  'frontend', 'main-deck', 'src', 'workbench', 'browserBridge.ts',
));
const deckBuild = read(path.join('scripts', 'build-deck.js'));
const browserHost = read(path.join(
  'frontend', 'main-deck', 'src', 'workbench', 'browserHostBridge.ts',
));
assert.match(browserHost, /runWorkbenchBrowserCommand/,
  'the ASTB browser host must target the visible workbench guest');
for (const action of ['new_page', 'activate_page', 'close_page']) {
  assert.ok(browserHost.includes(`action === "${action}"`),
    `the host must expose browser ${action}`);
}
assert.match(browserBridge, /sendInputEvent[\s\S]*captureWorkbenchPreview/,
  'visible browser actions and screenshots must share the Chromium guest');
assert.strictEqual((main.match(/createRoot\(/g) || []).length, 1,
  'Main Deck must have one React root');
assert.match(main, /createRoot\(reactRoot\)\.render\([\s\S]*<DeckApp api=\{api\}\/>/,
  'the renderer entry owns one Deck shell; native panels reuse its state');
assert.doesNotMatch(main, /BrowserPopoutApp|browserPopout/,
  'the retired parallel browser renderer must not return');
assert.doesNotMatch(main, /createPortal\(/,
  'the application shell must not use portal layout slots');
assert.match(main, /REACT_MODULE_MESSAGE_TYPES\[id\]/,
  'React runtime registrations must publish their inbound type filters');
assert.match(main, /new DeckRuntime/,
  'React platform must own the typed DeckRuntime');
assert.match(deckBuild, /splitting:\s*true/,
  'the production Deck entry must enable ESM code splitting');
assert.match(deckBuild, /chunkNames:\s*["']chunks\/\[name\]-\[hash\]["']/,
  'Deck chunks must use a deterministic bounded output directory');
assert.match(deckBuild, /esbuild\.build\(platformOptions\)[\s\S]*esbuild\.build\(fixtureOptions\)/,
  'production and development fixture entries must build separately');
for (const group of ['destinations', 'settings']) {
  assert.match(deckApp, new RegExp(`lazy\\(\\(\\) => import\\(["']\\./deferred/${group}["']\\)`),
    `DeckApp must lazy-load the ${group} group`);
}
assert.match(fs.readFileSync(path.join(root,"frontend/main-deck/src/context/TerminalPanel.tsx"),"utf8"), /lazy\(\(\) => import\(["']\.\.\/workbench\/TerminalSurface["']\)/,
  'the large xterm renderer must remain outside the first Chat bundle');

// First-party CSS has one cascade owner. Component-local imports previously
// duplicated artifacts/fabric/extensions styles into deferred CSS bundles.
for (const file of frontendFiles) {
  if (file === path.join(root, 'frontend', 'main-deck', 'src', 'main.tsx')) continue;
  assert.doesNotMatch(fs.readFileSync(file, 'utf8'),
    /import\s+["']\.\.?\/[^"']+\.css["']/,
    `${path.relative(root, file)} must not bypass styles/index.css`);
}

const electronWindows = read('electron-app-windows.js');
const appStore = read(path.join(
  'frontend', 'main-deck', 'src', 'state', 'appStore.ts',
));
const deckRoutes = JSON.parse(read('deck-routes.json'));
assert.deepStrictEqual(deckRoutes.primary,
  ['chat', 'memory', 'automations', 'overview', 'runtime', 'settings']);
assert.match(electronWindows, /const DECK_VIEWS = new Set\(DECK_ROUTES\.primary\)/,
  'Electron must consume the shared primary route authority');
assert.match(appStore, /new Set<PrimaryView>\(PRIMARY_VIEWS\)/,
  'React must consume the shared primary route authority');

const html = read(path.join('frontend', 'main-deck', 'index.html'));
assert.ok(!fs.existsSync(path.join(root, 'frontend', 'chat-prototype')),
  'the retired chat-prototype ownership root must stay removed');
assert.ok(!fs.existsSync(path.join(root, 'frontend', 'widget-prototypes')),
  'live Overview dashboards must stay inside the Main Deck root');
assert.match(html, /src="\.\/dist\/platform\.js"/,
  'the shell must load the generated Main Deck entry');
assert.match(main, /import "\.\/styles\/index\.css"/,
  'all authored Deck CSS must enter through one source cascade');
assert.doesNotMatch(html, /src="\.\/(?:app|runtime(?!-lib)(?:-[a-z-]+)?)\.js"/,
  'production must not load the retired vanilla application runtime');
assert.match(
  read(path.join('frontend', 'main-deck', 'src', 'runtime', 'DeckRuntime.ts')),
  /module\.messageTypes\.includes\(type\)/,
  'typed runtime fan-out must use domain message filters',
);
assert.doesNotMatch(
  html,
  /runtime-turn\.js/,
  'production must not load the retired vanilla turn controller',
);
assert.match(main, /turnController/,
  'the React platform must install the typed turn authority');
for (const globalName of [
  'variant1Runtime', 'Variant1Turn', 'variant1Chat', 'variant1ContextTabs',
  'variant1DeckRuntime',
]) {
  assert.doesNotMatch(main, new RegExp(globalName),
    `Main Deck must not publish window.${globalName}`);
}

assert.match(
  read(path.join('frontend', 'main-deck', 'src', 'protocol.ts')),
  /"react-runtime-mic":\s*\["transcript"\]/,
  'typed mic runtime must declare its inbound transcript message',
);

assert.doesNotMatch(html, /<script\s+src="\.\/fixtures-(?:seed|handlers)\.js"/,
  'production HTML must not request fixture payloads');

const deckDist = path.join(root, 'frontend', 'main-deck', 'dist');
const platformBundle = path.join(deckDist, 'platform.js');
const fixtureBundle = path.join(deckDist, 'fixture.js');
const chunkDir = path.join(deckDist, 'chunks');
assert.ok(fs.existsSync(platformBundle), 'the production Deck bundle must exist');
assert.ok(fs.existsSync(chunkDir), 'the production Deck chunk directory must exist');

const staticImport = /(?:^|[;}])import(?:[^('";]*?from)?["']([^"']+\.js)["']/g;
const dynamicImport = /\bimport\(["']([^"']+\.js)["']\)/g;
function imports(file, pattern) {
  const source = fs.readFileSync(file, 'utf8');
  return [...source.matchAll(pattern)].map(match => path.resolve(path.dirname(file), match[1]));
}
function reachable(start, includeDynamic) {
  const found = new Set();
  const visit = file => {
    if (found.has(file)) return;
    assert.ok(file.startsWith(deckDist + path.sep) || file === platformBundle,
      `Deck bundle import escaped dist: ${file}`);
    assert.ok(fs.existsSync(file), `Deck bundle import is missing: ${file}`);
    found.add(file);
    for (const child of imports(file, staticImport)) visit(child);
    if (includeDynamic) for (const child of imports(file, dynamicImport)) visit(child);
  };
  visit(start);
  return found;
}

const eagerFiles = reachable(platformBundle, false);
const eagerBytes = [...eagerFiles].reduce((total, file) => total + fs.statSync(file).size, 0);
assert.ok(fs.statSync(platformBundle).size <= 600 * 1024,
  `Deck entry regressed above 600 KiB: ${fs.statSync(platformBundle).size}`);
assert.ok(eagerFiles.size <= 8,
  `Deck startup regressed above 8 eager JS files: ${eagerFiles.size}`);
assert.ok(eagerBytes <= 780 * 1024,
  `Deck startup regressed above 780 KiB: ${eagerBytes}`);

const allReachable = reachable(platformBundle, true);
const chunkFiles = walk(chunkDir, file => file.endsWith('.js'));
assert.ok(chunkFiles.length >= 3 && chunkFiles.length <= 14,
  `expected 3-14 bounded Deck chunks, found ${chunkFiles.length}`);
const artChunks = chunkFiles.filter(file => /^p5Runtime-/.test(path.basename(file)));
assert.equal(artChunks.length, 1, 'one optional p5 renderer must be packaged');
assert.ok(!eagerFiles.has(artChunks[0]), 'generative art must not load on Deck startup');
assert.ok(fs.statSync(artChunks[0]).size <= 580 * 1024, 'optional p5 renderer exceeded 580 KiB');
assert.deepStrictEqual(
  chunkFiles.filter(file => !allReachable.has(file)).map(file => path.basename(file)),
  [],
  'every packaged Deck chunk must be reachable from the production entry',
);
assert.strictEqual(walk(chunkDir, file => file.endsWith('.css')).length, 0,
  'component CSS must stay in the single platform.css cascade');
assert.doesNotMatch(fs.readFileSync(fixtureBundle, 'utf8'), /["']\.\/chunks\//,
  'the excluded development fixture must not own packaged production chunks');

console.log(
  `deck architecture: ${outbound.size} commands / ${handlers.size} handlers; ` +
  `${eagerFiles.size} eager files / ${(eagerBytes / 1024).toFixed(1)} KiB`,
);
