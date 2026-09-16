'use strict';
const {execFileSync} = require('node:child_process');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
execFileSync(process.execPath, [require.resolve('typescript/bin/tsc'), '-p', path.join(__dirname, 'tsconfig.chat-contracts.json'), '--pretty', 'false'], {cwd: root, stdio: 'inherit'});
console.log('M10: valid chat commands compile; malformed calls fail at the actual sender boundary');
