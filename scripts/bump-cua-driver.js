'use strict';

/**
 * Pin the newest upstream cua-driver release in scripts/install-cua-driver.js.
 *
 * Usage: node scripts/bump-cua-driver.js [version]
 * Without a version, the newest cua-driver-rs-v* release of trycua/cua is used.
 * Digests come from GitHub's published asset digests (needs the gh CLI and a
 * token). Prints key=value lines (version, changed) for GitHub Actions.
 */
const fs = require('fs');
const path = require('path');
const {execFileSync} = require('child_process');

const REPO = 'trycua/cua';
const PREFIX = 'cua-driver-rs-v';
const installer = path.join(__dirname, 'install-cua-driver.js');
const PLATFORM_ASSETS = {
  'win32-x64': 'windows-x86_64-binary.zip',
  'win32-arm64': 'windows-arm64-binary.zip',
  'linux-x64': 'linux-x86_64-binary.tar.gz',
  'linux-arm64': 'linux-arm64-binary.tar.gz',
  // macOS ships the signed CuaDriver.app, which only this archive carries.
  'darwin-x64': 'darwin-universal.tar.gz',
  'darwin-arm64': 'darwin-universal.tar.gz',
};

function gh(args) {
  return JSON.parse(execFileSync('gh', ['api', ...args], {encoding: 'utf8', maxBuffer: 64 * 1024 * 1024}));
}

function semver(value) {
  const match = /^(\d+)\.(\d+)\.(\d+)$/.exec(value);
  return match ? match.slice(1).map(Number) : null;
}

function newer(a, b) {
  for (let i = 0; i < 3; i += 1) if (a[i] !== b[i]) return a[i] > b[i];
  return false;
}

function newestVersion() {
  let best = null;
  for (const page of [1, 2, 3]) {
    const rows = gh([`repos/${REPO}/releases?per_page=100&page=${page}`]);
    for (const row of rows) {
      if (row.draft || !String(row.tag_name).startsWith(PREFIX)) continue;
      const version = String(row.tag_name).slice(PREFIX.length);
      const parsed = semver(version);
      if (parsed && (!best || newer(parsed, best.parsed))) best = {version, parsed};
    }
    if (rows.length < 100) break;
  }
  if (!best) throw new Error('no cua-driver-rs release found');
  return best.version;
}

function pinsFor(version) {
  const release = gh([`repos/${REPO}/releases/tags/${PREFIX}${version}`]);
  const digests = new Map(release.assets.map(asset => [asset.name, String(asset.digest || '')]));
  const pins = {};
  for (const [key, suffix] of Object.entries(PLATFORM_ASSETS)) {
    const name = `cua-driver-rs-${version}-${suffix}`;
    const digest = digests.get(name) || '';
    if (!/^sha256:[0-9a-f]{64}$/.test(digest)) throw new Error(`${name} has no published sha256 digest`);
    pins[key] = {name, sha256: digest.slice('sha256:'.length)};
  }
  return pins;
}

function rewrite(source, version, pins) {
  let text = source
    .replace(/^const VERSION = '[^']+';$/m, `const VERSION = '${version}';`)
    .replace(/^const TAG = '[^']+';$/m, `const TAG = '${PREFIX}${version}';`);
  for (const [key, pin] of Object.entries(pins)) {
    const block = new RegExp(`('${key}': \\{\\n\\s+name: )'[^']+',(\\n\\s+sha256: )'[0-9a-f]{64}',`);
    if (!block.test(text)) throw new Error(`could not find the ${key} pin`);
    text = text.replace(block, `$1'${pin.name}',$2'${pin.sha256}',`);
  }
  return text;
}

function main() {
  const source = fs.readFileSync(installer, 'utf8').replace(/\r\n/g, '\n');
  const current = /^const VERSION = '([^']+)';$/m.exec(source)[1];
  const requested = process.argv[2] || '';
  const version = requested || newestVersion();
  if (!semver(version)) throw new Error(`not a release version: ${version}`);
  // Only an explicit version may pin an older release.
  if (version === current || (!requested && !newer(semver(version), semver(current)))) {
    console.log(`version=${version}`);
    console.log('changed=false');
    return;
  }
  fs.writeFileSync(installer, rewrite(source, version, pinsFor(version)));
  console.log(`version=${version}`);
  console.log('changed=true');
}

module.exports = {PLATFORM_ASSETS, newer, rewrite, semver};

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(error.message || error);
    process.exit(1);
  }
}
