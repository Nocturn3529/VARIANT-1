'use strict';

const {isUtf8} = require('node:buffer');

// Text editing must round-trip the bytes as UTF-8. An extension is only a
// preview hint; it cannot make a binary/UTF-16 payload safe for the text writer.
function isEditableText(bytes) {
  return isUtf8(bytes) && !bytes.some(byte => byte < 32 && ![9, 10, 13].includes(byte));
}

module.exports = {isEditableText};
