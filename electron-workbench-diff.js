'use strict';

const DISPLAY_CHARS = 256 * 1024;
const COMMAND_BYTES = 2 * 1024 * 1024;

/** Keep IPC payloads bounded and distinguish exact sizes from a killed writer. */
function projectGitDiff(value, overflow = false) {
  const text = String(value || '');
  const truncated = overflow || text.length > DISPLAY_CHARS;
  let diff = text.slice(0, DISPLAY_CHARS);
  if (truncated) {
    const boundary = diff.lastIndexOf('\n');
    if (boundary > diff.length / 2) diff = diff.slice(0, boundary);
  }
  return {diff, truncated, originalBytes: Buffer.byteLength(text, 'utf8'), originalBytesExact: !overflow,
    binary: /^Binary files .+ differ\r?$/m.test(text)};
}

module.exports = {projectGitDiff, COMMAND_BYTES};
