"use strict";

const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..");
const micTs = fs.readFileSync(path.join(
  root, "frontend", "main-deck", "src", "runtime", "MicController.ts",
), "utf8");
const micStore = fs.readFileSync(path.join(
  root, "frontend", "main-deck", "src", "state", "micStore.ts",
), "utf8");
const workletJs = fs.readFileSync(path.join(
  root, "frontend", "main-deck", "public", "mic-capture-processor.js",
), "utf8");
const chat = fs.readFileSync(path.join(
  root, "frontend", "main-deck", "src", "chat", "ChatComposer.tsx",
), "utf8");
const html = fs.readFileSync(path.join(
  root, "frontend", "main-deck", "index.html",
), "utf8");
const preload = fs.readFileSync(path.join(root, "deck-preload.js"), "utf8");
const deckApp = fs.readFileSync(path.join(
  root, "frontend", "main-deck", "src", "DeckApp.tsx",
), "utf8");

assert.match(micTs, /AudioWorkletNode/, "mic must use AudioWorkletNode");
assert.match(micTs, /audioWorklet\.addModule/, "mic must load an AudioWorklet module");
assert.doesNotMatch(micTs, /createScriptProcessor/, "deprecated ScriptProcessor path must be gone");
assert.match(micTs, /variant1-mic-capture/, "worklet processor name is stable");
assert.match(
  workletJs,
  /registerProcessor\(["']variant1-mic-capture["']/,
  "worklet file registers the processor",
);
assert.match(workletJs, /AudioWorkletProcessor/, "worklet extends AudioWorkletProcessor");
assert.match(micTs, /export function encodeWav/, "WAV encoder stays testable");
assert.match(micTs, /MIC_SILENCE_LIMIT_MS\s*=\s*1_200/);
assert.match(micTs, /MIC_RMS_GATE\s*=\s*0\.012/);
assert.match(micTs, /MIC_MAX_MS\s*=\s*30_000/);
assert.doesNotMatch(
  micTs,
  /document\.addEventListener/,
  "typed mic controller must not delegate document clicks",
);
assert.match(
  micStore,
  /submitUserInput\(\{source: "voice", text, sessionId\},owner\.deliveryMode\)/,
  "transcript submits its own input bundle and captured delivery choice without consuming the typed composer",
);
assert.match(chat, /id=["']composer-voice["']/, "React renders the mic control");
assert.match(chat, /onClick=\{toggleMic\}/, "React mic uses a direct handler");
assert.match(preload, /onVoiceToggle[\s\S]*voice:toggle/,
  "preload exposes the Ctrl+Space voice IPC event");
assert.match(preload, /removeListener\('voice:toggle'/,
  "voice IPC subscriptions can be disposed");
assert.match(deckApp, /onVoiceToggle[\s\S]*navigateTo\("chat"\)[\s\S]*toggleMic\(\)/,
  "Ctrl+Space navigates to Chat and uses the typed mic controller");
assert.doesNotMatch(
  html,
  /runtime-mic\.js/,
  "production no longer loads the vanilla mic runtime",
);

console.log("deck mic AudioWorklet: all tests passed");
