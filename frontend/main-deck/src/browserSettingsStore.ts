import type {RuntimeContext} from "./types";
import {createModuleStore} from "./state/createModuleStore";
import {asRecord, createRequestIdFactory} from "./state/storePrimitives";

export type BrowserSelection = Readonly<{mode: "embedded" | "managed" | "personal" | "cdp" | "cloud"; browser_id?: string; profile_id?: string; [key: string]: string | number | boolean | undefined}>;
export type BrowserField = {key: string; type: "string" | "number" | "boolean" | "select"; modes: BrowserSelection["mode"][]; min?: number; max?: number; options?: string[]};
export type BrowserRecording = {name: string; path: string; bytes: number; modified_at: number; session_id: string; complete: boolean};
const optionKeys = ["cdp_url", "cloud_provider", "project_id", "headed", "executable_path", "command_timeout_s", "click_timeout_s", "navigation_timeout_s", "dialog_policy", "record_sessions", "allow_private_urls", "evaluate_enabled"];
const modes = ["embedded", "managed", "personal", "cdp", "cloud"];
export type BrowserCatalog = Readonly<{
  default: {selection: BrowserSelection; revision: number};
  options?: {modes: BrowserSelection["mode"][]; defaults: Record<string, unknown>; fields: BrowserField[]};
  browsers: readonly {id: string; label: string; profiles: readonly {id: string; label: string; directory_name: string}[]}[];
}>;
export type BrowserReadiness = "idle" | "selection_required" | "login_required" | "profile_locked" | "connection_failed" | "connecting" | "ready" | "cancelled" | "unknown";
export type BrowserChatState = Readonly<{
  chat_id: string; state: BrowserReadiness; message: string; selection: BrowserSelection;
  selection_source: "default" | "chat"; revision: number; browser_session_id?: string;
  pending_operation_id?: string; actions: readonly string[];
}>;
type Pending = Readonly<{id: string; reply: string; chatId?: string; operationId?: string}>;
type BrowserSettingsState = Readonly<{
  connected: boolean; catalog: BrowserCatalog | null; chats: Readonly<Record<string, BrowserChatState>>;
  pending: Readonly<Record<string, Pending>>; errors: Readonly<Record<string, string>>;
  dismissed: Readonly<Record<string,string>>;
  recordings: Readonly<Record<string, BrowserRecording[]>>;
}>;
const initial: BrowserSettingsState = {connected: false, catalog: null, chats: {}, pending: {}, errors: {}, dismissed:{}, recordings:{}};
const store = createModuleStore<BrowserSettingsState>({initialState: initial});
const requestId = createRequestIdFactory("browser-ui");
const timers = new Map<string, ReturnType<typeof setTimeout>>();
let focusedChatId = "";

export const useBrowserSettings = store.useStore;
export const getBrowserSettings = store.getState;
export const setBrowserSettingsContext = (context: RuntimeContext) => store.setContext(context);
export const browserSelectionKey = (selection: BrowserSelection) => JSON.stringify(Object.keys(selection).filter(key => selection[key] !== undefined && !(selection[key] === "" && ["browser_id", "profile_id", "executable_path", "project_id"].includes(key))).sort().map(key => [key, selection[key]]));
export const browserSelectionRequestKey = (chatId?: string) => chatId ? `selection:${chatId}` : "selection:default";
export function browserNoticeKey(chatId:string):string {
  const state=store.getState(),chat=state.chats[chatId];
  return JSON.stringify([chat?.revision,chat?.state,chat?.pending_operation_id,chat?.message,state.errors[`state:${chatId}`]]);
}
export function dismissBrowserNotice(chatId:string):void {
  store.setState({dismissed:{...store.getState().dismissed,[chatId]:browserNoticeKey(chatId)}});
}

function selectionFrom(value: unknown): BrowserSelection | null {
  const row = asRecord(value);
  if (!modes.includes(String(row.mode))) return null;
  return {...Object.fromEntries(optionKeys.filter(key => ["string", "number", "boolean"].includes(typeof row[key])).map(key => [key, row[key]])), mode: row.mode as BrowserSelection["mode"],
    ...(typeof row.browser_id === "string" ? {browser_id: row.browser_id} : {}),
    ...(typeof row.profile_id === "string" ? {profile_id: row.profile_id} : {})};
}
const revisionFrom = (value: unknown) => typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : -1;

