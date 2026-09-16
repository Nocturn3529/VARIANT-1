'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');

const html = read(path.join('frontend', 'index.html'));
const renderer = read(path.join('frontend', 'renderer.js'));
const css = read(path.join('frontend', 'style.css'));
const preload = read('preload.js');
const electronOverlay = read('electron-overlay.js');
const main = read('main.js');

assert.doesNotMatch(html, /id="chat-input"|id="btn-mic"|id="input-wrap"/,
  'overlay HTML must not ship chat input or mic controls');
assert.match(html, /id="avatar-canvas"|id="avatar-box"/,
  'overlay must keep the Live2D avatar stage');
assert.doesNotMatch(`${html}\n${css}`, /fonts\.googleapis|fonts\.gstatic/,
  'overlay must not load Google Fonts');
assert.doesNotMatch(`${html}\n${css}\n${renderer}`, /id=["']hud["']|#hud\b|updateHud|placeholder-label/,
  'overlay must not keep a HUD or other on-screen text');
assert.doesNotMatch(`${html}\n${renderer}\n${css}`,
  /id=["']onboarding["']|setupOnboarding|finishOnboarding|ob-(?:card|mode|start|cloud|provider|key|tier)/i,
  'onboarding and product configuration must live outside the avatar overlay');
assert.doesNotMatch(`${html}\n${renderer}\n${css}`,
  /tool-approval|approval:request|approval_response|runtime-approval/,
  'the avatar overlay must not retain point-of-action approval UI or protocol');
assert.doesNotMatch(`${renderer}\n${css}`,
  /verify:(?:start|result|retry)|Verifying(?:State)|stage-(?:verifying)/,
  'the avatar overlay must not retain the retired central verifier state');

assert.doesNotMatch(renderer, /getElementById\(['"]chat-input['"]\)|getElementById\(['"]btn-mic['"]\)/,
  'renderer must not bind removed capsule controls');
assert.doesNotMatch(renderer, /function sendUserMessage|function toggleMic|function startMic/,
  'renderer must not wire overlay chat send or STT entry');
assert.match(renderer, /Live2D|MODEL_URL|setMood|animations/,
  'renderer must still load avatar animations');
assert.match(renderer, /case ['"]activity['"]/,
  'overlay must retain the global activity-to-mood projection');
assert.match(renderer, /case ['"]proactive['"]/,
  'overlay must retain proactive mood reactions');
assert.match(renderer, /\/ws\/activity\?token=/,
  'overlay must connect only to the subscribe-only presence endpoint');
assert.doesNotMatch(renderer, /127\.0\.0\.1:\$\{port\}\/ws\?token=/,
  'overlay must never connect to the Main Deck command socket');
assert.doesNotMatch(renderer, /\.send\s*\(|sendBackend|type:\s*['"](?:ping|config:get|cancel|mode:set|apikey:set)/,
  'the overlay backend socket must remain read-only');
assert.doesNotMatch(renderer,
  /case ['"](?:config|orphaned_task|thinking|tool:activity|transcript|speak|pong|start|token|done)['"]/,
  'overlay must not retain product/session message branches it cannot own');

assert.doesNotMatch(css, /#input-wrap\s*\{|#chat-input\s*\{|#btn-mic\.active/,
  'overlay CSS must not style the retired capsule');
assert.doesNotMatch(css,
  /#(?:ai-panel|tools-panel|onboarding)|\.(?:state-(?:idle|active|talking|thinking)|side-(?:left|right)|ink-panel|tool-row|mcp-row)/,
  'overlay CSS must contain only the live avatar surface');

assert.doesNotMatch(preload,
  /setOnboarded|getScreenContext|openDeckWindow|openSettings|openMonitor|pickFolder|getPathForFile|onVoiceMuted|onToggleMic|onOpenSettings/,
  'overlay preload must expose only avatar/window and read-only backend APIs');
assert.match(preload, /dragStart[\s\S]*dragMove[\s\S]*dragEnd[\s\S]*setMouseIgnore/,
  'overlay preload must retain drag and click-through controls');
assert.doesNotMatch(preload, /getBackendInfo|backend:getInfo|onBackendStatus|backend:status/,
  'overlay preload must not expose Main Deck backend discovery or status');
assert.match(preload, /onActivityStatus[\s\S]*activity:status/,
  'overlay preload must retain scoped subscribe-only activity discovery');
assert.doesNotMatch(preload, /rawInfo\.token|info:\s*\{\s*port,\s*token/,
  'overlay preload must never forward the Main Deck bearer token');
assert.doesNotMatch(electronOverlay, /send\(['"]backend:status['"]/,
  'overlay main-process status must use its scoped activity channel');
assert.match(main, /getStatusWindows:\s*\(\)\s*=>\s*\[deckWindow,\s*\.\.\.\(chatWindows\?\.list\(\) \|\| \[\]\)\]/,
  'full backend status goes only to Deck and pinned chats, excluding avatar and monitor');
const fullStatusFanout = main.match(/getStatusWindows:\s*\(\)\s*=>\s*([^\n]+)/)?.[1] || '';
assert.doesNotMatch(fullStatusFanout, /mainWindow|monitorWindow/,
  'avatar and monitor must never receive the full-bearer backend status');
assert.match(main, /getActivityWindows:\s*\(\)\s*=>\s*\[mainWindow\]/,
  'avatar status must use the separately sanitised activity fan-out');
assert.doesNotMatch(electronOverlay,
  /settings:setOnboarded|window:getScreenContext/,
  'overlay main-process IPC must not retain onboarding or dead layout state');

// Composer chrome lives in the React chat island (not static HTML after §26).
const chatIsland = [
  read(path.join('frontend', 'main-deck', 'src', 'ChatDestination.tsx')),
  read(path.join('frontend', 'main-deck', 'src', 'chat', 'ChatComposer.tsx')),
  read(path.join('frontend', 'main-deck', 'src', 'chat', 'ModelPicker.tsx')),
].join('\n');
const micStore = read(path.join('frontend', 'main-deck', 'src', 'state', 'micStore.ts'));
const micController = read(path.join(
  'frontend', 'main-deck', 'src', 'runtime', 'MicController.ts',
));
assert.match(chatIsland, /id=["']composer-input["']/,
  'Main Deck React composer retains chat input');
assert.match(chatIsland, /id=["']composer-voice["']/,
  'Main Deck React composer retains voice entry control');
assert.match(chatIsland, /id=["']model-button["']/,
  'Main Deck React composer retains model picker control');
assert.match(micStore, /new MicController/,
  'Main Deck mic store owns the typed microphone controller');
assert.match(micStore, /export function toggleMic/,
  'Main Deck retains voice entry surface wiring');
assert.match(micController, /export class MicController/,
  'Main Deck retains the microphone transport implementation');
assert.match(chatIsland, /id=["']model-menu["']/,
  'Main Deck React tree retains the provider model picker');

console.log('overlay avatar-only: all tests passed');
