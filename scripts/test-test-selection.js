'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const calls = [];
vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'test.js'), 'utf8'), {
  __dirname, process,
  require: id => id === 'node:child_process' ? {execFileSync: (_exe, args) => calls.push(path.basename(args[0]))} : require(id),
});
assert.deepEqual(calls, ['test-python.js', 'test-frontend.js']);
// Scope to the frontend-scripts job body only. Taking everything after the
// 'frontend-scripts:' key also swept in every later job, so a later job that
// legitimately runs a native smoke script (macos-qualify) tripped an
// assertion that only governs this job's delegation.
const ciText = fs.readFileSync(path.join(root, '.github/workflows/ci.yml'), 'utf8');
const [, afterFrontendScripts = ''] = ciText.split(/^ {2}frontend-scripts:$/m);
const frontendScriptsJob = afterFrontendScripts.split(/^ {2}[a-z0-9_-]+:$/m)[0];
assert.ok(frontendScriptsJob, 'frontend-scripts job not found in ci.yml');
assert.match(frontendScriptsJob, /run: npm run test:frontend/);
assert.doesNotMatch(frontendScriptsJob, /run: node scripts\/test-/);
assert.equal(require('../package.json').scripts['test:frontend'], 'node scripts/test-frontend.js');
assert.match(require('../package.json').scripts['test:deck:e2e'], /test-native-popouts-electron/);
console.log('M06: npm test and CI delegate to the maintained frontend runner; desktop tests stay selectable');
