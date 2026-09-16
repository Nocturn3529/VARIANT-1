import type {WsCommand} from "./protocol";
import {createModuleStore} from "./state/createModuleStore";
import type {RuntimeContext} from "./types";

export type SessionContextCategory = Readonly<{
  id: string;
  label: string;
  tokens: number;
  percent: number;
}>;

export type ComposerModelOption = Readonly<{
  id: string;
  label: string;
  detail: string;
  vision: boolean;
  selectable: boolean;
  reasoningEfforts: string[];
}>;

export type ComposerModelProvider = Readonly<{
  id: string;
  name: string;
  mode: "local" | "cloud";
  description: string;
  models: ComposerModelOption[];
  supportsReasoning: boolean;
  supportsVision: boolean;
  discovery: string;
  warning: string;
}>;

export type SessionContextState = Readonly<{
  connected: boolean;
  sessionId: string;
  status: "empty" | "ready";
  capturedAt: number | null;
  route: "local" | "cloud" | "";
  provider: string;
  model: string;
  reasoningEffort: string;
  reasoningEfforts: string[];
  measurement: string;
  categoryMeasurement: string;
  usedTokens: number;
  contextLimitTokens: number | null;
  availableTokens: number | null;
  outputReserveTokens: number;
  cachedInputTokens: number;
  percentUsed: number;
  categories: SessionContextCategory[];
  modelOptionsStatus: "idle" | "loading" | "ready" | "error";
  modelOptionsError: string;
  modelOptionsRequestId: string;
  modelProviders: ComposerModelProvider[];
  settingsPending: Readonly<{requestId:string;operation:"mode:set"|"reasoning:effort:set";label:string;checking:boolean}> | null;
  settingsError: string;
}>;

const CATEGORY_ORDER = [
  ["messages", "Messages"],
  ["tools", "Tools"],
  ["skills", "Skills"],
  ["mcps", "MCPs"],
  ["plugins", "Plugins"],
  ["memory", "Memory"],
  ["other", "Other"],
] as const;

const store = createModuleStore<SessionContextState>({
  initialState: emptyState(""),
});
const cache = new Map<string, SessionContextState>();
const settingsTimers = new Map<string,ReturnType<typeof setTimeout>>();
const settingsChecks = new Map<string,{requestId:string;settingRequestId:string}>();
const modelTimers = new Map<string,ReturnType<typeof setTimeout>>();

function number(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.max(0, parsed) : 0;
}

function optionalNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  return number(value);
}

function emptyState(sessionId: string): SessionContextState {
  return {
    connected: false,
    sessionId,
    status: "empty",
    capturedAt: null,
    route: "",
    provider: "",
    model: "",
    reasoningEffort: "",
    reasoningEfforts: [],
    measurement: "pending",
    categoryMeasurement: "estimated",
    usedTokens: 0,
    contextLimitTokens: null,
    availableTokens: null,
    outputReserveTokens: 0,
    cachedInputTokens: 0,
    percentUsed: 0,
    categories: CATEGORY_ORDER.map(([id, label]) => ({
      id, label, tokens: 0, percent: 0,
    })),
    modelOptionsStatus: "idle",
    modelOptionsError: "",
    modelOptionsRequestId: "",
    modelProviders: [],
    settingsPending: null,
    settingsError: "",
  };
}

function parseSnapshot(message: Record<string, unknown>): SessionContextState | null {
  const sessionId = String(message.session_id || "").trim();
  if (!sessionId) return null;
  const byId = new Map<string, SessionContextCategory>();
  for (const value of Array.isArray(message.categories) ? message.categories : []) {
    if (!value || typeof value !== "object") continue;
    const row = value as Record<string, unknown>;
    const id = String(row.id || "").trim().toLowerCase();
    if (!id) continue;
    byId.set(id, {
      id,
      label: String(row.label || id),
      tokens: Math.round(number(row.tokens)),
      percent: Math.min(100, number(row.percent)),
    });
  }
  const existing = cache.get(sessionId) || (
    store.getState().sessionId === sessionId ? store.getState() : emptyState(sessionId)
  );
  return {
    connected: store.getState().connected,
    sessionId,
    status: message.status === "ready" ? "ready" : "empty",
    capturedAt: optionalNumber(message.captured_at),
    route: message.route === "cloud" ? "cloud" : message.route === "local" ? "local" : "",
    provider: String(message.provider || ""),
    model: String(message.model || ""),
    reasoningEffort: String(message.reasoning_effort || ""),
    reasoningEfforts: (Array.isArray(message.reasoning_efforts)
      ? message.reasoning_efforts
      : []).map(value => String(value || "")).filter(Boolean),
    measurement: String(message.measurement || "estimated"),
    categoryMeasurement: String(message.category_measurement || "estimated"),
    usedTokens: Math.round(number(message.used_tokens)),
    contextLimitTokens: optionalNumber(message.context_limit_tokens),
    availableTokens: optionalNumber(message.available_tokens),
    outputReserveTokens: Math.round(number(message.output_reserve_tokens)),
    cachedInputTokens: Math.round(number(message.cached_input_tokens)),
    percentUsed: Math.min(100, number(message.percent_used)),
    categories: CATEGORY_ORDER.map(([id, label]) => byId.get(id) || {
      id, label, tokens: 0, percent: 0,
    }),
    modelOptionsStatus: existing.modelOptionsStatus,
    modelOptionsError: existing.modelOptionsError,
    modelOptionsRequestId: existing.modelOptionsRequestId,
    modelProviders: existing.modelProviders,
    settingsPending: existing.settingsPending,
    settingsError: existing.settingsError,
  };
}

