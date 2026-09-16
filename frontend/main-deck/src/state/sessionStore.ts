import {detachedChatId} from "../runtime/viewIdentity";
import {releaseDeletedChat} from "./deletedChatCleanup";
import {canClosePreviewTabs,getPreviewState} from "../workbench/previewStore";
import type {WsCommand} from "../protocol";
import type {RuntimeContext} from "../types";
import {createModuleStore} from "./createModuleStore";
import {activeTurnSessionIds, subscribeTurn} from "./turnStore";
import {stopSpeech} from "../chat/speech";
import {parseChatProject,type ChatProject} from "./chatProjectStore";
import type {ChatNavigationOutcome} from "../protocol/chatEvents";

export type SessionSummary = {
  id: string;
  title: string;
  messageCount: number;
  updatedAt: number | string | null;
  pinned: boolean;
  archived: boolean;
  project?: ChatProject | null;
};

export type SessionSearchHit = SessionSummary & {
  role: string;
  snippet: string;
  ts: number | string | null;
};

type PendingSessionAction =
  | {type: "switch"; id: string; requestId: string}
  | {type: "new"; requestId: string}
  | null;

type ExpectedSessionChange =
  | {type: "switch"; id: string; requestId: string}
  | {type: "new"; requestId: string}
  | {type: "fallback"}
  | null;

export type SessionState = Readonly<{
  connected: boolean;
  loading: boolean;
  items: SessionSummary[];
  activeSessionId: string | null;
  displayedSessionId: string | null;
  workingSessionIds: string[];
  searchQuery: string;
  searchResults: SessionSearchHit[] | null;
  openMenuId: string | null;
  error: string;
  pendingAction: PendingSessionAction;
}>;

let searchTimer: ReturnType<typeof setTimeout> | null = null;
let pendingDispatch: "queued" | "sent" | null = null;
let pendingNeedsRecovery = false;
let expectedSessionChange: ExpectedSessionChange = null;
const OFFLINE_ERROR = "Backend offline — reconnecting";
const initialState = (): SessionState => ({
  connected: false,
  loading: true,
  items: [],
  activeSessionId: null,
  displayedSessionId: null,
  workingSessionIds: [],
  searchQuery: "",
  searchResults: null,
  openMenuId: null,
  error: "",
  pendingAction: null,
});
const store = createModuleStore<SessionState>({initialState: initialState()});

function send(command: WsCommand): boolean {
  return store.send(command);
}

function notify(message: string): void {
  store.getContext()?.notify(message);
}

function parseSession(value: unknown): SessionSummary | null {
  if (!value || typeof value !== "object") return null;
  const row = value as Record<string, unknown>;
  const id = String(row.id || row.session_id || "");
  if (!id) return null;
  return {
    id,
    title: String(row.title || "New chat"),
    messageCount: Number(row.message_count || 0),
    updatedAt: row.updated_at as number | string | null ?? null,
    pinned: !!row.pinned,
    archived: !!row.archived,
    project: parseChatProject(row.project),
  };
}

function parseSearchHit(value: unknown): SessionSearchHit | null {
  const session = parseSession(value);
  if (!session || !value || typeof value !== "object") return null;
  const row = value as Record<string, unknown>;
  return {
    ...session,
    role: String(row.role || ""),
    snippet: String(row.snippet || ""),
    ts: row.ts as number | string | null ?? null,
  };
}

function dispatchPendingAction(): boolean {
  const state = store.getState();
  const pending = state.pendingAction;
  if (!pending) return false;
  stopSpeech();
  const ok = pending.type === "switch"
    ? send({type: "chat:session:switch", id: pending.id, request_id: pending.requestId})
    : send({type: "chat:session:new", request_id: pending.requestId});
  pendingDispatch = ok ? "sent" : "queued";
  if (!ok) notify("Backend offline — the chat change will retry after reconnecting");
  return ok;
}

function flushPendingAction(): void {
  const state = store.getState();
  if (!state.pendingAction || pendingDispatch !== "queued") return;
  dispatchPendingAction();
}

subscribeTurn(() => { store.setState({workingSessionIds: activeTurnSessionIds()}); });

export function setSessionContext(next: RuntimeContext): void {
  store.setContext(next);
}

