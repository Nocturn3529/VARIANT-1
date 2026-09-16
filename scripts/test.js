'use strict';

/** One backend owner and one frontend owner maintain their own test inventories. */
const {execFileSync} = require('node:child_process');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
for (const runner of ['test-python.js', 'test-frontend.js']) {
  execFileSync(process.execPath, [path.join(__dirname, runner)], {cwd: root, stdio: 'inherit'});
}
