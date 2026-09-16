'use strict';

/**
 * Remove generated development artifacts that are safe to recreate.
 *
 * This intentionally does not remove user data, logs, models, node_modules, or
 * the backend virtualenv. It is meant for trimming stale build/cached output
 * from a working tree before packaging, sharing, or archiving.
 */

const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const packageOnly = process.argv.includes('--package-only');
const pythonCacheOnly = process.argv.includes('--python-cache-only');
const resetExtensions = process.argv.includes('--reset-extensions');
const packagedOutput = path.join(root, 'dist');
const explicitTargets = [
  packagedOutput,
  path.join(root, 'backend', 'build'),
  path.join(root, 'backend', 'dist'),
  path.join(root, 'frontend', 'main-deck', 'dist'),
  path.join(root, 'backend', '.pytest_cache'),
  // Removed product feature: safe derived desktop index cache, never source text.
  path.join(root, 'data', 'desktop_index'),
];

function isInsideRoot(target) {
  const rel = path.relative(root, target);
  return rel && !rel.startsWith('..') && !path.isAbsolute(rel);
}

function removePath(target) {
  const resolved = path.resolve(target);
  if (!isInsideRoot(resolved)) {
    throw new Error('refusing to remove outside workspace: ' + resolved);
  }
  if (!fs.existsSync(resolved)) return 0;
  fs.rmSync(resolved, { recursive: true, force: true });
  console.log('removed ' + path.relative(root, resolved));
  return 1;
}

function walk(dir, out) {
  if (!fs.existsSync(dir)) return;
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (full.includes(path.join('backend', '.venv') + path.sep)) continue;
    if (entry.isDirectory()) {
      if (entry.name === '__pycache__' || entry.name === '.pytest_cache') {
        out.push(full);
      } else {
        walk(full, out);
      }
    } else if (/\.py[co]$/.test(entry.name)) {
      out.push(full);
    }
  }
}

const generatedTargets = [];
if (!packageOnly) {
  for (const dir of ['backend', 'config', 'sprite-test']) {
    walk(path.join(root, dir), generatedTargets);
  }
}

let count = 0;
const targets = packageOnly
  ? [packagedOutput]
  : pythonCacheOnly
    ? generatedTargets
    : [
    ...explicitTargets,
    ...generatedTargets,
    // Explicit clean-break/dev recovery only. Plugin source remains under
    // config/plugins and is re-imported on the next startup.
    ...(resetExtensions ? [path.join(root, 'data', 'extensions')] : []),
    ];
for (const target of targets) {
  count += removePath(target);
}

console.log(count ? `cleaned ${count} generated artifact paths.` : 'no generated artifacts found.');
