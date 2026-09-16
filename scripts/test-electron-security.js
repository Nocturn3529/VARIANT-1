'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {isAllowedExternalUrl, isAllowedGuestUrl, isTrustedAppUrl} = require('../electron-security');
const {focusDeckForSecondInstance, isAudioOnlyMediaDetails} = require('../electron-app-boot');
const {normalizeAbsoluteLocalPath} = require('../electron-deck-ipc');

assert.strictEqual(isTrustedAppUrl('variant1://app/frontend/index.html'), true);
assert.strictEqual(isTrustedAppUrl('variant1://app/frontend/index.html?view=chat'), true);
assert.strictEqual(isTrustedAppUrl('https://app/frontend/index.html'), false);
assert.strictEqual(isTrustedAppUrl('variant1://app.example/frontend/index.html'), false);
assert.strictEqual(isTrustedAppUrl('not a url'), false);

assert.strictEqual(isAllowedExternalUrl('https://example.com/docs'), true);
assert.strictEqual(isAllowedExternalUrl('http://127.0.0.1:8000/status'), true);
assert.strictEqual(isAllowedExternalUrl('https://user:secret@example.com'), false);
assert.strictEqual(isAllowedExternalUrl('file:///C:/Windows/System32'), false);
assert.strictEqual(isAllowedExternalUrl('javascript:alert(1)'), false);
assert.strictEqual(isAllowedGuestUrl('about:blank'), true);
assert.strictEqual(isAllowedGuestUrl('https://example.com'), true);
assert.strictEqual(isAllowedGuestUrl('file:///C:/Windows/System32'), false);
assert.strictEqual(isAllowedGuestUrl('javascript:alert(1)'), false);
assert.strictEqual(isAllowedGuestUrl('variant1://app/frontend/index.html'), false);

assert.strictEqual(isAudioOnlyMediaDetails({mediaType: 'audio'}), true);
assert.strictEqual(isAudioOnlyMediaDetails({mediaType: 'video'}), false);
assert.strictEqual(isAudioOnlyMediaDetails({mediaTypes: ['audio']}), true);
assert.strictEqual(isAudioOnlyMediaDetails({mediaTypes: ['audio', 'video']}), false);

const root = path.join(__dirname, '..');
const windowSource = fs.readFileSync(path.join(root, 'electron-app-windows.js'), 'utf8');
const appBoot = fs.readFileSync(path.join(root, 'electron-app-boot.js'), 'utf8');
const browserDownloads = fs.readFileSync(path.join(root, 'electron-browser-downloads.js'), 'utf8');
const mainSource = fs.readFileSync(path.join(root, 'main.js'), 'utf8');
const deckPreload = fs.readFileSync(path.join(root, 'deck-preload.js'), 'utf8');
const deckIpc = fs.readFileSync(path.join(root, 'electron-deck-ipc.js'), 'utf8');
const browserBridge = fs.readFileSync(path.join(root, 'frontend/main-deck/src/workbench/browserBridge.ts'), 'utf8');
const browserPane = fs.readFileSync(path.join(root, 'frontend/main-deck/src/workbench/PreviewPane.tsx'), 'utf8');

assert.match(windowSource, /contextIsolation:\s*true[\s\S]*webviewTag:\s*true[\s\S]*nodeIntegration:\s*false[\s\S]*sandbox:\s*true/,
  'the Deck must expose sandboxed Chromium guests without renderer Node access');
assert.match(browserPane, /persist:variant1-preview/);
assert.match(browserPane, /contextIsolation=yes,nodeIntegration=no,sandbox=yes,webSecurity=yes/);
assert.doesNotMatch(browserPane, /preload=/,
  'remote guest pages must never receive the app preload');
assert.match(appBoot, /session\.fromPartition\('persist:variant1-preview'\)/,
  'the visible browser must have one persistent guest session');
assert.match(appBoot, /will-attach-webview/,
  'main must strip guest webPreferences before a webview attaches');
