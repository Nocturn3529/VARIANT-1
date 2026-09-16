'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {
  isWindowsX64NativeRecipe,
  llamaServerBasename,
} = require('./native-runtime-paths');

const root = path.resolve(__dirname, '..');

if (!isWindowsX64NativeRecipe()) {
  const binName = llamaServerBasename();
  const candidate = path.join(root, 'bin', binName);
  if (fs.existsSync(candidate)) {
    console.log(
      'Native check (' + process.platform + '): found ' + binName +
        ' (no Unix hash manifest yet)'
    );
    process.exit(0);
  }
  console.error(
    'check-native-runtime: no ' + binName + ' under bin/ and no hash manifest for ' +
      process.platform + '/' + process.arch + '.'
  );
  console.error(
    'Windows x64: run npm run prepare:native. Unix: place llama-server in bin/ (M3).'
  );
  process.exit(2);
}

const manifest = JSON.parse(
  fs.readFileSync(path.join(root, 'config/native-runtime.json'), 'utf8')
);
for (const entry of manifest.files) {
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
for (const notice of manifest.license_sources) {
  if (!fs.existsSync(path.join(root, 'assets/licenses/native', notice.file))) {
    throw new Error('Missing native notice: ' + notice.file);
  }
}
console.log(
  'Native release inputs: ' +
    manifest.files.length +
    ' file hashes and notices verified'
);
