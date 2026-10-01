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
import {useCallback, useRef, useSyncExternalStore} from "react";
import {resetTraceAnnotations} from "./chat/annotations";
import {resetDisclosures} from "./chat/disclosures";

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

export function shallowChatSelection<T extends object>(left:T,right:T):boolean {
  const keys=Object.keys(left) as Array<keyof T>;
  return keys.length===Object.keys(right).length && keys.every(key=>Object.is(left[key],right[key]));
}

/** Cache selected snapshots so unrelated root mutations do not repaint controls. */
export function useChatSelection<T>(selector:(state:ChatState)=>T,equal:(left:T,right:T)=>boolean=Object.is):T {
  const current=useRef({selector,equal});current.current={selector,equal};
  const cached=useRef<{state:ChatState;selector:typeof selector;selection:T}|undefined>(undefined);
  const snapshot=useCallback(()=>{
    const state=getDisplayedChatState(),{selector,equal}=current.current,prior=cached.current;
    if(prior?.state===state && prior.selector===selector)return prior.selection;
    const selected=selector(state),selection=prior && equal(prior.selection,selected)?prior.selection:selected;
    cached.current={state,selector,selection};return selection;
  },[]);
  return useSyncExternalStore(subscribe,snapshot,snapshot);
}

/** Test/helper: replace state (used sparingly). */
export function __resetChatStoreForTests() {
  resetTraceAnnotations();
  resetDisclosures();
  resetRecoveryNotices();
  invalidatePendingChatAttachments();
  resetSpeechForTests();
  stopSpeech();
  resetTurnReceipt();
  resetChatStateBag();
}
