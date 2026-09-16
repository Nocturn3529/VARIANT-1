'use strict';

/**
 * Freeze the Python backend into a self-contained executable with PyInstaller,
 * so a packaged VARIANT-1 install needs NO Python on the target machine.
 *
 *   npm run build:backend
 *
 * Output: backend/dist/Variant1Backend/Variant1Backend.exe (+ _internal/) and
 * backend/dist/Variant1Kernel/Variant1Kernel.exe (+ _internal/).
 * The electron-builder step (package.json `extraResources`) copies that folder
 * into <resources>/backend/, where main.js launches Variant1Backend.exe.
 *
 * Uses the project venv's interpreter (created by `npm run setup:backend`) so
 * PyInstaller sees exactly the runtime deps the app uses. Installs PyInstaller
 * into that venv on first run if it's missing.
 */

const { execFileSync } = require('child_process');
const path = require('path');
const fs = require('fs');

const isWin = process.platform === 'win32';
const root = path.join(__dirname, '..');
const backendDir = path.join(root, 'backend');
const venvPy = isWin
  ? path.join(backendDir, '.venv', 'Scripts', 'python.exe')
  : path.join(backendDir, '.venv', 'bin', 'python');
const py = venvPy;
const PYINSTALLER_VERSION = '6.20.0';

function run(file, args, cwd) {
  console.log('> ' + file + ' ' + args.join(' '));
  execFileSync(file, args, { stdio: 'inherit', cwd: cwd || root });
}

try {
  if (!fs.existsSync(venvPy)) {
    throw new Error('backend/.venv is required; run `npm run setup:backend` first');
  }
  // The bootloader and hook set affect the executable, so do not silently
  // accept whichever PyInstaller happens to be installed on the build host.
  try {
    execFileSync(py, ['-c',
      `import PyInstaller; assert PyInstaller.__version__ == '${PYINSTALLER_VERSION}', PyInstaller.__version__`],
    { stdio: 'ignore' });
  } catch (_) {
    console.log(`Installing pinned PyInstaller ${PYINSTALLER_VERSION} into the build interpreter…`);
    run(py, ['-m', 'pip', 'install', '-r',
      path.join(backendDir, 'requirements-build.lock')]);
  }

  // Clean previous output so stale files never ship.
  for (const d of ['build', 'dist']) {
    const p = path.join(backendDir, d);
    try { fs.rmSync(p, { recursive: true, force: true }); } catch (_) {}
  }

  // Freeze both independently audited entry points. Run from backend/ so each
  // spec's pathex='.' resolves to the modules.
  run(py, ['-m', 'PyInstaller', '--noconfirm', '--clean', 'variant1_backend.spec'], backendDir);
  run(py, ['-m', 'PyInstaller', '--noconfirm', '--clean', 'variant1_kernel.spec'], backendDir);

  const exe = path.join(backendDir, 'dist', 'Variant1Backend',
    isWin ? 'Variant1Backend.exe' : 'Variant1Backend');
  if (!fs.existsSync(exe)) {
    throw new Error('expected output missing: ' + exe);
  }
  const kernelExe = path.join(backendDir, 'dist', 'Variant1Kernel',
    isWin ? 'Variant1Kernel.exe' : 'Variant1Kernel');
  if (!fs.existsSync(kernelExe)) {
    throw new Error('expected kernel output missing: ' + kernelExe);
  }
  console.log('\nBackend frozen: ' + exe);
  console.log('Kernel frozen: ' + kernelExe);
  console.log('Now run `npm run dist` to build the installer (it bundles this exe).');
} catch (err) {
  console.error('\nBackend freeze failed:', err.message);
  console.error('Tips: run `npm run setup:backend` first; if the backend then fails to start '
    + 'in a packaged build, set console=True in backend/variant1_backend.spec to see the traceback.');
  process.exit(1);
}
