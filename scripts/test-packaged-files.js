'use strict';

/**
 * Packaged Electron must ship every main-process module main.js requires.
 * Regression for the package.json allowlist that only listed electron-security.js
 * (and omitted electron-backend, tray, overlay, …).
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {execFileSync} = require('child_process');

const root = path.join(__dirname, '..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');

const mainJs = read('main.js');
const backendLauncher = read('electron-backend.js');
const backendSpec = read('backend/variant1_backend.spec');
const backendSetup = read('scripts/setup-backend.js');
const backendBuild = read('scripts/build-backend.js');
const browserProvisioning = read('backend/browser_fabric/provisioning.py');
const speechAssets = read('backend/speech/assets.py');
const appBoot = read('electron-app-boot.js');
const deckBuild = path.join(root, 'scripts', 'build-deck.js');
const packageJson = JSON.parse(read('package.json'));
const defaultLlmConfig = JSON.parse(read('config/llm_config.default.json'));
const files = packageJson.build && packageJson.build.files;
assert.ok(Array.isArray(files), 'package.json build.files must be an array');
assert.strictEqual(packageJson.name, 'variant1');
assert.strictEqual(packageJson.build.productName, 'VARIANT-1');
assert.strictEqual(packageJson.build.appId, 'app.variant1.desktop');
assert.strictEqual(read('backend/server.py').match(/^VERSION = "([^"]+)"/m)?.[1], packageJson.version,
  'the backend handshake version must match the Electron release version');
assert.strictEqual(Object.hasOwn(packageJson, 'author'), false,
  'a company/author identity must not be invented for VARIANT-1');
assert.match(String(packageJson.build.linux && packageJson.build.linux.maintainer || ''),
  /^[^<]+ <[^@\s]+@[^>\s]+>$/,
  'deb packages need a maintainer without adding package.json author');
assert.match(mainJs, /app\.setName\(['"]VARIANT-1['"]\)/,
  'Electron must establish the VARIANT-1 name before deriving userData');

// Glob that covers every local electron-*.js module.
assert.ok(
  files.some(entry => entry === 'electron-*.js' || entry === 'electron-*.js/**'),
  'package.json build.files must include electron-*.js (packaged installs need all main modules)',
);

// Parse require('./electron-…') from main.js
const requireRe = /require\(\s*['"]\.\/(electron-[a-z0-9-]+)['"]\s*\)/g;
const requiredModules = new Set();
let match;
while ((match = requireRe.exec(mainJs)) !== null) {
  requiredModules.add(match[1] + '.js');
}

assert.ok(requiredModules.size >= 8, 'main.js should require the electron-* module set');

for (const file of requiredModules) {
  const abs = path.join(root, file);
  assert.ok(fs.existsSync(abs), `main.js requires ./${file} but file is missing`);
}

// Also require that preloads + main entry are allowlisted.
for (const entry of [
  'main.js', 'deck-preload.js', 'monitor-preload.js',
  'deck-routes.json', 'THIRD_PARTY_NOTICES.md',
]) {
  assert.ok(
    files.includes(entry),
    `package.json build.files must include ${entry}`,
  );
}

// Frontend + assets stay in the package (Main Deck lives under frontend/).
assert.ok(files.some(e => e === 'frontend/**/*' || e.startsWith('frontend/')),
  'package.json build.files must include frontend/**/*');
for (const fixture of [
  '!frontend/main-deck/src/**/*',
  '!frontend/main-deck/dev/**/*',
  '!frontend/main-deck/tsconfig.json',
  '!frontend/main-deck/dist/*.map',
  '!frontend/main-deck/dist/**/*.map',
  '!frontend/main-deck/dist/fixture.js',
  '!frontend/main-deck/dist/fixture.js.map',
]) {
  assert.ok(files.includes(fixture),
    `package.json build.files must exclude non-runtime ${fixture.slice(1)}`);
}
assert.ok(files.some(e => e === 'assets/**/*' || e.startsWith('assets/')),
  'package.json build.files must include assets/**/*');
assert.ok(!fs.existsSync(path.join(root, 'assets', 'unused-assets', 'overview-widgets')),
  'retired standalone Overview sources must not remain in the repository');

// Bundled config is an explicit allowlist. Development config also contains
// personal memory, chat-derived timelines, usage telemetry,
// and encrypted credentials; a broad "**/*" filter would silently put those
// files into every installer built from a developer workstation.
const configResource = (packageJson.build.extraResources || []).find(
  entry => entry && entry.from === 'config' && entry.to === 'config',
);
assert.ok(configResource, 'package.json must define the bundled config resource');
assert.ok(Array.isArray(configResource.filter),
  'bundled config resource must use an explicit filter');
assert.ok(!configResource.filter.includes('**/*'),
  'bundled config must not copy the whole development config directory');
