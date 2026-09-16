/**
 * Shared chat store core: state bag, emit, runtime context, turn helpers.
 * Feature modules (attachments, speech, session, turn, composer) import this —
 * not each other in a cycle through chatStore.
 */
import type {ChatWsCommand} from "../protocol";
import {initialAgentTeam} from "../protocol/children";
import type {RuntimeContext} from "../types";
import {focusTurnSession, turnController, withTurnSession} from "../state/turnStore";
import type {ChatAttachment, ChatState, SubtitleState} from "./types";

export const CLIENT_ID = `deck-react-${Math.random().toString(36).slice(2, 10)}`;
export const MAX_ATTACHMENTS = 6;
export const MAX_TURN_STEPS = 48;

export let context: RuntimeContext | null = null;

export let state: ChatState = {
  agentTeam:initialAgentTeam(),
  connected: false,
  clientId: CLIENT_ID,
  sessionId: null,
  title: "VARIANT-1",
  messages: [],
  turnActive: false,
  streaming: false,
  streamText: "",
  subtitle: "Ready",
  subtitleState: "ready",
  lastError: "",
  draft: "",
  attachments: [],
  attachmentsPreparing: 0,
  deliveryMode: "steer",
  stopPending: false,
  pause: null,
  inputQueue: {snapshot:null,synced:false,recent:[]},
  goal: {snapshot:null,synced:false},
  speechKey: null,
  speechPhase: "idle",
  turnSteps: [],
  queuedFollowUps: 0,
  activeTurnId: null,
  pendingActiveInputs: [],
  mutationTogglePending: null,
  runtime: null,
  orphanedTask: null,
};

const listeners = new Set<() => void>();
const sessions = new Map<string, ChatState>();
let displayedDuringEvent: ChatState | null = null;
const revisions = new Map<string, number>();

export const getDisplayedChatState = () => displayedDuringEvent || state;
export const getCachedChatState = (id: string) => state.sessionId === id ? state : sessions.get(id);
export const cachedChatStates = () => [...sessions.values()];

/** Preserve recognized speech when its original chat cannot send it now. */
export function retainChatDraft(id:string,text:string,title="New chat"):void {
  const current=getCachedChatState(id) || {...initialChatState(),sessionId:id,title};
  const draft=[current.draft,text].filter(Boolean).join("\n\n");
  if(id===state.sessionId) patchChatState({draft});
  else {sessions.set(id,{...current,draft});revisions.set(id,(revisions.get(id)||0)+1);}
}

/** A navigation changes the projection; running chats retain their live state. */
export function activateChatState(id: string): void {
  if (state.sessionId === id) { focusTurnSession(id); return; }
  const prior = state;
  if (prior.sessionId) sessions.set(prior.sessionId, prior);
  state = {...(sessions.get(id) || {...initialChatState(), sessionId:id,
    ...(!prior.sessionId ? {draft:prior.draft, attachments:prior.attachments} : {})}), connected:prior.connected};
  revisions.set(id, (revisions.get(id) || 0) + 1);
  sessions.set(id, state);focusTurnSession(id);
  for (const [key, value] of sessions) {
    if (sessions.size <= 8) break;
    if (key !== id && !value.turnActive && !value.draft && !value.attachments.length && !value.pendingActiveInputs.length && !value.inputQueue.snapshot?.items.length && !value.inputQueue.action && !value.goal.pending && !value.agentTeam.active && !value.agentTeam.selectedId) {
      sessions.delete(key);revokeRemovedAttachmentUrls(value, initialChatState());
    }
  }
}

/** All mutations in this callback are synchronous projections of one event. */
export function withCachedChatState(id: string, callback: () => void): boolean {
  if (!id || id === state.sessionId) { callback(); return true; }
  const cached = sessions.get(id);
  if (!cached) return false;
  const previous = state, previousDisplayed = displayedDuringEvent;
  displayedDuringEvent ||= state;state = cached;
  try { withTurnSession(id, callback); }
  finally { sessions.set(id, state);state=previous;displayedDuringEvent=previousDisplayed; }
  return true;
}

function attachmentUrlsInState(value: ChatState): Set<string> {
  const urls = new Set<string>();
  const collect = (items: ChatAttachment[] | undefined) => {
    for (const item of items || []) {
      if (item.previewUrl) urls.add(item.previewUrl);
    }
  };
  collect(value.attachments);
  for (const message of value.messages) collect(message.attachments);
  return urls;
}

