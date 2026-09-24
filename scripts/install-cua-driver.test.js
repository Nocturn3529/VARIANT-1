'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {assetFor, VERSION} = require('./install-cua-driver');

test('pinned cua-driver covers the install platforms', () => {
  assert.equal(VERSION, '0.28.2');
  for (const key of ['win32-x64', 'win32-arm64', 'linux-x64', 'linux-arm64', 'darwin-x64', 'darwin-arm64']) {
    const asset = assetFor(...key.split('-'));
    assert.ok(asset, key);
    assert.equal(asset.sha256.length, 64);
  }
  assert.equal(assetFor('darwin', 'arm64').name, assetFor('darwin', 'x64').name);
  assert.equal(assetFor('freebsd', 'x64'), null);
});