for (const required of [
  'llm_config.default.json',
  'tools.default.json',
  'messaging.default.json',
  'prompts/**/*',
]) {
  assert.ok(configResource.filter.includes(required),
    `bundled config must include ${required}`);
}
for (const privateFile of [
  'llm_config.json',
  'settings.json',
  'tools.json',
  'user_profile.json',
  'timeline.jsonl',
  'cloud_usage.json',
  'automations.json',
  'automation_suggestions.json',
  'skill_usage.json',
  'curator_state.json',
]) {
  assert.ok(!configResource.filter.includes(privateFile),
    `bundled config must not include private runtime file ${privateFile}`);
}

for (const omittedRuntime of [
  'backend/.playwright-browsers',
  'models/base/whisper-small.bin',
  'models/base/kokoro',
]) {
  assert.ok(!(packageJson.build.extraResources || []).some(
    entry => entry && entry.from === omittedRuntime,
  ), `thin installer must not bundle ${omittedRuntime}`);
}
assert.match(backendLauncher, /PLAYWRIGHT_BROWSERS_PATH/,
  'Electron must publish the user-owned Playwright runtime path');
assert.match(backendLauncher, /path\.join\(getDataDir\(\),\s*'runtimes',\s*'playwright'\)/,
  'packaged Playwright must live under writable user data');
