'use strict';

/**
 * Run the complete default Python suite.
 *
 *   npm run test:python
 *   npm run test:python -- tests/test_tools_safety.py -vv
 *   npm run test:python:desktop   // also runs real Windows app scenarios
 */

const {execFileSync, spawnSync} = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const {makeTreeWritable, removeTreeWithRetry} = require('./test-isolation-cleanup');

const root = path.join(__dirname, '..');
const backend = path.join(root, 'backend');
const isWin = process.platform === 'win32';
const python = isWin
  ? path.join(backend, '.venv', 'Scripts', 'python.exe')
  : path.join(backend, '.venv', 'bin', 'python');

if (!fs.existsSync(python)) {
  console.error('Backend venv not found. Run: npm run setup:backend');
  process.exit(1);
}

const cleanupTests = spawnSync(
  process.execPath,
  ['--test', path.join(__dirname, 'test-isolation-cleanup.test.js')],
  {cwd: root, stdio: 'inherit'},
);
if ((cleanupTests.status || 0) !== 0) {
  process.exit(cleanupTests.status || 1);
}

const userArgs = process.argv.slice(2);
const desktopIndex = userArgs.indexOf('--desktop');
const runDesktop = desktopIndex >= 0;
if (runDesktop) userArgs.splice(desktopIndex, 1);

// Keep generated repositories outside the workspace in VARIANT-1's explicitly
// ignored test root. Desktop Git/indexing clients otherwise traverse and lock
// pytest worktrees while the runner is deleting them.
const testTempParent = path.join(os.homedir(), 'variant1-test-tmp');
fs.mkdirSync(testTempParent, {recursive: true});
const tempBase = fs.mkdtempSync(path.join(testTempParent, 'variant-1-pytest-'));
const tempRoot = path.join(tempBase, `runner-${process.pid}`);
const tempDir = path.join(tempRoot, 'tmp');
const baseTemp = path.join(tempRoot, 'pytest');
const testDataDir = path.join(tempRoot, 'variant1-data');
const testConfigDir = path.join(testDataDir, 'config');
fs.mkdirSync(tempDir, {recursive: true});
fs.mkdirSync(baseTemp, {recursive: true});
fs.mkdirSync(testConfigDir, {recursive: true});

// Importing server.py constructs VARIANT-1's durable service graph.  The test
// interpreter must therefore receive its isolated data/config roots before
// pytest imports any test module or conftest dependency.  TEMP/--basetemp alone
// do not redirect catalog, conversation, Work, extension, or snapshot stores.
const pathOverrideVars = [
  'VARIANT1_AGENT_SNAPSHOT_DB',
  'VARIANT1_AGENT_SNAPSHOT_DIR',
  'VARIANT1_AUTOMATIONS',
  'VARIANT1_BROWSER_FABRIC_DB',
  'VARIANT1_BROWSER_PROFILE_ROOT',
  'VARIANT1_CODING_DB',
  'VARIANT1_CODING_WORKTREE_ROOT',
  'VARIANT1_CONVERSATION_DB',
  'VARIANT1_DESKTOP_FABRIC_DB',
  'VARIANT1_EXECUTION_DB',
  'VARIANT1_MESSAGING_CONFIG',
  'VARIANT1_PATCH_JOURNAL_DIR',
  'VARIANT1_TOOLS_CONFIG',
  'VARIANT1_TRACE_PATH',
  'VARIANT1_WORK_DB',
  'VARIANT1_WORKSPACE_DB',
];

function runtimeTreeInventory(base) {
  const rows = new Map();
  if (!fs.existsSync(base)) return rows;
  const visit = (current) => {
    for (const entry of fs.readdirSync(current, {withFileTypes: true})) {
      const full = path.join(current, entry.name);
      const relative = path.relative(base, full).split(path.sep).join('/');
      const stat = fs.lstatSync(full);
      rows.set(relative, `${entry.isDirectory() ? 'd' : entry.isSymbolicLink() ? 'l' : 'f'}:${stat.size}:${stat.mtimeMs}`);
      if (entry.isDirectory() && !entry.isSymbolicLink()) visit(full);
    }
  };
  visit(base);
  return rows;
}

const protectedRoots = [
  path.join(root, 'data'),
  path.join(backend, 'data'),
  path.join(root, 'config'),
];

function protectedRuntimeInventory() {
  const rows = new Map();
  for (const base of protectedRoots) {
    const label = path.relative(root, base).split(path.sep).join('/') || '.';
    for (const [name, value] of runtimeTreeInventory(base)) {
      rows.set(`${label}/${name}`, value);
    }
  }
  return rows;
}

function inventoryChanges(before, after) {
  const changed = [];
  const names = new Set([...before.keys(), ...after.keys()]);
  for (const name of [...names].sort()) {
    if (before.get(name) !== after.get(name)) changed.push(name);
  }
  return changed;
}