export function setSessionConnection(status: string): void {
  const connected = status === "connected";
  const state = store.getState();
  if (!connected && state.pendingAction && pendingDispatch === "sent") {
    // A successful socket write can still lose its acknowledgement. Keep the
    // intent until reconnect hydration proves which session the backend bound.
    pendingNeedsRecovery = true;
  }
  if (state.connected === connected) {
    if (connected && state.pendingAction && pendingDispatch === "queued") {
      flushPendingAction();
    } else if (!connected && !state.items.length && state.error !== OFFLINE_ERROR) {
      store.setState({loading: false, error: OFFLINE_ERROR});
    }
    return;
  }
  store.setState({
    connected,
    loading: connected ? state.loading : false,
    error: connected
      ? (state.error === OFFLINE_ERROR ? "" : state.error)
      : (state.items.length ? state.error : OFFLINE_ERROR),
  });
  if (connected && state.pendingAction && pendingDispatch === "queued") {
    flushPendingAction();
  } else if (connected && !state.pendingAction && state.displayedSessionId) {
    // Snapshot reads no longer mutate the server's view. Rebind the selected
    // chat explicitly when a new socket inherited another shared default.
    const action={type:"switch" as const,id:state.displayedSessionId,requestId:"reconnect-"+globalThis.crypto.randomUUID()};
    expectedSessionChange=action;pendingDispatch="queued";pendingNeedsRecovery=false;
    store.setState({pendingAction:action});dispatchPendingAction();
  }
}

export function refreshSessions(): void {
  const state = store.getState();
  if (!state.connected && !store.getContext()?.isOpen?.()) return;
  store.setState({loading: true, error: ""});
  send({type: "chat:sessions"});
}

export function switchSession(id: string): boolean {
  const nextId = String(id || "");
  if(detachedChatId() && nextId!==detachedChatId())return false;
  const state = store.getState();
  if (!nextId || (nextId === state.displayedSessionId && !state.pendingAction)) return false;
  const action = {type: "switch" as const, id: nextId, requestId: `switch-${globalThis.crypto.randomUUID()}`};
  store.setState({
    pendingAction: action,
    openMenuId: null,
  });
  pendingDispatch = "queued";
  pendingNeedsRecovery = false;
  expectedSessionChange = action;
  return dispatchPendingAction();
}

export function requestNewSession(): boolean {
  if(detachedChatId())return false;
  const action = {type:"new" as const, requestId:`new-${globalThis.crypto.randomUUID()}`};
  store.setState({pendingAction: action, openMenuId: null});
  pendingDispatch = "queued";
  pendingNeedsRecovery = false;
  expectedSessionChange = action;
  return dispatchPendingAction();
}

/**
 * Admit session snapshots without allowing a late request for the previous
 * chat to repaint the Deck after a switch/new-chat acknowledgement.
 */
export function acceptIncomingSession(id: string, currentId: string | null, navigation?: ChatNavigationOutcome): boolean {
  const nextId = String(id || "");
  if(detachedChatId() && nextId!==detachedChatId())return false;
  const current = String(currentId || "");
  if (!nextId) return false;
  const expected = expectedSessionChange;
  if (navigation) {
    if (expected?.type === "new") {
      if (navigation.request_id !== expected.requestId || navigation.status !== "created" || navigation.effective_id !== nextId) return false;
      finishNavigation();return true;
    }
    if (expected?.type !== "switch" || navigation.request_id !== expected.requestId
      || navigation.requested_id !== expected.id || navigation.effective_id !== nextId
      || navigation.status === "rejected" || (navigation.status === "switched" && nextId !== expected.id)) return false;
    finishNavigation();
    if (navigation.status === "fallback") notify("That conversation is no longer available. An available chat is open.");
    return true;
  }
  if (!expected) return !current || nextId === current;
  // Same-session refreshes remain safe while navigation is outstanding.
  if (current && nextId === current) return true;
  const matches = expected.type === "fallback";
  if (!matches) return false;
  finishNavigation();
  return true;
}

function finishNavigation(): void {
  expectedSessionChange = null;
  pendingDispatch = null;
  pendingNeedsRecovery = false;
  if (store.getState().pendingAction) {
    store.setState({pendingAction: null});
  }
}

export function noteDisplayedSession(id: string | null): void {
  if (id && id !== store.getState().displayedSessionId) stopSpeech();
  const nextId = String(id || "") || null;
  const state = store.getState();
  if (state.displayedSessionId === nextId) return;
  store.setState({displayedSessionId: nextId, openMenuId: null});
}

export function setSessionSearchQuery(query: string): void {
  const value = String(query || "");
  store.setState({
    searchQuery: value,
    // `null` is the pending state. Never render hits from the previous query
    // while the debounce or backend response for this query is outstanding.
    searchResults: null,
  });
  if (searchTimer) clearTimeout(searchTimer);
  if (!value.trim()) return;
  searchTimer = setTimeout(() => {
    send({type: "chat:search", query: value.trim(), limit: 20});
  }, 220);
}

