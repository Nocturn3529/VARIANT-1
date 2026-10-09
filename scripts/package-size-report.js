'use strict';

/**
 * Print where an unpacked VARIANT-1 package spends its bytes, on any platform.
 *
 *   node scripts/package-size-report.js [unpacked-dir-or-.app]
 *
 * Without an argument it finds dist/win-unpacked, dist/linux-unpacked or the
 * mac .app. It prints the installers in dist/, the biggest areas of the
 * package (Electron, the frozen backend and its largest Python packages,
 * the desktop driver, the Deck) and the largest single files, so size changes
 * between platforms and releases can be explained. It never fails a build.
 */
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const dist = path.join(root, 'dist');
const MiB = 1024 * 1024;

function findUnpacked() {
  for (const name of ['win-unpacked', 'linux-unpacked']) {
    const candidate = path.join(dist, name);
    if (fs.existsSync(candidate)) return candidate;
  }
  for (const entry of fs.existsSync(dist) ? fs.readdirSync(dist) : []) {
    const app = path.join(dist, entry, 'VARIANT-1.app');
    if (entry.startsWith('mac') && fs.existsSync(app)) return app;
  }
  return '';
}

function walk(directory, rows) {
  for (const entry of fs.readdirSync(directory, {withFileTypes: true})) {
    const full = path.join(directory, entry.name);
    if (entry.isSymbolicLink()) continue;
    if (entry.isDirectory()) walk(full, rows);
    else if (entry.isFile()) rows.push({file: full, bytes: fs.statSync(full).size});
  }
  return rows;
}

function mib(bytes) {
  return (bytes / MiB).toFixed(1).padStart(8) + ' MiB';
}

function bucket(relative) {
  const parts = relative.split('/');
  const backend = parts.indexOf('backend');
  if (parts.includes('resources') && backend >= 0) {
    const rest = parts.slice(backend + 1);
    if (rest[0] === '_internal' && rest.length > 2) return 'backend/_internal/' + rest[1];
    if (rest[0] === '_internal') return 'backend/_internal (files)';
    return 'backend/' + rest[0];
  }
  const bin = parts.indexOf('bin');
  if (parts.includes('resources') && bin >= 0) return 'bin/' + (parts[bin + 1] || '');
  if (parts.includes('app.asar') || parts.includes('app.asar.unpacked')) return 'app.asar (Deck)';
  if (parts.includes('locales')) return 'Electron locales';
  if (parts.includes('Frameworks')) return 'Frameworks/' + (parts[parts.indexOf('Frameworks') + 1] || '');
  if (parts.includes('resources')) return 'resources/' + (parts[parts.indexOf('resources') + 1] || '');
  return 'Electron (' + parts[parts.length - 1] + ')';
}

function main() {
  const target = path.resolve(process.argv[2] || findUnpacked());
  if (!target || !fs.existsSync(target)) {
    console.log('package-size-report: no unpacked package found');
    return;
  }
  for (const entry of fs.existsSync(dist) ? fs.readdirSync(dist) : []) {
    if (/\.(exe|dmg|zip|AppImage|deb)$/.test(entry)) {
      console.log('installer ' + mib(fs.statSync(path.join(dist, entry)).size) + '  ' + entry);
    }
  }
  const rows = walk(target, []);
  const total = rows.reduce((sum, row) => sum + row.bytes, 0);
  console.log(`unpacked ${mib(total)}  ${path.relative(root, target) || target} (${rows.length} files)`);
  const buckets = new Map();
  for (const row of rows) {
    const key = bucket(path.relative(target, row.file).split(path.sep).join('/'));
    buckets.set(key, (buckets.get(key) || 0) + row.bytes);
  }
  console.log('\nlargest areas');
  for (const [key, bytes] of [...buckets].sort((a, b) => b[1] - a[1]).slice(0, 30)) {
    console.log(mib(bytes) + '  ' + key);
  }
  console.log('\nlargest files');
  for (const row of rows.sort((a, b) => b.bytes - a.bytes).slice(0, 15)) {
    console.log(mib(row.bytes) + '  ' + path.relative(target, row.file).split(path.sep).join('/'));
  }
}

main();