function revokeRemovedAttachmentUrls(previous: ChatState, next: ChatState): void {
  const retained = attachmentUrlsInState(next);
  for (const url of attachmentUrlsInState(previous)) {
    if (retained.has(url)) continue;
    try {
      URL.revokeObjectURL(url);
    } catch {
      /* ignore environments without object-URL support */
    }
  }
}

export function emit(): void {
  if (state.sessionId) sessions.set(state.sessionId, state);
  listeners.forEach(listener => listener());
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export const getComposerRevision = (sessionId = state.sessionId || "") => revisions.get(sessionId) || 0;
function noteComposerChange(next: ChatState) {
  if (next.draft !== state.draft || next.attachments !== state.attachments || next.sessionId !== state.sessionId) revisions.set(next.sessionId || "", (revisions.get(next.sessionId || "") || 0) + 1);
}

export function getChatState(): ChatState {
  return state;
}

export function setChatState(next: ChatState): void {
  noteComposerChange(next);
  revokeRemovedAttachmentUrls(state, next);
  state = next;
  emit();
}

export function patchChatState(partial: Partial<ChatState>): void {
  const next = {...state, ...partial};
  noteComposerChange(next);
  revokeRemovedAttachmentUrls(state, next);
  state = next;
  emit();
}

/** Mutate state without emit (caller emits once after batched updates). */
export function replaceChatState(next: ChatState): void {
  noteComposerChange(next);
  revokeRemovedAttachmentUrls(state, next);
  state = next;
}

export function setChatContext(next: RuntimeContext): void {
  context = next;
}

export function getChatContext(): RuntimeContext | null {
  return context;
}

export function sendChat(payload: ChatWsCommand): boolean {
  // Quiet while offline — the subtitle already shows reconnecting.
  return !!context?.send(payload);
}

export function notifyChat(message: string): void {
  context?.notify(displayedDuringEvent ? `${state.title || "Another chat"}: ${message}` : message);
}

export function turnApi() {
  return turnController;
}

export function beginSharedTurn(opts: {
  clientId: string;
  source?: string;
  sessionId?: string | null;
}): void {
  turnController.begin({
    clientId: opts.clientId,
    source: opts.source || "chat",
    sessionId: opts.sessionId || "",
  });
}

export function endSharedTurn(status = "complete"): void {
  turnController.end({status});
}

export function sharedTurnActive(): boolean {
  return turnController.isActive();
}

export function isChatTurnActive(): boolean {
  return sharedTurnActive();
}

export function getChatClientId(): string {
  return state.clientId;
}

export function setSubtitle(text: string, subtitleState: SubtitleState = "ready"): void {
  if (state.subtitle === text && state.subtitleState === subtitleState) return;
  state = {...state, subtitle: text, subtitleState};
}

export function applyChatSubtitle(text: string, subtitleState: SubtitleState = "ready"): void {
  setSubtitle(text, subtitleState);
  emit();
}

export function revokeAttachmentUrls(items: ChatAttachment[]): void {
  for (const item of items) {
    if (item.previewUrl) {
      try {
        URL.revokeObjectURL(item.previewUrl);
      } catch {
        /* ignore */
      }
    }
  }
}

export function initialChatState(): ChatState {
  return {
    agentTeam:initialAgentTeam(),
    connected: false,
    clientId: CLIENT_ID,
    sessionId: null,
    title: "VARIANT-1",
    messages: [],
    turnActive: false,
    streaming: false,
    streamText: "",
    subtitle: "Ready",
    subtitleState: "ready",
    lastError: "",
    draft: "",
    attachments: [],
    attachmentsPreparing: 0,
    deliveryMode: "steer",
    stopPending: false,
    pause: null,
    inputQueue: {snapshot:null,synced:false,recent:[]},
  goal: {snapshot:null,synced:false},
    speechKey: null,
    speechPhase: "idle",
    turnSteps: [],
    queuedFollowUps: 0,
    activeTurnId: null,
    pendingActiveInputs: [],
    mutationTogglePending: null,
    runtime: null,
    orphanedTask: null,
  };
}

export function resetChatStateBag(): void {
  const next = initialChatState();
  noteComposerChange(next);
  revokeRemovedAttachmentUrls(state, next);
  for (const value of sessions.values()) revokeRemovedAttachmentUrls(value, next);
  sessions.clear();revisions.clear();displayedDuringEvent=null;focusTurnSession("");
  state = next;
  emit();
}
