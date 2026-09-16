'use strict';
const {spawnSync} = require('node:child_process');
const path = require('node:path');
const result = spawnSync(process.execPath, [path.join(__dirname, 'test-deck-electron-smoke.js')], {
  env: {...process.env, VARIANT1_TEST_NATIVE_POPOUTS: '1'}, stdio: 'inherit', windowsHide: true,
});
if (result.error) console.error(result.error);
process.exitCode = result.status ?? 1;
