'use strict';
const fs = require('node:fs');
const path = require('node:path');

/** Keep a new acceptance run from overwriting a previous handoff's evidence. */
module.exports = function nativeTestArtifacts(root, fallback, category) {
  const directory = process.env.VARIANT1_FRONTEND_TEST_ARTIFACTS
    ? path.resolve(process.env.VARIANT1_FRONTEND_TEST_ARTIFACTS, category)
    : path.resolve(root, fallback);
  fs.mkdirSync(directory, {recursive: true});
  return directory;
};