function finish(key: string, error = ""): void {
  const pending = {...store.getState().pending};
  clearTimeout(timers.get(key)); timers.delete(key); delete pending[key];
  store.setState({pending, errors: {...store.getState().errors, [key]: error}});
}

function request(key: string, reply: string, payload: Record<string, unknown> & {type: string}, chatId?: string, operationId?: string): boolean {
  const state = store.getState();
  if (state.pending[key]) return false;
  if (!state.connected) { store.setState({errors: {...state.errors, [key]: "Backend disconnected. Reconnect before changing the browser."}}); return false; }
  const id = requestId(key);
  store.setState({pending: {...state.pending, [key]: {id, reply, chatId, operationId}}, errors: {...state.errors, [key]: ""}});
  timers.set(key, setTimeout(() => {
    if (store.getState().pending[key]?.id === id) finish(key, "No acknowledgement received. Refresh the browser state before trying again.");
  }, 15000));
  if (!store.send({...payload, request_id: id})) { finish(key, "The browser request could not be sent."); return false; }
  return true;
}

export function refreshBrowserSettings(): void { request("catalog", "browser:settings", {type: "browser:settings:get"}); }
export function refreshBrowserRecordings(chatId: string): void {
  if (chatId) request(`recordings:${chatId}`, "browser:recordings", {type: "browser:recordings:get", chat_id: chatId}, chatId);
}
export function refreshBrowserChat(chatId: string): void {
  if (chatId) request(`state:${chatId}`, "browser:state", {type: "browser:state:get", chat_id: chatId}, chatId);
}
export function observeBrowserChat(chatId: string | null): void {
  focusedChatId = chatId || "";
  if (focusedChatId && store.getState().connected) refreshBrowserChat(focusedChatId);
}
export function setBrowserSettingsConnection(status: string): void {
  const connected = status === "connected";
  if (store.getState().connected === connected) return;
  store.setConnected(connected);
  if (!connected) for (const key of Object.keys(store.getState().pending)) finish(key, "Connection interrupted. Refresh to confirm the current browser state.");
  else if (focusedChatId) refreshBrowserChat(focusedChatId);
}

export function saveBrowserSelection(selection: BrowserSelection, expectedRevision: number, chatId?: string): boolean {
  const key = browserSelectionRequestKey(chatId);
  const profile = store.getState().catalog?.browsers.find(item => item.id === selection.browser_id)?.profiles.find(item => item.id === selection.profile_id);
  if (selection.mode === "personal" && !profile) {
    store.setState({errors: {...store.getState().errors, [key]: "Choose an available browser and profile explicitly."}}); return false;
  }
  return request(key, "browser:selection:result", {type: "browser:selection:set", scope: chatId ? "chat" : "default",
    ...(chatId ? {chat_id: chatId} : {}), selection, expected_revision: expectedRevision}, chatId);
}

export function resolveBrowserOperation(chatId: string, action: "retry" | "cancel"): boolean {
  const chat = store.getState().chats[chatId];
  if (!chat?.pending_operation_id || !chat.actions.includes(action)) return false;
  return request(`resolve:${chatId}`, "browser:resolve:result", {type: "browser:resolve", chat_id: chatId,
    operation_id: chat.pending_operation_id, action}, chatId, chat.pending_operation_id);
}