export function setOpenSessionMenu(id: string | null): void {
  store.setState({openMenuId: id});
}

export function renameSession(id: string, title: string): boolean {
  const clean = title.trim();
  if (!clean) return false;
  setOpenSessionMenu(null);
  return send({type: "chat:session:rename", id, title: clean});
}

export function setSessionPinned(id: string, value: boolean): boolean {
  setOpenSessionMenu(null);
  return send({type: "chat:session:pin", id, value});
}

export function setSessionArchived(id: string, value: boolean): boolean {
  setOpenSessionMenu(null);
  return send({type: "chat:session:archive", id, value});
}

export function deleteSession(id: string): boolean {
  const state = store.getState();
  if (state.workingSessionIds.includes(id)) {
    notify("Wait for VARIANT-1 to finish before deleting this chat");
    return false;
  }
  if(!canClosePreviewTabs(getPreviewState().tabs.filter(tab=>tab.ownerChatId===id).map(tab=>tab.id)))return false;
  setOpenSessionMenu(null);
  const deletingDisplayed = id === state.displayedSessionId;
  if (deletingDisplayed) expectedSessionChange = {type: "fallback"};
  const ok = send({type: "chat:session:delete", id});
  if (!ok && deletingDisplayed) expectedSessionChange = null;
  return ok;
}

export function ingestSessions(message: Record<string, unknown>): void {
  const type = String(message.type || "");
  const state = store.getState();
  if(type==="chat:session:deleted") {
    const id=String(message.session_id || message.id || "");if(!id)return;
    releaseDeletedChat(id);
    store.setState({items:state.items.filter(row=>row.id!==id),searchResults:state.searchResults?.filter(row=>row.id!==id) || null,openMenuId:state.openMenuId===id?null:state.openMenuId});
    return;
  }
  if (type === "chat:new:result") {
    const pending = state.pendingAction;
    if (message.status !== "rejected" || pending?.type !== "new" || message.request_id !== pending.requestId) return;
    finishNavigation();const error=String(message.error || "Could not create that conversation");
    store.setState({error,loading:false});notify(error);return;
  }
  if (type === "chat:switch:result") {
    const pending = state.pendingAction;
    if (message.status !== "rejected" || pending?.type !== "switch"
      || message.request_id !== pending.requestId || message.requested_id !== pending.id) return;
    finishNavigation();
    const error = typeof message.error === "string" ? message.error : "Could not open that conversation";
    store.setState({error, loading: false});
    notify(error);
    return;
  }
  if (type === "chat:sessions") {
    const items = Array.isArray(message.items)
      ? message.items.map(parseSession).filter((row): row is SessionSummary => !!row)
      : [];
    const activeSessionId = String(message.active_id || "") || null;
    store.setState({
      connected: true,
      loading: false,
      items,
      activeSessionId,
      error: "",
    });
    if (state.pendingAction && pendingNeedsRecovery) {
      pendingNeedsRecovery = false;
      // New and switch are idempotent by request ID/target. A shared default
      // is not evidence of which navigation this window requested.
      expectedSessionChange = state.pendingAction;
      pendingDispatch = "queued";dispatchPendingAction();
    }
    if (!state.displayedSessionId && activeSessionId && !state.pendingAction) {
      send({type: "chat:session:get", id: activeSessionId});
    } else if (
      state.displayedSessionId
      && !state.pendingAction
      && !items.some(item => item.id === state.displayedSessionId)
    ) {
      // The displayed session was deleted (possibly by another window). The
      // backend's generic get now legitimately returns a different fallback.
      expectedSessionChange = {type: "fallback"};
      if (!send({type: "chat:session:get"})) expectedSessionChange = null;
    }
    return;
  }
  if (type === "chat:search:results") {
    const query = String(message.query || "");
    if (query.trim() !== state.searchQuery.trim()) return;
    const results = Array.isArray(message.items)
      ? message.items.map(parseSearchHit).filter(
        (row): row is SessionSearchHit => !!row,
      )
      : [];
    store.setState({searchResults: results});
    return;
  }
  if (type === "chat:session:error") {
    store.setState({
      loading: false,
      error: String(message.error || "Could not update session"),
    });
  }
}

export function getSessionState(): SessionState {
  return store.getState();
}
export const subscribeSessions = store.subscribe;

export function useSessionState(): SessionState {
  return store.useStore();
}

export function __resetSessionStoreForTests(): void {
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = null;
  store.setContext(null);
  pendingDispatch = null;
  pendingNeedsRecovery = false;
  expectedSessionChange = null;
  store.replaceState(initialState());
}
