import type {RuntimeContext} from "./types";
import {createModuleStore} from "./state/createModuleStore";
import {asRecord, createRequestIdFactory} from "./state/storePrimitives";

export type ModelOperation = "get" | "search" | "files" | "download" | "cancel" | "activate" | "eject" | "delete";
export type InstalledModel = {id: string; name: string; path: string; size_bytes?: number; active: boolean; managed_download: boolean; vision?: boolean};
export type ModelJob = {id: string; repo: string; status: string; phase: string; done_bytes: number; total_bytes: number; error?: string};
export type ModelVariant = {label: string; paths: string[]; bytes: number; complete: boolean; projector: boolean};
type Snapshot = {revision: number; installed: InstalledModel[]; jobs: ModelJob[]; hardware: Record<string, unknown>; supported_actions: string[]; warning?: string};
export const modelJobFinished = (status: string) => ["done", "failed", "cancelled", "interrupted", "error"].includes(status);
type State = {
  connected: boolean; snapshot: Snapshot | null; pending: Partial<Record<ModelOperation, string>>;
  errors: Partial<Record<ModelOperation, string>>;
  results: {repo: string; downloads: number; gated: boolean}[];
  files: {repo: string; revision: string; variants: ModelVariant[]} | null;
};
const store = createModuleStore<State>({initialState: {connected: false, snapshot: null, pending: {}, errors: {}, results: [], files: null}});
const nextId = createRequestIdFactory("local-models-ui");
const timers = new Map<ModelOperation, ReturnType<typeof setTimeout>>();
export const useLocalModels = store.useStore;
export const getLocalModels = store.getState;
export const setLocalModelsContext = (context: RuntimeContext) => store.setContext(context);
const mutations: ModelOperation[] = ["download", "cancel", "activate", "eject", "delete"];
export const modelMutationPending = (state: State) => mutations.some(operation => !!state.pending[operation]);

function finish(operation: ModelOperation, error = "") {
  clearTimeout(timers.get(operation)); timers.delete(operation);
  const pending = {...store.getState().pending}; delete pending[operation];
  store.setState({pending, errors: {...store.getState().errors, [operation]: error}});
}
export function setLocalModelsConnection(status: string) {
  const connected = status === "connected";
  store.setConnected(connected);
  if (!connected) for (const operation of Object.keys(store.getState().pending) as ModelOperation[]) {
    finish(operation, "Connection interrupted. Refresh to check the result before trying again.");
  }
}
export function requestLocalModels(operation: ModelOperation, payload: Record<string, unknown> = {}): boolean {
  const state = store.getState();
  if (state.pending[operation] || (mutations.includes(operation) && modelMutationPending(state))) return false;
  if (!state.connected) {store.setState({errors: {...state.errors, [operation]: "Backend offline."}}); return false;}
  if (mutations.includes(operation) && !state.snapshot?.supported_actions.includes(operation)) return false;
  const id = nextId(operation);
  store.setState({pending: {...state.pending, [operation]: id}, errors: {...state.errors, [operation]: ""},
    ...(operation === "files" ? {files: null} : {}), ...(operation === "search" ? {results: []} : {})});
  timers.set(operation, setTimeout(() => {
    if (store.getState().pending[operation] === id) finish(operation, "No response received. Refresh to check the result before retrying.");
  }, operation === "activate" || operation === "eject" ? 180000 : 45000));
  if (!store.send({...payload, type: `local-models:${operation}`, request_id: id})) {finish(operation, "Request could not be sent."); return false;}
  return true;
}
export function refreshLocalModels() {return requestLocalModels("get");}
export function ingestLocalModels(message: Record<string, unknown>) {
  if (message.type !== "local-models:result") return;
  const operation = message.operation as ModelOperation;
  if (!store.getState().pending[operation] || store.getState().pending[operation] !== message.request_id) return;
  if (message.ok !== true) {finish(operation, String(asRecord(message.error).message || "The model operation failed.")); return;}
  const result = asRecord(message.result);
  if (operation === "get") {
    if (!Number.isSafeInteger(result.revision) || !Array.isArray(result.installed) || !Array.isArray(result.jobs) || !Array.isArray(result.supported_actions)) {
      finish(operation, "The model library response was incomplete."); return;
    }
    store.setState({snapshot: {...result, hardware: asRecord(result.hardware)} as Snapshot});
  } else if (operation === "search") {
    store.setState({results: Array.isArray(result.items) ? result.items.filter(item => typeof asRecord(item).repo === "string") as State["results"] : []});
  } else if (operation === "files") {
    if (typeof result.repo !== "string" || typeof result.revision !== "string" || !Array.isArray(result.variants)) {finish(operation, "Repository files could not be read."); return;}
    store.setState({files: result as State["files"]});
  }
  finish(operation);
  if (mutations.includes(operation)) {
    // A pre-mutation inventory response must not replace the refreshed result.
    if (store.getState().pending.get) finish("get");
    refreshLocalModels();
  }
}