export function ingestBrowserSettings(message: Record<string, unknown>): void {
  const type = String(message.type || "");
  const id = String(message.request_id || "");
  const entry = Object.entries(store.getState().pending).find(([, pending]) => pending.id === id && pending.reply === type);
  if (id && !entry) return; // Late/disconnected replies cannot acknowledge a newer edit.
  const chatId = String(message.chat_id || "");
  if (entry?.[1].chatId && entry[1].chatId !== chatId) return;
  if (entry?.[1].operationId && entry[1].operationId !== message.operation_id) return;
  if (entry && message.error && (type === "browser:settings" || type === "browser:state" || type === "browser:recordings")) {
    finish(entry[0], String(asRecord(message.error).message || "The browser state could not be read.")); return;
  }
  if (type === "browser:recordings" && entry) {
    const items = (Array.isArray(message.items) ? message.items : []).filter(item => typeof asRecord(item).path === "string") as BrowserRecording[];
    store.setState({recordings: {...store.getState().recordings, [chatId]: items}}); finish(entry[0]); return;
  }
  const selection = selectionFrom(message.selection);
  const revision = revisionFrom(message.revision);
  if (type === "browser:settings" && entry) {
    const defaults = asRecord(message.default);
    const selected = selectionFrom(defaults.selection);
    const version = revisionFrom(defaults.revision);
    if (!selected || version < 0 || !Array.isArray(message.browsers)) { finish(entry[0], "Browser settings could not be read."); return; }
    const browsers = message.browsers.flatMap(value => {
      const row = asRecord(value);
      if (typeof row.id !== "string" || typeof row.label !== "string") return [];
      return [{id: row.id, label: row.label, profiles: (Array.isArray(row.profiles) ? row.profiles : []).flatMap(value => {
        const profile = asRecord(value);
        return typeof profile.id === "string" && typeof profile.label === "string"
          ? [{id: profile.id, label: profile.label, directory_name: String(profile.directory_name || "")}] : [];
      })}];
    });
    const prior = store.getState().catalog?.default;
    const rawOptions = asRecord(message.catalog);
    const options = Array.isArray(rawOptions.modes) && Array.isArray(rawOptions.fields) ? {
      modes: rawOptions.modes.filter(mode => modes.includes(String(mode))) as BrowserSelection["mode"][], defaults: asRecord(rawOptions.defaults),
      fields: rawOptions.fields.filter(value => {const field = asRecord(value); return optionKeys.includes(String(field.key)) && ["string", "number", "boolean", "select"].includes(String(field.type)) && Array.isArray(field.modes);}) as BrowserField[],
    } : undefined;
    store.setState({catalog: {browsers, options, default: prior && prior.revision > version ? prior : {selection: selected, revision: version}}});
    finish(entry[0]);
  } else if (type === "browser:state" && chatId) {
    if (!selection || revision < 0) { if (entry) finish(entry[0], "Browser readiness could not be read."); return; }
    const prior = store.getState().chats[chatId];
    if (!prior || revision >= prior.revision) {
      const known = ["idle", "selection_required", "login_required", "profile_locked", "connection_failed", "connecting", "ready", "cancelled"];
      const state = known.includes(String(message.state)) ? message.state as BrowserReadiness : "unknown";
      const chat: BrowserChatState = {chat_id: chatId, state, message: String(message.message || ""), selection,
        selection_source: message.selection_source === "chat" ? "chat" : "default", revision,
        browser_session_id: typeof message.browser_session_id === "string" ? message.browser_session_id : undefined,
        pending_operation_id: typeof message.pending_operation_id === "string" ? message.pending_operation_id : undefined,
        actions: state === "unknown" ? [] : (Array.isArray(message.actions) ? message.actions : []).filter((item): item is string => ["select_profile", "retry", "cancel"].includes(String(item)))};
      store.setState({chats: {...store.getState().chats, [chatId]: chat},errors:{...store.getState().errors,[`state:${chatId}`]:""}});
    }
    if (entry) finish(entry[0]);
  } else if ((type === "browser:selection:result" || type === "browser:resolve:result") && entry) {
    if (type === "browser:selection:result" && message.scope !== (entry[1].chatId ? "chat" : "default")) return;
    if (message.ok !== true) {
      finish(entry[0], String(asRecord(message.error).message || asRecord(message.error).code || "The browser request was rejected.")); return;
    }
    if (type === "browser:selection:result") {
      if (!selection || revision < 0) { finish(entry[0], "The browser choice was not acknowledged correctly. Refresh to confirm it."); return; }
      const state = store.getState();
      if (chatId && state.chats[chatId] && revision >= state.chats[chatId].revision) {
        store.setState({chats: {...state.chats, [chatId]: {...state.chats[chatId], selection, revision, selection_source: "chat"}}});
      } else if (!chatId && state.catalog && revision >= state.catalog.default.revision) {
        store.setState({catalog: {...state.catalog, default: {selection, revision}}});
      }
    }
    finish(entry[0]);
    if (chatId) refreshBrowserChat(chatId);
    else if (type === "browser:selection:result" && focusedChatId) refreshBrowserChat(focusedChatId);
  }
}

export function __resetBrowserSettingsForTests(): void {
  for (const timer of timers.values()) clearTimeout(timer);
  timers.clear(); focusedChatId = ""; store.setContext(null); store.replaceState(initial);
}
