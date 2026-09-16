'use strict';
const path = require('node:path'), {execFileSync} = require('node:child_process');
const root = path.resolve(__dirname, '..');
if (process.platform !== 'win32' || process.arch !== 'x64') throw new Error('This native release recipe targets Windows x64.');
execFileSync(path.join(root, 'backend/.venv/Scripts/python.exe'),
  [path.join(__dirname, 'prepare-native-runtime.py'), ...process.argv.slice(2)],
  {cwd: root, stdio: 'inherit', windowsHide: true});
