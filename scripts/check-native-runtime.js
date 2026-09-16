'use strict';
const fs = require('node:fs'), path = require('node:path'), crypto = require('node:crypto');
const root = path.resolve(__dirname, '..');
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'config/native-runtime.json'), 'utf8'));
for (const entry of manifest.files) {
  const file = path.join(root, 'bin', entry.file);
  if (!fs.existsSync(file) || crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex') !== entry.sha256) {
    throw new Error(`Native release input missing or unverified: ${entry.file}. Run npm run prepare:native.`);
  }
}
for (const notice of manifest.license_sources) {
  if (!fs.existsSync(path.join(root, 'assets/licenses/native', notice.file))) throw new Error(`Missing native notice: ${notice.file}`);
}
console.log(`Native release inputs: ${manifest.files.length} file hashes and notices verified`);
