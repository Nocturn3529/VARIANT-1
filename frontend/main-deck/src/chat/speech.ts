/**
 * Reply TTS: on-demand Play and auto-speak after turns.
 */
import type {SpeakMessage, TtsPreviewMessage} from "../protocol";
import type {ChatMessage, SpeechPhase} from "./types";
import {
  playSpeechPlayback,
  stopSpeechPlayback,
} from "../runtime/SpeechPlayback";
import {
  getChatState,
  notifyChat,
  patchChatState,
  sendChat,
  getChatContext,
} from "./stateCore";

let speechRequestSeq = 0;
let speechPendingId = "";

/** Transcript insertions must not move the playing indicator to another reply. */
export function speechKeyFor(message: ChatMessage, _index: number): string {
  if (message.localId) return `local:${message.localId}`;
  const ts = Number(message.ts) || 0;
  let hash = 2166136261;
  for (const character of String(message.text || "")) hash = Math.imul(hash ^ character.codePointAt(0)!, 16777619);
  return `m:${message.runId || ""}:${ts}:${(hash >>> 0).toString(36)}`;
}

function clearSpeechAudio() {
  stopSpeechPlayback("chat");
}

function setSpeechIdle() {
  const state = getChatState();
  if (state.speechPhase === "idle" && !state.speechKey) return;
  patchChatState({speechKey: null, speechPhase: "idle" as SpeechPhase});
}

/** Stop any in-flight or playing reply audio. */
export function stopSpeech() {
  speechRequestSeq += 1;
  const pending = speechPendingId;
  speechPendingId = "";
  if (pending) sendChat({type: "tts:preview:cancel", request_id: pending});
  clearSpeechAudio();
  setSpeechIdle();
}

function playSpeechData(audioB64: string, mime: string, key: string | null) {
  clearSpeechAudio();
  const safeMime = mime && mime.includes("/") ? mime : "audio/wav";
  patchChatState({
    speechKey: key,
    speechPhase: "playing",
  });
  playSpeechPlayback({
    owner: "chat",
    src: `data:${safeMime};base64,${audioB64}`,
    onEnded: setSpeechIdle,
    onStopped: setSpeechIdle,
    onError: () => {
      notifyChat("Couldn't play speech audio");
      setSpeechIdle();
    },
  });
}

/**
 * On-demand speak for one assistant reply via ``tts:preview`` (purpose=chat).
 * Clicking Play on the same bubble while loading/playing stops it.
 */
export function toggleSpeakReply(text: string, key: string): boolean {
  const body = String(text || "").trim();
  if (!body || body === "…") {
    notifyChat("Nothing to speak");
    return false;
  }
  const state = getChatState();
  if (
    state.speechKey === key
    && (state.speechPhase === "loading" || state.speechPhase === "playing")
  ) {
    stopSpeech();
    return true;
  }
  const context = getChatContext();
  if (!context || !context.isOpen?.()) {
    notifyChat("VARIANT-1 is reconnecting to the local backend");
    return false;
  }
  clearSpeechAudio();
  const requestId = `chat-${++speechRequestSeq}`;
  speechPendingId = requestId;
  patchChatState({speechKey: key, speechPhase: "loading"});
  const ok = sendChat({
    type: "tts:preview",
    purpose: "chat",
    request_id: requestId,
    session_id: state.sessionId || undefined,
    text: body.slice(0, 12000),
  });
  if (!ok) {
    speechPendingId = "";
    setSpeechIdle();
    notifyChat("Couldn't reach the speech service");
    return false;
  }
  return true;
}

export function handleTtsPreview(message: TtsPreviewMessage) {
  if (message.purpose !== "chat") return;
  const requestId = message.request_id;
  if (!requestId || requestId !== speechPendingId) return;
  speechPendingId = "";
  if (message.session_id && message.session_id !== getChatState().sessionId) {
    setSpeechIdle();
    return;
  }
  if (message.cancelled) {
    setSpeechIdle();
    return;
  }
  if (message.error) {
    const err = message.error.replace(/\s+/g, " ").trim();
    notifyChat(err ? `Speech failed: ${err.slice(0, 120)}` : "Speech failed");
    setSpeechIdle();
    return;
  }
  if (!message.audio) {
    notifyChat("Speech returned no audio");
    setSpeechIdle();
    return;
  }
  playSpeechData(
    message.audio,
    message.mime_type || "audio/wav",
    getChatState().speechKey,
  );
}

export function handleAutoSpeak(message: SpeakMessage) {
  if (!message.audio) return;
  const state = getChatState();
  if (message.session_id && message.session_id !== state.sessionId) return;
  if (state.speechPhase === "loading" && speechPendingId) return;
  speechPendingId = "";
  playSpeechData(
    message.audio,
    message.mime_type || "audio/wav",
    state.speechKey || "auto",
  );
}

export function resetSpeechForTests() {
  speechRequestSeq = 0;
  speechPendingId = "";
  clearSpeechAudio();
}
