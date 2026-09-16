/**
 * Chat destination store — composition root for Main Deck chat modules.
 *
 * Lifecycle split (see `./chat/`):
 * - types / stateCore — shared bag + turn helpers
 * - attachments — binary encode + composer attach list
 * - speech — TTS play / auto-speak
 * - session — applySession + message rehydration
 * - turn — STEPS, activity, finishStream
 * - composer — optimistic send / cancel / draft
 * - ingest — pure WS event router
 * - connection — wire status + turn bridge
 *
 * Public import path stays `./chatStore` for DeckApp / ChatDestination / mic.
 */
import {useSyncExternalStore} from "react";

import type {ChatState} from "./chat/types";
import {
  applyChatSubtitle,
  getChatClientId,
  getChatContext,
  getChatState,
  getDisplayedChatState,
  isChatTurnActive,
  notifyChat,
  resetChatStateBag,
  sendChat,
  setChatContext,
  subscribe,
} from "./chat/stateCore";

import {
  cancelChatTurn,
  refreshChat,
  resumeOrphanedTask,
  sendUserMessage,
  setMutationWriteEnabled,
  setChatDraft,
} from "./chat/composer";
import {
  addChatFiles,
  addChatPathAttachment,
  clearChatAttachments,
  invalidatePendingChatAttachments,
  removeChatAttachment,
} from "./chat/attachments";
import {
  speechKeyFor,
  stopSpeech,
  toggleSpeakReply,
  resetSpeechForTests,
} from "./chat/speech";
import {ingestChat} from "./chat/ingest";
import {attachTurnBridge, setChatConnection} from "./chat/connection";
import {resetTurnReceipt} from "./chat/receipt";
import {resetRecoveryNotices} from "./chat/recovery";

// Re-export types for consumers.
export type {
  ChatAttachment,
  ChatEvidence,
  ChatEvidenceKind,
  ChatMessage,
  ChatSessionMeta,
  ChatRuntimeState,
  ChatState,
  ChatTurnReceipt,
  ChatTurnStep,
  SpeechPhase,
  SubtitleState,
} from "./chat/types";

export {
  applyChatSubtitle,
  addChatPathAttachment,
  cancelChatTurn,
  clearChatAttachments,
  getChatClientId,
  getChatContext,
  getChatState,
  ingestChat,
  isChatTurnActive,
  notifyChat,
  refreshChat,
  resumeOrphanedTask,
  removeChatAttachment,
  sendChat,
  sendUserMessage,
  setChatConnection,
  setChatContext,
  setChatDraft,
  setMutationWriteEnabled,
  speechKeyFor,
  stopSpeech,
  toggleSpeakReply,
  addChatFiles,
  invalidatePendingChatAttachments,
};

if (typeof window !== "undefined") {
  attachTurnBridge();
}

export function useChatState(): ChatState {
  return useSyncExternalStore(subscribe, getDisplayedChatState, getDisplayedChatState);
}

/** Test/helper: replace state (used sparingly). */
export function __resetChatStoreForTests() {
  resetRecoveryNotices();
  invalidatePendingChatAttachments();
  resetSpeechForTests();
  stopSpeech();
  resetTurnReceipt();
  resetChatStateBag();
}
