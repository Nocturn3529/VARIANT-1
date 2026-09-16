'use strict';

/**
 * Static contract: Main Deck composer attachments reach chat WS + backend.
 * Attachment encode/list live in `chat/attachments.ts`; send wiring in
 * `chat/composer.ts`; `chatStore.ts` re-exports the public API.
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const read = rel => fs.readFileSync(path.join(root, rel), 'utf8');

const chatStore = read('frontend/main-deck/src/chatStore.ts');
const chatAttachments = read('frontend/main-deck/src/chat/attachments.ts');
const chatComposer = read('frontend/main-deck/src/chat/composer.ts');
const chatComposerView = read('frontend/main-deck/src/chat/ChatComposer.tsx');
const chatDest = [
  read('frontend/main-deck/src/ChatDestination.tsx'),
  chatComposerView,
].join('\n');
const chatProtocol = read('frontend/main-deck/src/protocol/chatCommands.ts');
const deckPreload = read('deck-preload.js');
const backendAttachments = read('backend/chat_attachments.py');
const chatSetupStage = read('backend/chat_setup_stage.py');
const chatTurnPlan = read('backend/chat_turn_plan.py');
const chatContextStage = read('backend/chat_context_stage.py');
const chatAgentStage = read('backend/chat_agent_stage.py');
const dispatch = read('backend/ws_dispatch.py');
const agentNodes = read('backend/agent_engine/nodes.py');
const agentState = read('backend/agent_engine/state.py');
const snapshotStore = read('backend/agent_engine/sqlite_snapshot_store.py');

assert.match(chatStore, /addChatFiles/, 'chatStore must expose addChatFiles');
assert.match(chatAttachments, /createImageBitmap|bytesToBase64/,
  'image encode must use robust bitmap/raw fallbacks');
assert.match(chatAttachments, /attachments/, 'attachments module must track pending attachments');
assert.match(chatComposer, /payload\.attachments|attachments:\s*wireAttachments/,
  'sendUserMessage must wire attachments array');
assert.doesNotMatch(chatComposer, /payload\.image_b64/,
  'frontend must not duplicate the first image in a legacy top-level field');
assert.doesNotMatch(chatProtocol, /image_b64/,
  'typed chat wire contract must use attachments only');
assert.doesNotMatch(chatAttachments, /\bfile\.path\b/i,
  'renderer must not use Electron\'s removed File.path augmentation');
assert.match(deckPreload, /webUtils\.getPathForFile/,
  'preload must resolve disk-backed Web Files with Electron webUtils');
assert.doesNotMatch(deckPreload, /pickFiles|dialog:pickFiles/,
  'the replaced path-only native attachment route must stay deleted');
assert.doesNotMatch(chatAttachments, /split\(["']?,["']?,\s*1\)\[1\]/,
  'data URL extraction must not use Python-style split semantics');
assert.match(chatAttachments, /path:/, 'attachments must carry absolute path when available');
assert.match(chatDest, /id=["']attach-button["']/, 'composer must always render attach button');
assert.match(chatDest, /ComposerChips|composer__chips/, 'composer must render attachment chips');
assert.match(chatDest, /removeChatAttachment/, 'chips must allow remove');
assert.match(chatDest, /dragover|onDrop|drop/, 'composer must accept drag-and-drop');
assert.match(chatComposerView, /onPaste=/,
  'composer must accept clipboard image and file paste');

assert.match(chatComposerView, /fileInputRef\.current\?\.click/,
  'attach button must use Web Files so path-backed images retain a byte fallback');
assert.match(chatDest, /addChatFiles\(event\.currentTarget\.files,filePickerOwner\.current\)/,
  'hidden input hands files and the originating chat to the typed action');
assert.doesNotMatch(chatDest, /window\.variant1Chat/,
  'attachments must not cross a window bridge');

assert.match(backendAttachments, /def parse_chat_attachments/, 'backend parses attachments');
assert.match(backendAttachments, /def normalize_image_b64/, 'backend normalizes image_b64');
assert.match(backendAttachments, /prepare_image_observations/,
  'backend must validate, orient, resize, and tile model images centrally');
assert.match(backendAttachments, /class PathAttachmentLoad/,
  'backend path attachment status must not be inferred from rendered prose');
assert.match(chatComposer, /optimisticAttachmentRetry/,
  'delayed rejection must retain a short-lived wire-capable retry copy');
assert.match(backendAttachments, /_MAX_USER_IMAGES\s*=\s*4/,
  'backend must bound original user images');
assert.match(chatTurnPlan, /user_images\s*=\s*tuple/,
  'turn planning must preserve every prepared user image');
assert.doesNotMatch(chatSetupStage, /vision_state/,
  'setup must not reject images before native-image-first provider routing');
assert.match(chatContextStage, /model_images\s*=\s*list\(attachments\.user_images/,
  'context stage must project every prepared user image');
assert.match(chatAgentStage, /images=request\.model_images/,
  'graph stage must pass prepared user images into the live graph runtime');
assert.match(dispatch, /parse_chat_attachments/,
  'ws chat handler must parse the canonical attachments array');
assert.doesNotMatch(dispatch, /msg\.get\(["']image_b64["']\)/,
  'ws chat handler must not retain the removed top-level image contract');
assert.match(agentNodes, /list\(runtime\.images or \[\]\)/,
  'every model step must reattach current-turn runtime images');
assert.doesNotMatch(agentState, /initial_image_b64|pending_image_b64/,
  'durable graph state must contain image metadata only');
assert.match(snapshotStore, /_normalize_and_scrub/,
  'checkpoint writes must mechanically strip transient image bytes');

console.log('chat attachments contract: all tests passed');
