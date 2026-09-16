'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {
  llamaServerBasename,
  nativeRuntimeManifestRelPath,
} = require('./native-runtime-paths');

const root = path.resolve(__dirname, '..');
const rel = nativeRuntimeManifestRelPath();
if (!rel) {
  console.error(
    'check-native-runtime: no recipe mapping for ' +
      process.platform + '/' + process.arch + '.'
  );
  process.exit(2);
}
const manifestPath = path.join(root, rel);
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
const files = Array.isArray(manifest.files) ? manifest.files : [];

if (manifest.status === 'stub' || files.length === 0) {
  const binName = llamaServerBasename();
  const candidate = path.join(root, 'bin', binName);
  if (fs.existsSync(candidate)) {
    console.log(
      'Native check (' + process.platform + '): found ' + binName +
        ' (stub manifest ' + rel + ' ? hashes not verified)'
    );
    process.exit(0);
  }
  console.error(
    'check-native-runtime: stub manifest ' + rel +
      ' and no ' + binName + ' under bin/.'
  );
  console.error(
    'Fill the Unix recipe or place llama-server in bin/ (M3). Windows x64 still uses config/native-runtime.json.'
  );
  process.exit(2);
}

for (const entry of files) {
  const file = path.join(root, 'bin', entry.file);
  if (
    !fs.existsSync(file) ||
    crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex') !==
      entry.sha256
  ) {
    throw new Error(
      'Native release input missing or unverified: ' +
        entry.file +
        '. Run npm run prepare:native.'
    );
  }
}
for (const notice of manifest.license_sources || []) {
  if (!fs.existsSync(path.join(root, 'assets/licenses/native', notice.file))) {
    throw new Error('Missing native notice: ' + notice.file);
  }
}
console.log(
  'Native release inputs: ' + files.length + ' file hashes and notices verified (' + rel + ')'
);