function describePathChain(target, isolatedRoot) {
  const lines = [];
  let current = target;
  const seen = new Set();
  while (current && !seen.has(current)) {
    seen.add(current);
    try {
      const st = fs.lstatSync(current);
      const mode = (st.mode & 0o7777).toString(8).padStart(4, '0');
      let extra = '';
      if (st.isSymbolicLink()) {
        const dest = fs.readlinkSync(current);
        extra = ` -> ${dest}`;
      }
      lines.push(
        `  ${st.isDirectory() ? 'd' : st.isSymbolicLink() ? 'l' : 'f'} ${mode} uid=${st.uid} gid=${st.gid} nlink=${st.nlink} ${current}${extra}`
      );
    } catch (error) {
      lines.push(`  missing ${current}: ${error.code || ''} ${error.message}`);
    }
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
    if (isolatedRoot && current.length < isolatedRoot.length) break;
  }
  return lines;
}

function describeIsolationFailure(error, tempRoot, chmodErrors) {
  const lines = [
    `TEST ISOLATION FAILURE: could not remove pytest temp directory: ${error.message}`,
  ];
  if (typeof process.getuid === 'function') {
    lines.push(`euid=${process.getuid()} egid=${process.getgid()} cwd=${process.cwd()}`);
  }
  lines.push(`platform=${process.platform} fs=${os.type()} tmp=${tempRoot}`);
  const failedPath = error.path || '';
  if (failedPath) {
    lines.push('path chain (lstat, isolated root only):');
    lines.push(...describePathChain(failedPath, tempRoot));
  }
  if (chmodErrors.length) {
    lines.push('chmod errors during makeTreeWritable:');
    for (const item of chmodErrors.slice(0, 40)) lines.push(`  ${item}`);
  }
  try {
    const mnt = spawnSync('findmnt', ['-T', failedPath || tempRoot, '-o', 'TARGET,FSTYPE,OPTIONS', '-n'], {
      encoding: 'utf8',
      timeout: 2000,
    });
    if (mnt.status === 0 && mnt.stdout.trim()) lines.push(`mount: ${mnt.stdout.trim()}`);
  } catch (_) {}
  try {
    const lsof = spawnSync('lsof', ['-n', failedPath || tempRoot], {
      encoding: 'utf8',
      timeout: 2000,
    });
    if (lsof.stdout && lsof.stdout.trim()) {
      lines.push('lsof:');
      lines.push(...lsof.stdout.trim().split(/\r?\n/).slice(0, 20).map((row) => `  ${row}`));
    }
  } catch (_) {}
  return lines.join('\n');
}

const pytestArgs = userArgs.length ? userArgs : ['tests', '-q'];
const hasOption = name => pytestArgs.some(
  arg => arg === name || arg.startsWith(`${name}=`),
);
if (!hasOption('-p')) pytestArgs.push('-p', 'no:cacheprovider');
if (!hasOption('--basetemp')) pytestArgs.push('--basetemp', baseTemp);

const env = Object.assign({}, process.env);
for (const name of pathOverrideVars) delete env[name];
const testSecretstoreKey = path.join(tempRoot, 'secretstore.key');
Object.assign(env, {
  TMP: tempDir,
  TEMP: tempDir,
  VARIANT1_TEST_RUNTIME_ROOT: tempRoot,
  VARIANT1_DATA_DIR: testDataDir,
  VARIANT1_CONFIG: testConfigDir,
  VARIANT1_LLM_CONFIG: path.join(root, 'config', 'llm_config.default.json'),
  // R2.c: isolate Fernet key material inside the runner temp tree.
  VARIANT1_SECRETSTORE_KEY: testSecretstoreKey,
});
if (runDesktop) env.VARIANT1_DESKTOP_INTEGRATION = '1';

const protectedBefore = protectedRuntimeInventory();

try {
  execFileSync(python, ['-m', 'pytest', ...pytestArgs], {
    stdio: 'inherit',
    cwd: backend,
    env,
  });
} catch (error) {
  if (typeof error.status === 'number') {
    process.exitCode = error.status;
  } else {
    throw error;
  }
} finally {
  try {
    // Git deliberately writes read-only object files on Windows. Test-created
    // repositories must not strand runner directories after an interrupted or
    // failed suite.
    if (isWin && fs.existsSync(tempRoot)) {
      try {
        execFileSync('attrib.exe', ['-R', path.join(tempRoot, '*'), '/S', '/D'], {
          stdio: 'ignore',
          windowsHide: true,
        });
      } catch (_) {}
    }
    const chmodErrors = makeTreeWritable(tempRoot);
    try {
      removeTreeWithRetry(tempRoot);
      removeTreeWithRetry(tempBase);
    } catch (cleanupError) {
      console.error(describeIsolationFailure(cleanupError, tempRoot, chmodErrors));
      process.exitCode = 1;
    }
  } catch (error) {
    console.error(describeIsolationFailure(error, tempRoot, []));
    process.exitCode = 1;
  }
}

const protectedAfter = protectedRuntimeInventory();
const leakedWrites = inventoryChanges(protectedBefore, protectedAfter);
if (leakedWrites.length) {
  console.error('\nTEST ISOLATION FAILURE: pytest changed VARIANT-1 development runtime state.');
  for (const name of leakedWrites.slice(0, 40)) {
    console.error(`  ${name}`);
  }
  if (leakedWrites.length > 40) {
    console.error(`  ...and ${leakedWrites.length - 40} more path(s)`);
  }
  process.exitCode = 1;
}
