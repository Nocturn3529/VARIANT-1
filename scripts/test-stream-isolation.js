'use strict';

/**
 * Stream isolation contract: multi-surface Deck chat must not adopt voice
 * streams, and turn-scoped errors must honor client_id/source. Chat logic lives in
 * `frontend/main-deck/src/chat/*` (not a single monolithic chatStore body).
 */

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const read = rel => fs.readFileSync(path.join(root, rel), 'utf8');
const chatStore = read('frontend/main-deck/src/chatStore.ts');
const chatIngest = read('frontend/main-deck/src/chat/ingest.ts');
const chatTurn = read('frontend/main-deck/src/chat/turn.ts');
const chatStateCore = read('frontend/main-deck/src/chat/stateCore.ts');
const turnStore = read('frontend/main-deck/src/state/turnStore.ts');
const chatDestination = [
  read('frontend/main-deck/src/ChatDestination.tsx'),
  read('frontend/main-deck/src/chat/ChatComposer.tsx'),
  read('frontend/main-deck/src/chat/ModelPicker.tsx'),
].join('\n');
const pipeline = read('backend/chat_pipeline.py');
const chatStream = read('backend/chat_stream.py');
const wsConfig = read('backend/ws_config.py');

assert.match(chatStream, /def stream_meta/);
assert.match(chatStream, /def infer_turn_source/);
assert.match(chatStream, /def bind_turn_identity/);
assert.match(pipeline, /from chat_stream import bind_turn_identity, infer_turn_source, stream_meta/);
assert.match(chatStream, /["']client_id["']/);
assert.match(chatStream, /["']source["']/);

assert.match(turnStore, /matchesEvent/);
// Execute the source/client/chat fences instead of coupling this contract to
// the local variable names used by a particular store implementation.
const Module = require('node:module');
const compiled = new Module(path.join(__dirname, '.stream-isolation.cjs'), module);
compiled._compile(require('esbuild').transformSync(turnStore, {loader: 'ts', format: 'cjs'}).code, compiled.id);
const turns = compiled.exports.turnController;
turns.begin({sessionId: 'A', clientId: 'client-A', source: 'chat'});
assert.equal(turns.matchesEvent({session_id: 'A', client_id: 'client-A', source: 'voice'}), false);
assert.equal(turns.matchesEvent({session_id: 'A', client_id: 'client-B', source: 'chat'}), false);
assert.equal(turns.matchesEvent({session_id: 'B', client_id: 'client-A', source: 'chat'}), false);
assert.equal(turns.matchesEvent({session_id: 'A', client_id: 'client-A', source: 'chat'}), true);
turns.bind('admission-A', 'run-A');
assert.equal(turns.matchesEvent({session_id: 'A', source: 'chat', admission_id: 'older-A'}), false);
assert.equal(turns.matchesEvent({session_id: 'A', client_id: 'another-window', source: 'chat', admission_id: 'admission-A'}), true);
turns.end();

// Isolation rules live on the chat modules after the chatStore split.
assert.match(chatTurn, /source === "voice"/);
assert.match(chatIngest, /client_id/);
assert.match(chatIngest, /isForeignStream/);
assert.match(chatIngest, /isTurnScopedError/);
assert.match(chatIngest, /isConfigSurfaceError/);
assert.match(chatTurn, /activeTurnClientIds/);
assert.match(chatStateCore, /getChatClientId/);
// Facade still re-exports the public client-id getter.
assert.match(chatStore, /getChatClientId/);

assert.match(chatDestination, /mode:set/);
assert.match(chatDestination, /mode:\s*["']local["']\s*\|\s*["']cloud["']/,
  'the composer model catalog must retain explicit local/cloud routes');
assert.doesNotMatch(chatDestination, /fallback\s*:/);
assert.doesNotMatch(chatDestination, /["']Auto["']/);

for (const globalName of ['Variant1Turn', 'variant1Chat', 'variant1Runtime']) {
  assert.doesNotMatch(chatStore, new RegExp(`window\\.${globalName}`));
  assert.doesNotMatch(chatIngest, new RegExp(`window\\.${globalName}`));
  assert.doesNotMatch(chatDestination, new RegExp(`window\\.${globalName}`));
}

assert.match(wsConfig, /_broadcast_config|HUB\.broadcast\(srv\._config_status_msg/);

console.log('stream isolation contract: all tests passed');
