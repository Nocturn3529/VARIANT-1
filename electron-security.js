'use strict';

/**
 * Pure app-window security helpers shared by Electron's main process and the
 * Node test suite. Workbench guests may load only http(s) and about:blank.
 */

const TRUSTED_APP_SCHEME = 'variant1:';
const TRUSTED_APP_HOST = 'app';
function parseUrl(raw) {
  try {
    return new URL(String(raw || ''));
  } catch (_) {
    return null;
  }
}

function isTrustedAppUrl(raw) {
  const parsed = parseUrl(raw);
  return !!(
    parsed
    && parsed.protocol === TRUSTED_APP_SCHEME
    && parsed.host === TRUSTED_APP_HOST
    && !parsed.username
    && !parsed.password
  );
}

function isAllowedExternalUrl(raw) {
  const parsed = parseUrl(String(raw || '').trim());
  return !!(
    parsed
    && (parsed.protocol === 'http:' || parsed.protocol === 'https:')
    && !parsed.username
    && !parsed.password
  );
}

function isAllowedGuestUrl(raw) {
  const text = String(raw || '').trim();
  if (text === 'about:blank') return true;
  return isAllowedExternalUrl(text);
}

module.exports = {
  isAllowedExternalUrl,
  isAllowedGuestUrl,
  isTrustedAppUrl,
};