function parseModelProviders(value: unknown): ComposerModelProvider[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap(item => {
    if (!item || typeof item !== "object") return [];
    const row = item as Record<string, unknown>;
    const id = String(row.id || "").trim();
    const mode = row.mode === "local" ? "local" : "cloud";
    if (!id) return [];
    const models = (Array.isArray(row.models) ? row.models : []).flatMap(value => {
      if (typeof value === "string") {
        const model = value.trim();
        return model ? [{
          id: model, label: model, detail: "", vision: false,
          selectable: true, reasoningEfforts: [],
        }] : [];
      }
      if (!value || typeof value !== "object") return [];
      const model = value as Record<string, unknown>;
      const modelId = String(model.id || "").trim();
      if (!modelId) return [];
      return [{
        id: modelId,
        label: String(model.label || modelId),
        detail: String(model.detail || ""),
        vision: !!model.vision,
        selectable: model.selectable !== false,
        reasoningEfforts: (Array.isArray(model.reasoning_efforts)
          ? model.reasoning_efforts : []).map(String).filter(Boolean),
      } satisfies ComposerModelOption];
    });
    return [{
      id,
      name: String(row.name || id),
      mode,
      description: String(row.description || ""),
      models,
      supportsReasoning: !!row.supports_reasoning,
      supportsVision: !!row.supports_vision,
      discovery: String(row.discovery || ""),
      warning: String(row.warning || ""),
    } satisfies ComposerModelProvider];
  });
}

export function setSessionContextContext(next: RuntimeContext): void {
  store.setContext(next);
}

export function setSessionContextConnection(status: string): void {
  const connected = status === "connected";
  const state = store.getState();
  if (connected === state.connected) return;
  store.setState({connected});
  if(!connected) {
    for(const timer of modelTimers.values()) clearTimeout(timer);
    modelTimers.clear();
    for(const timer of settingsTimers.values()) clearTimeout(timer);
    settingsTimers.clear();settingsChecks.clear();
    for(const [id,value] of cache) if(value.settingsPending) updateSettingsState(id,{settingsPending:{...value.settingsPending,checking:true}});
  } else {
    for(const [id,value] of cache) if(value.settingsPending) checkSessionSettings(id);
  }
}

function updateSettingsState(id:string,partial:Partial<SessionContextState>) {
  const state=getContextForSession(id),next={...state,...partial};cache.set(id,next);
  if(store.getState().sessionId===id) store.replaceState(next);
}
function clearSettingsTimer(id:string) {clearTimeout(settingsTimers.get(id));settingsTimers.delete(id);}
function checkSessionSettings(id:string) {
  clearSettingsTimer(id);
  const pending=getContextForSession(id).settingsPending;
  if(!pending || !store.getContext()?.isOpen?.()) return;
  const requestId="settings-check-"+globalThis.crypto.randomUUID();
  settingsChecks.set(id,{requestId,settingRequestId:pending.requestId});
  updateSettingsState(id,{settingsPending:{...pending,checking:true}});
  store.send({type:"session:settings:get",session_id:id,request_id:requestId});
  settingsTimers.set(id,setTimeout(()=>checkSessionSettings(id),5000));
}

/** A setting remains pending until its scoped ack or authoritative status read. */
export function changeSessionSettings(id:string,command:WsCommand & {type:"mode:set"|"reasoning:effort:set"},label:string):boolean {
  const current=getContextForSession(id);
  if(!id || current.settingsPending || store.getContext()?.isOpen?.()===false) return false;
  const requestId="composer-setting-"+globalThis.crypto.randomUUID();
  updateSettingsState(id,{settingsPending:{requestId,operation:command.type,label,checking:false},settingsError:""});
  if(!store.send({...command,id,request_id:requestId})) {
    updateSettingsState(id,{settingsPending:null,settingsError:"Could not send this change. Reconnect and try again."});return false;
  }
  settingsTimers.set(id,setTimeout(()=>checkSessionSettings(id),15000));
  return true;
}

function settingRoute(value:unknown,previous:SessionContextState):Partial<SessionContextState> {
  if(!value || typeof value!=="object")return {};
  const row=value as Record<string,unknown>,mode=row.mode || row.route;
  return {route:mode==="local"||mode==="cloud" ? mode : previous.route,
    provider:typeof row.provider==="string" ? row.provider : previous.provider,
    model:typeof row.model==="string" ? row.model : previous.model,
    reasoningEffort:typeof row.reasoning_effort==="string" ? row.reasoning_effort : previous.reasoningEffort};
}

