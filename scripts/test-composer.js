'use strict';
const {spawnSync} = require('node:child_process');
const path = require('node:path');
const result = spawnSync(process.execPath, [path.join(__dirname, 'test-frontend-maintainability.js'), '--composer'], {stdio: 'inherit', windowsHide: true});
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
