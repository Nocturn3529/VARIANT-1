'use strict';

/**
 * Creates a Python virtual environment at backend/.venv and installs the
 * backend dependencies into it. main.js automatically prefers this venv's
 * interpreter when it exists, so after running this once `npm start` will
 * launch the backend with no further setup.
 *
 *   npm run setup:backend
 *
 * Notes:
 *  - All pip calls go through `<venv python> -m pip` (NOT pip.exe). On Windows,
 *    running pip.exe to upgrade itself fails because the executable is locked;
 *    `python -m pip` is the supported way.
 *  - The pip self-upgrade is best-effort and never aborts the dependency install.
 *  - execFileSync with argument arrays avoids any shell-quoting issues.
 */

const { execFileSync } = require('child_process');
const path = require('path');
const fs = require('fs');

const isWin = process.platform === 'win32';
const root = path.join(__dirname, '..');
const venvRel = path.join('backend', '.venv');
const venvDir = path.join(root, venvRel);
const sysPy = isWin ? 'python' : 'python3';
const venvPy = isWin
  ? path.join(venvDir, 'Scripts', 'python.exe')
  : path.join(venvDir, 'bin', 'python');

function run(file, args, options = {}) {
  console.log('> ' + file + ' ' + args.join(' '));
  execFileSync(file, args, {
    stdio: 'inherit',
    cwd: root,
    env: options.env || process.env,
  });
}

const playwrightBrowsersDir = path.join(root, 'backend', '.playwright-browsers');

try {
  if (!fs.existsSync(venvPy)) {
    run(sysPy, ['-m', 'venv', venvRel]);
  } else {
    console.log('venv already exists, reusing it.');
  }

  // Best-effort pip upgrade; must not abort setup if it fails.
  try {
    run(venvPy, ['-m', 'pip', 'install', '--upgrade', 'pip']);
  } catch (e) {
    console.warn('pip self-upgrade skipped (continuing): ' + e.message);
  }

  // Prefer the pinned lock on Windows for reproducible installs. The checked-in
  // requirements.lock is win32-targeted (pywin32/uiautomation); on Linux/macOS
  // use the platform-marked requirements.txt until a Unix lock exists.
  const lockFile = path.join('backend', 'requirements.lock');
  const reqFile = path.join('backend', 'requirements.txt');
  const lockPath = path.join(root, lockFile);
  const reqPath = path.join(root, reqFile);
  let depsFile;
  if (isWin && fs.existsSync(lockPath)) {
    depsFile = lockFile;
  } else if (fs.existsSync(reqPath)) {
    if (!isWin && fs.existsSync(lockPath)) {
      console.log('Using requirements.txt on ' + process.platform +
        ' (requirements.lock is win32-targeted).');
    }
    depsFile = reqFile;
  } else if (fs.existsSync(lockPath)) {
    depsFile = lockFile;
  } else {
    throw new Error('missing backend/requirements.txt and backend/requirements.lock');
  }
  console.log('installing from ' + depsFile);
  run(venvPy, ['-m', 'pip', 'install', '-r', depsFile]);

  // Browser automation v1 uses Playwright-managed isolated Chromium. The Python
  // package does not include browser binaries, so fetch Chromium once during
  // setup. Keep it in a deterministic project-owned staging directory so the
  // installer can bundle the exact runtime instead of depending on the build
  // user's global Playwright cache.
  try {
    fs.mkdirSync(playwrightBrowsersDir, {recursive: true});
    run(venvPy, ['-m', 'playwright', 'install', 'chromium'], {
      env: {
        ...process.env,
        PLAYWRIGHT_BROWSERS_PATH: playwrightBrowsersDir,
      },
    });
  } catch (e) {
    console.warn('Playwright Chromium install skipped (' + e.message +
      '). Development browser automation stays unavailable until you rerun ' +
      '`npm run setup:backend`; packaged VARIANT-1 provisions it on first use.');
  }

  console.log('\nBackend ready. Speech model weights are user-supplied under models/speech/.');
  console.log('Run `npm start`.');
} catch (err) {
  console.error('\nBackend setup failed:', err.message);
  console.error('Make sure CPython 3.13 x64 is installed and on your PATH.');
  process.exit(1);
}