export function requestSessionContext(sessionId: string | null, activate = true): void {
  const id = String(sessionId || "").trim();
  let state = store.getState();
  if (activate && id !== state.sessionId) {
    const cached = cache.get(id);
    state = cached
      ? {...cached, connected: state.connected}
      : {...emptyState(id), connected: state.connected};
    store.replaceState(state);
  }
  const context = store.getContext();
  if (id && (state.connected || context?.isOpen?.())) {
    store.send({type: "chat:context", id} satisfies WsCommand);
  }
}

export function requestModelOptions(
  sessionId: string | null,
  {refresh = false}: {refresh?: boolean} = {},
): string {
  const id = String(sessionId || "").trim();
  if (!id) return "";
  requestSessionContext(id);
  const requestId = `model-options-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 9)}`;
  const state = store.getState();
  clearTimeout(modelTimers.get(id));
  modelTimers.set(id,setTimeout(()=>{
    const value=getContextForSession(id);
    if(value.modelOptionsRequestId===requestId && value.modelOptionsStatus==="loading") updateSettingsState(id,{modelOptionsStatus:"error",modelOptionsError:"Loading models is taking longer than expected. Retry or choose a cached model."});
    modelTimers.delete(id);
  },15000));
  store.setState({
    modelOptionsStatus: "loading",
    modelOptionsError: "",
    modelOptionsRequestId: requestId,
  });
  if (!store.send({
    type: "model:options",
    id,
    request_id: requestId,
    refresh,
  } satisfies WsCommand)) {
    clearTimeout(modelTimers.get(id));modelTimers.delete(id);
    store.setState({
      modelOptionsStatus: "error",
      modelOptionsError: "VARIANT-1 is offline.",
      modelOptionsRequestId: "",
    });
    return "";
  }
  cache.set(id, {...state,
    modelOptionsStatus: "loading",
    modelOptionsError: "",
    modelOptionsRequestId: requestId,
  });
  return requestId;
}

export function ingestSessionContext(message: Record<string, unknown>): void {
  if(message.type==="session:settings:ack" || message.type==="session:settings:snapshot") {
    const id=String(message.session_id || ""),state=getContextForSession(id),pending=state.settingsPending;
    if(!pending)return;
    if(message.type==="session:settings:ack") {
      if(message.request_id!==pending.requestId || message.operation!==pending.operation || !["applied","rejected"].includes(String(message.status)))return;
    } else {
      const check=settingsChecks.get(id);
      if(!check || check.requestId!==message.request_id || check.settingRequestId!==pending.requestId)return;
      if(message.pending!==false) {clearSettingsTimer(id);settingsTimers.set(id,setTimeout(()=>checkSessionSettings(id),1000));return;}
    }
    clearSettingsTimer(id);settingsChecks.delete(id);
    updateSettingsState(id,{...settingRoute(message.route,state),settingsPending:null,settingsError:String(message.error || (message.status==="rejected" ? "This change could not be applied." : ""))});
    store.send({type:"chat:runtime:get",id});
    requestSessionContext(id,store.getState().sessionId===id);return;
  }
  if (message.type === "model:options" || message.type === "model:options:error") {
    const sessionId = String(message.session_id || "").trim();
    if (!sessionId) return;
    const existing = cache.get(sessionId) || (
      store.getState().sessionId === sessionId ? store.getState() : emptyState(sessionId)
    );
    const requestId = String(message.request_id || "");
    if (requestId && requestId !== existing.modelOptionsRequestId) return;
    clearTimeout(modelTimers.get(sessionId));modelTimers.delete(sessionId);
    const failed = message.type === "model:options:error";
    const next: SessionContextState = {
      ...existing,
      modelOptionsStatus: failed ? "error" : "ready",
      modelOptionsError: failed ? String(message.error || "Could not load the model catalog.") : "",
      modelOptionsRequestId: "",
      modelProviders: failed ? existing.modelProviders : parseModelProviders(message.providers),
    };
    cache.set(sessionId, next);
    if (store.getState().sessionId === sessionId) store.replaceState(next);
    return;
  }
  if (message.type !== "chat:context") return;
  const parsed = parseSnapshot(message);
  if (!parsed) return;
  cache.set(parsed.sessionId, parsed);
  if (parsed.sessionId !== store.getState().sessionId) return;
  store.replaceState(parsed);
}

export function getSessionContextState(): SessionContextState {
  return store.getState();
}
export function getContextForSession(id: string): SessionContextState {
  return store.getState().sessionId === id ? store.getState() : cache.get(id) || emptyState(id);
}

export function useSessionContextState(): SessionContextState {
  return store.useStore();
}

export function __resetSessionContextStoreForTests(): void {
  for(const timer of modelTimers.values()) clearTimeout(timer);
  modelTimers.clear();
  for(const timer of settingsTimers.values()) clearTimeout(timer);
  settingsTimers.clear();settingsChecks.clear();
  store.setContext(null);
  cache.clear();
  store.replaceState(emptyState(""));
}