assert.match(appBoot, /previewAllowed/,
  'the preview session must allowlist permissions instead of auto-granting');
assert.doesNotMatch(appBoot, /previewSession\.setPermissionRequestHandler[\s\S]*callback\(true\)/,
  'the preview session must not auto-grant every Chromium permission');
assert.match(appBoot, /first !== 'frontend' && first !== 'assets'/,
  'variant1: must not map the whole appRoot');
assert.match(deckIpc, /core\.hooksPath/,
  'workbench git must disable repository hooks in the main process');
assert.match(mainSource, /registerBrowserDownloads\(/,
  'the application must install its owned download staging handler');
assert.match(browserDownloads, /session\.fromPartition\('persist:variant1-preview'\)\.on\('will-download'/,
  'downloads must use the same persistent browser session');
assert.doesNotMatch(appBoot, /setSaveDialogOptions/,
  'automatic save dialogs must not block download settlement or browser recovery');

assert.match(browserBridge, /data-variant1-browser-ref/);
assert.match(browserBridge, /sendInputEvent[\s\S]*mouseDown[\s\S]*mouseUp/,
  'model clicks must become Chromium input against the same visible guest');
assert.match(browserBridge, /captureWorkbenchPreview[\s\S]*guestId/,
  'the same guest must produce model-visible screenshot bytes');
assert.match(browserPane, /console-message[\s\S]*openDevTools[\s\S]*findInPage/,
  'console, DevTools, and find-in-page must remain available');
assert.strictEqual(fs.existsSync(path.join(root, 'electron-browser.js')), false,
  'the retired global WebContentsView authority must stay deleted');

const deckHtml = fs.readFileSync(path.join(root, 'frontend/main-deck/index.html'), 'utf8');
const framePolicy = deckHtml.match(/frame-src\s+([^;]+);/i);
assert.ok(framePolicy, 'Main Deck CSP must define frame-src');
assert.match(framePolicy[1], /https:/);
assert.doesNotMatch(framePolicy[1], /(?:^|\s)file:/);

assert.match(deckPreload, /readWorkbenchDirectory[\s\S]*readWorkbenchFile[\s\S]*writeWorkbenchFile/);
assert.match(deckIpc, /isTrustedIpcSender\(event, deckWin\(\)\)[\s\S]*workbench:fs:readDir/,
  'workbench filesystem IPC must remain Deck-authenticated');
assert.match(deckIpc, /shell\.trashItem\(target\)/,
  'file deletion must remain recoverable through the OS recycle bin');
assert.doesNotMatch(deckIpc + deckPreload + appBoot, /workbench:browser:popout|__variant1BrowserPopout|openBrowserWindow/,
  'browser windows must use the shared inert native host without an extra privileged preload');

const normalizedDocs = normalizeAbsoluteLocalPath(path.join(root, 'docs', '..', 'docs'));
assert.strictEqual(normalizedDocs, path.join(root, 'docs'));
assert.strictEqual(normalizeAbsoluteLocalPath('docs/README.md'), null);
assert.strictEqual(normalizeAbsoluteLocalPath('https://example.com/file'), null);
assert.strictEqual(normalizeAbsoluteLocalPath('C:\\bad\0path'), null);

assert.match(appBoot, /createBeforeQuitHandler\(/);
assert.match(appBoot, /function sendDeckWhenReady\(/);
assert.match(appBoot, /globalShortcut\.register\('CommandOrControl\+Space'/);

const secondInstanceCalls = [];
const expectedDeck = {kind: 'deck'};
assert.strictEqual(focusDeckForSecondInstance(
  {isReady: () => true},
  view => { secondInstanceCalls.push(view); return expectedDeck; },
), expectedDeck);
assert.deepStrictEqual(secondInstanceCalls, ['chat']);

console.log('electron app isolation and shared workbench-browser contract: ok');

