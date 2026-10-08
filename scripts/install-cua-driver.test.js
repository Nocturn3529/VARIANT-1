'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {ASSETS, assetFor, VERSION} = require('./install-cua-driver');
const fs = require('node:fs/promises');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const {downloadPinnedAsset} = require('./download-pinned-asset');

async function fixture(t, handle) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'variant1-cua-download-'));
  const server = http.createServer(handle);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    const resolved = path.resolve(directory);
    assert.equal(path.dirname(resolved), path.resolve(os.tmpdir()));
    assert.ok(path.basename(resolved).startsWith('variant1-cua-download-'));
    await fs.rm(resolved, {recursive: true, force: true});
  });
  return {
    url: 'http://127.0.0.1:' + server.address().port,
    destination: path.join(directory, 'asset.zip'), directory,
    options: {get: http.get, baseDelayMs: 0, timeoutMs: 1000},
  };
}

test('pinned cua-driver covers the install platforms', () => {
  assert.match(VERSION, /^\d+\.\d+\.\d+$/);
  for (const key of ['win32-x64', 'win32-arm64', 'linux-x64', 'linux-arm64', 'darwin-x64', 'darwin-arm64']) {
    const asset = assetFor(...key.split('-'));
    assert.ok(asset, key);
    assert.equal(asset.sha256.length, 64);
  }
  assert.equal(assetFor('darwin', 'arm64').name, assetFor('darwin', 'x64').name);
  for (const asset of Object.values(ASSETS)) {
    assert.ok(asset.name.startsWith('cua-driver-rs-' + VERSION + '-'), asset.name);
  }
  assert.equal(assetFor('freebsd', 'x64'), null);
});

test('the weekly bump rewrites every pin and never downgrades on its own', async () => {
  const {PLATFORM_ASSETS, newer, rewrite, semver} = require('./bump-cua-driver');
  assert.deepEqual(Object.keys(PLATFORM_ASSETS).sort(), Object.keys(ASSETS).sort());
  const source = (await fs.readFile(path.join(__dirname, 'install-cua-driver.js'), 'utf8')).replace(/\r\n/g, '\n');
  const pins = {};
  for (const key of Object.keys(ASSETS)) pins[key] = {name: 'cua-driver-rs-9.9.9-' + key, sha256: 'a'.repeat(64)};
  const text = rewrite(source, '9.9.9', pins);
  assert.match(text, /^const VERSION = '9\.9\.9';$/m);
  assert.match(text, /^const TAG = 'cua-driver-rs-v9\.9\.9';$/m);
  assert.equal(text.split('a'.repeat(64)).length - 1, Object.keys(ASSETS).length);
  assert.equal(text.includes(VERSION), false);
  assert.equal(newer(semver('0.34.0'), semver('0.28.2')), true);
  assert.equal(newer(semver('0.28.2'), semver('0.28.2')), false);
  assert.equal(semver('0.34.0-rc.1'), null);
});

test('a timed-out partial download retries without publishing its bytes', async t => {
  let calls = 0;
  const f = await fixture(t, (_req, res) => {
    if (++calls === 1) { res.writeHead(200); res.write('partial'); return; }
    res.end('complete pinned archive');
  });
  await fs.writeFile(f.destination, 'old archive');
  const retries = [];
  await downloadPinnedAsset(f.url, f.destination, {
    ...f.options, timeoutMs: 75, onRetry: info => retries.push(info.error.code),
  });
  assert.equal(calls, 2);
  assert.deepEqual(retries, ['ETIMEDOUT']);
  assert.equal(await fs.readFile(f.destination, 'utf8'), 'complete pinned archive');
  assert.deepEqual(await fs.readdir(f.directory), ['asset.zip']);
});

test('relative redirects work and transient HTTP failure is bounded', async t => {
  let assetCalls = 0;
  const f = await fixture(t, (req, res) => {
    if (req.url === '/') { res.writeHead(302, {Location: '/asset'}); res.end(); return; }
    if (++assetCalls === 1) { res.writeHead(503, {'Retry-After': '0'}); res.end(); return; }
    res.end('verified bytes');
  });
  await downloadPinnedAsset(f.url, f.destination, f.options);
  assert.equal(assetCalls, 2);
  assert.equal(await fs.readFile(f.destination, 'utf8'), 'verified bytes');
});

test('permanent HTTP failure is not retried and leaves no partial file', async t => {
  let calls = 0;
  const f = await fixture(t, (_req, res) => { calls++; res.writeHead(404); res.end(); });
  await assert.rejects(downloadPinnedAsset(f.url, f.destination, f.options), /HTTP 404/);
  assert.equal(calls, 1);
  assert.deepEqual(await fs.readdir(f.directory), []);
});

test('redirect loops and excessive server waits fail instead of hanging setup', async t => {
  const loop = await fixture(t, (_req, res) => { res.writeHead(302, {Location: '/'}); res.end(); });
  await assert.rejects(downloadPinnedAsset(loop.url, loop.destination, {...loop.options, maxRedirects: 2}), /redirect limit/);
  const wait = await fixture(t, (_req, res) => { res.writeHead(429, {'Retry-After': '3600'}); res.end(); });
  await assert.rejects(downloadPinnedAsset(wait.url, wait.destination, wait.options), /HTTP 429/);
});

test('truncated responses exhaust a fixed attempt budget and clean staging', async t => {
  let calls = 0;
  const f = await fixture(t, (_req, res) => {
    calls++; res.writeHead(200, {'Content-Length': 100}); res.write('short');
    setImmediate(() => res.destroy());
  });
  await assert.rejects(downloadPinnedAsset(f.url, f.destination, {...f.options, attempts: 2}));
  assert.equal(calls, 2);
  assert.deepEqual(await fs.readdir(f.directory), []);
});