assert.match(backendSetup, /playwright['"],\s*['"]install['"],\s*['"]chromium['"]/,
  'backend setup must populate the staged Playwright Chromium runtime');
assert.match(backendSetup, /PLAYWRIGHT_BROWSERS_PATH:\s*playwrightBrowsersDir/,
  'development setup must retain its deterministic browser cache');
assert.doesNotMatch(backendBuild, /containsPlaywrightBrowser|playwrightBrowsersDir/,
  'backend freezing must not require a browser payload');
assert.match(browserProvisioning, /"install",\s*\n\s*"chromium"/,
  'managed Browser Fabric must provision Chromium on first use');
assert.match(browserProvisioning, /VARIANT1_DATA_DIR[\s\S]*"runtimes",\s*"playwright"/,
  'first-use Chromium must remain in user data');
assert.doesNotMatch(backendSetup, /kokoro-v1\.0\.onnx|voices-v1\.0\.bin|thewh1teagle/,
  'backend setup must not download speech model weights');
assert.match(speechAssets, /"models"\s*\/\s*"speech"\s*\/\s*"whisper"/,
  'Whisper discovery must use the user speech folder');
assert.match(speechAssets, /"models"\s*\/\s*"speech"\s*\/\s*"kokoro"/,
  'Kokoro discovery must use the user speech folder');
assert.match(appBoot, /VARIANT-1 user-supplied speech models/,
  'fresh user data must explain the speech drop contract');
assert.strictEqual(
  defaultLlmConfig.voice.binary,
  'models/speech/whisper/whisper-server',
  'fresh installs must resolve Whisper from the user drop folder (platform-neutral basename)',
);
assert.strictEqual(
  defaultLlmConfig.local.binary,
  'bin/llama-server',
  'fresh installs must pin llama-server without a Windows-only .exe suffix',
);

function effectiveExtraResources(platform) {
  const build = packageJson.build || {};
  const shared = Array.isArray(build.extraResources) ? build.extraResources : [];
  const plat = build[platform] && Array.isArray(build[platform].extraResources)
    ? build[platform].extraResources
    : [];
  return shared.concat(plat);
}

function binResource(platform) {
  return effectiveExtraResources(platform).find(
    entry => entry && entry.from === 'bin' && entry.to === 'bin',
  );
}

const kernelRuntime = effectiveExtraResources('win').find(
  entry => entry && entry.from === 'backend/dist/Variant1Kernel'
    && entry.to === 'backend/kernel',
) || (packageJson.build.extraResources || []).find(
  entry => entry && entry.from === 'backend/dist/Variant1Kernel'
    && entry.to === 'backend/kernel',
);
assert.ok(kernelRuntime,
  'the one-directory kernel runtime must remain isolated under backend/kernel');

const WINDOWS_NATIVE_FILTER = [
  'llama-server.exe',
  'llama-server-impl.dll',
  'llama-common.dll',
  'llama.dll',
  'mtmd.dll',
  'ggml.dll',
  'ggml-base.dll',
  'ggml-cpu-*.dll',
  'ggml-cuda.dll',
  'ggml-rpc.dll',
  'libomp140.x86_64.dll',
  'cua-driver/cua-driver.exe',
  'cua-driver/VERSION',
  'cublas64_13.dll',
  'cublasLt64_13.dll',
  'cudart64_13.dll',
];

const winNative = binResource('win');
assert.ok(winNative && Array.isArray(winNative.filter),
  'Windows native runtime packaging must use an explicit filter');
assert.deepStrictEqual(winNative.filter, WINDOWS_NATIVE_FILTER,
  'installer Windows native-runtime manifest changed without updating its contract');
assert.ok(!winNative.filter.some(item => item.includes('**') || item.includes('whisper')),
  'Windows native runtime manifest must not broaden or rebundle user-supplied Whisper');
for (const excludedTool of [
  'llama-cli.exe', 'llama-bench.exe', 'llama-quantize.exe',
  'llama-perplexity.exe', 'ggml-rpc-server.exe', 'whisper/whisper-server.exe',
]) {
  assert.ok(!winNative.filter.includes(excludedTool),
    `installer must not retain non-runtime tool ${excludedTool}`);
}

for (const platform of ['linux', 'mac']) {
  const unixNative = binResource(platform);
  assert.ok(unixNative && Array.isArray(unixNative.filter),
    `${platform} native runtime packaging must use an explicit filter`);
  assert.ok(unixNative.filter.includes('llama-server'),
    `${platform} native filter must include llama-server`);
  assert.ok(unixNative.filter.includes('cua-driver/cua-driver'),
    `${platform} native filter must include the pinned cua-driver binary`);
  assert.ok(unixNative.filter.includes('cua-driver/VERSION'),
    `${platform} native filter must include the pinned cua-driver version`);
  assert.ok(!unixNative.filter.includes('llama-server.exe'),
    `${platform} native filter must not require Windows llama-server.exe`);
  assert.ok(!unixNative.filter.some(item => String(item).endsWith('.dll')),
    `${platform} native filter must not ship Windows DLLs`);
  assert.ok(!unixNative.filter.some(item => item.includes('**') || item.includes('whisper')),
    `${platform} native runtime must not broaden or rebundle Whisper`);
}

// Shared config/backend resources remain available on every platform.
for (const platform of ['win', 'linux', 'mac']) {
  const extras = effectiveExtraResources(platform);
  assert.ok(extras.some(e => e && e.from === 'config' && e.to === 'config'),
    `${platform} must still bundle the allowlisted config resource`);
  assert.ok(extras.some(e => e && e.from === 'backend/dist/Variant1Backend' && e.to === 'backend'),
    `${platform} must still bundle the frozen backend`);
}

// A PyInstaller windowed process sets stdout/stderr to None on Windows. Uvicorn
// configures its default formatter against stdout before publishing the backend
// handshake, so the frozen backend must retain console streams while Electron
// remains responsible for hiding the process window.
assert.match(backendSpec, /\bconsole\s*=\s*True\b/,
  'frozen backend must retain stdout/stderr for Uvicorn and host diagnostics');
assert.doesNotMatch(backendSpec, /\bconsole\s*=\s*False\b/,
  'frozen backend must not use PyInstaller windowed stream suppression');
assert.match(backendLauncher, /windowsHide\s*:\s*true/,
  'Electron must hide the console-capable frozen backend process');

// `backend/memory.py` was retired with the ChromaDB memory layer. Keeping its
// hidden import can silently resurrect that package and its heavy transitive
// dependency tree when an old source/build artifact is present.
assert.doesNotMatch(backendSpec, /["']memory["']/,
  'the frozen backend must not collect the retired memory module');
assert.doesNotMatch(backendSpec, /["']kernel_runtime\.capsule_worker["']/,
  'the backend bundle must not hidden-import worker-only capsule codecs');
assert.doesNotMatch(backendSpec, /\bcollect_all\b/,
  'the frozen backend must not use package-wide collect_all()');
for (const optional of ['kokoro_onnx', 'phonemizer', 'espeakng_loader', 'onnxruntime']) {
  assert.ok(backendSpec.includes('"' + optional + '"'), 'speech exclusion missing: ' + optional);
  assert.ok(!backendSpec.includes('copy_metadata("' + optional), 'speech metadata must not ship');
}
assert.doesNotMatch(backendSpec, /ESPEAK_EN_US_ASSETS|collect_data_files\("kokoro/, 'offline speech payload must not be frozen');
for (const workerOnly of [
  'IPython', 'ipykernel', 'jupyter_client', 'jupyter_core', 'zmq', 'debugpy',
  'traitlets', 'tornado', 'comm', 'prompt_toolkit', 'jedi', 'parso',
  'duckdb', 'matplotlib',
  'pandas', 'plotly', 'pyarrow', 'safetensors', 'scipy',
]) {
  assert.match(backendSpec, new RegExp(`["']${workerOnly}["']`),
    `${workerOnly} must be excluded from Variant1Backend`);
}

// esbuild does not remove outputs for deleted/renamed entry points. Prove the
// production builder replaces the generated directory instead of packaging a
// stale bundle left by an earlier source layout.
const deckDist = path.join(root, 'frontend', 'main-deck', 'dist');
const staleDeckOutput = path.join(deckDist, '__stale-build-output.js');
fs.mkdirSync(deckDist, {recursive: true});
fs.writeFileSync(staleDeckOutput, 'throw new Error("stale");\n', 'utf8');
execFileSync(process.execPath, [deckBuild], {cwd: root, stdio: 'pipe'});
const staleDeckOutputSurvived = fs.existsSync(staleDeckOutput);
fs.rmSync(staleDeckOutput, {force: true});
assert.strictEqual(staleDeckOutputSurvived, false,
  'Deck builds must remove stale generated outputs before bundling');

console.log(`packaged files: ${requiredModules.size} electron modules + allowlist ok`);
