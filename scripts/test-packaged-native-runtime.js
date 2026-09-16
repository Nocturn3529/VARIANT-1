'use strict';

/** Validate and execute the native runtime from a freshly unpacked installer. */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {spawnSync, execFileSync} = require('child_process');

const root = path.join(__dirname, '..');
const runtime = path.join(root, 'dist', 'win-unpacked', 'resources', 'bin');
assert.ok(fs.existsSync(runtime), `missing packaged native runtime: ${runtime}`);

const expectedExact = [
  'llama-server.exe', 'llama-server-impl.dll', 'llama-common.dll',
  'llama.dll', 'mtmd.dll', 'ggml.dll', 'ggml-base.dll',
  'ggml-cuda.dll', 'ggml-rpc.dll', 'libomp140.x86_64.dll',
  'cublas64_13.dll', 'cublasLt64_13.dll', 'cudart64_13.dll',
];
for (const name of expectedExact) {
  assert.ok(fs.existsSync(path.join(runtime, name)), `missing native runtime file ${name}`);
}
const cpuBackends = fs.readdirSync(runtime).filter(name => /^ggml-cpu-.+\.dll$/i.test(name));
assert.strictEqual(cpuBackends.length, 14, 'packaged runtime must retain all CPU dispatch backends');

const packagedFiles = fs.readdirSync(runtime, {withFileTypes: true});
assert.strictEqual(packagedFiles.filter(entry => entry.isDirectory()).length, 0,
  'native runtime must not retain tool/demo subdirectories');
assert.strictEqual(packagedFiles.filter(entry => entry.isFile()).length, 27,
  'native runtime manifest must stay at 27 files');
for (const forbidden of [
  'llama-cli.exe', 'llama-bench.exe', 'llama-quantize.exe',
  'llama-perplexity.exe', 'ggml-rpc-server.exe',
]) {
  assert.ok(!fs.existsSync(path.join(runtime, forbidden)),
    `packaged runtime retained ${forbidden}`);
}

const server = path.join(runtime, 'llama-server.exe');
function runServer(args) {
  const result = spawnSync(server, args, {
    cwd: runtime, encoding: 'utf8', windowsHide: true,
  });
  assert.strictEqual(result.status, 0,
    `packaged llama-server ${args.join(' ')} failed: ${result.error || result.stderr}`);
  return String(result.stdout || '') + String(result.stderr || '');
}
const version = runServer(['--version']);
assert.match(version, /version:/i, 'packaged llama-server did not load its DLL closure');
const devices = runServer(['--list-devices']);
assert.match(devices, /CUDA0:/, 'packaged llama-server did not load the CUDA backend');
execFileSync(path.join(root, 'backend/.venv/Scripts/python.exe'),
  [path.join(root, 'scripts/test-native-matrix.py'), '--runtime', runtime],
  {cwd: root, stdio: 'inherit', windowsHide: true});

const bytes = fs.readdirSync(runtime).reduce(
  (total, name) => total + fs.statSync(path.join(runtime, name)).size,
  0,
);
console.log(
  `packaged native runtime: 27 files / ${(bytes / 1024 / 1024).toFixed(2)} MiB, ` +
  'llama-server DLL closure + CUDA device ok',
);
