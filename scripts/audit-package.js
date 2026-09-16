'use strict';
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), crypto = require('node:crypto');
const asar = require('@electron/asar');
const root = path.resolve(__dirname, '..');
const folder = path.resolve(process.argv[2] || path.join(root, 'dist/win-unpacked'));
const archive = path.join(folder, 'resources/app.asar');
const entries = asar.listPackage(archive).map(name => name.replaceAll('\\', '/'));
const forbiddenAsar = entries.filter(name => /^\/(artifacts|data|logs|attachments)\//.test(name)
  || /\/main-deck\/(src|dev)\//.test(name) || /\.(map|d\.ts)$/.test(name)
  || /node_modules\/(react|react-dom|p5|@xterm)(\/|$)/.test(name)
  || /live2d|blackcat|electron-overlay|frontend\/renderer\.js/.test(name));
assert.deepEqual(forbiddenAsar, [], 'private/development/retired files in app.asar');
assert.ok(entries.includes('/LICENSE') && entries.some(name => name.endsWith('/THIRD_PARTY_LICENSES.txt')),
  'application and renderer dependency notices must ship');
const runtime = JSON.parse(fs.readFileSync(path.join(root, 'config/native-runtime.json'), 'utf8'));
for (const row of runtime.files) {
  const actual = fs.readFileSync(path.join(folder, 'resources/bin', row.file));
  assert.equal(crypto.createHash('sha256').update(actual).digest('hex'), row.sha256, row.file);
}
const files = [];
function walk(dir) {
  for (const item of fs.readdirSync(dir, {withFileTypes: true})) {
    const file = path.join(dir, item.name);
    if (item.isDirectory()) walk(file);
    else if (item.isFile()) files.push({path: path.relative(folder, file).replaceAll('\\', '/'), bytes: fs.statSync(file).size});
  }
}
walk(folder);
const forbiddenResources = files.filter(row => /\/(?:kokoro_onnx|phonemizer|espeakng_loader|onnxruntime|neutts|kittentts|piper|soundfile)(?:\/|[._-])/.test(row.path)
  || row.path.includes('/pyarrow/tests/') || /\.(gguf|onnx|sqlite3|jsonl|part|pdb)$/.test(row.path));
assert.deepEqual(forbiddenResources, [], 'offline speech, private data or test payload leaked into package');
for (const name of ['resources/backend/Variant1Backend.exe', 'resources/backend/kernel/Variant1Kernel.exe',
  'resources/backend/_internal/THIRD_PARTY_LICENSES.txt']) assert.ok(files.some(row => row.path === name), name);
const configFiles = fs.readdirSync(path.join(folder, 'resources/config'));
assert.ok(!configFiles.some(name => ['llm_config.json', 'settings.json', 'tools.json', 'messaging.json', 'plugins'].includes(name)));
const defaults = JSON.parse(fs.readFileSync(path.join(folder, 'resources/config/llm_config.default.json'), 'utf8'));
assert.equal(defaults.local.model, '');
assert.equal(defaults.voice.auto_tts, false);
console.log(JSON.stringify({passed: true, asar_entries: entries.length, resource_files: files.length,
  bytes: files.reduce((sum, row) => sum + row.bytes, 0), native_files_verified: runtime.files.length,
  largest_files: [...files].sort((a, b) => b.bytes - a.bytes).slice(0, 12)}, null, 2));
