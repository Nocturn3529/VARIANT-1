import {createModuleStore} from "./state/createModuleStore";
import type {RuntimeContext} from "./types";

export type KernelInventoryRow = Readonly<{
  chatId: string; title: string; generation: number; state: string; busy: boolean;
  ageSeconds: number | null; idleSeconds: number | null;
  memoryBytes: number | null; treeMemoryBytes: number | null; processes: number | null;
  treeComplete: boolean; measurementSource: string; sampledAt: number | null;
}>;
type InventoryState = {connected: boolean; items: readonly KernelInventoryRow[];
  requestId: string; closing: Readonly<Record<string, string>>; error: string};
const store = createModuleStore<InventoryState>({initialState: {
  connected: false, items: [], requestId: "", closing: {}, error: ""}});
const timers = new Map<string, ReturnType<typeof setTimeout>>();
const record = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const amount = (value: unknown): number | null => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
function finish(id: string) { clearTimeout(timers.get(id)); timers.delete(id); }
export const useKernelInventory = store.useStore;
export const getKernelInventory = store.getState;
export const setKernelInventoryContext = (context: RuntimeContext) => store.setContext(context);
export function setKernelInventoryConnection(status: string) {
  const connected = status === "connected";
  if (!connected) {
    for (const id of timers.keys()) finish(id);
    store.setState({requestId: "", closing: {}});
  }
  store.setConnected(connected);
}
export function refreshKernelInventory(): boolean {
  if (!store.getState().connected || store.getState().requestId) return false;
  const requestId = crypto.randomUUID();
  store.setState({requestId, error: ""});
  timers.set(requestId, setTimeout(() => {
    finish(requestId);
    if (store.getState().requestId === requestId) store.setState({requestId: "", error: "No kernel inventory reply received. Refresh to try again."});
  }, 15000));
  if (store.send({type: "kernel:inventory:get", request_id: requestId})) return true;
  finish(requestId); store.setState({requestId: "", error: "Backend disconnected"}); return false;
}
export function releaseKernel(chatId: string, expectedGeneration: number): boolean {
  const state = store.getState(), row = state.items.find(item => item.chatId === chatId);
  if (!state.connected || !row || row.generation !== expectedGeneration || row.busy || row.state !== "ready" || state.closing[chatId]) return false;
  const requestId = crypto.randomUUID();
  store.setState({closing: {...state.closing, [chatId]: requestId}, error: ""});
  timers.set(requestId, setTimeout(() => {
    finish(requestId);
    if (store.getState().closing[chatId] !== requestId) return;
    const closing = {...store.getState().closing}; delete closing[chatId];
    store.setState({closing, error: "Kernel close was not acknowledged. Refresh before retrying."});
  }, 15000));
  if (store.send({type: "kernel:release", chat_id: chatId, generation: row.generation, request_id: requestId})) return true;
  finish(requestId);
  const closing = {...store.getState().closing}; delete closing[chatId];
  store.setState({closing, error: "Backend disconnected"}); return false;
}
export function ingestKernelInventory(message: Record<string, unknown>): void {
  const state = store.getState(), requestId = String(message.request_id || "");
  if (message.type === "kernel:inventory:result") {
    if (!requestId || state.requestId !== requestId || !state.connected) return;
    finish(requestId);
    if (!Array.isArray(message.items)) {store.setState({requestId: "", error: "Invalid kernel inventory reply"}); return;}
    const items = message.items.flatMap(value => {
      const row = record(value), resources = record(row.resources), process = record(resources.process), tree = record(resources.tree);
      if (typeof row.chat_id !== "string" || !row.chat_id || typeof row.generation !== "number" || !Number.isSafeInteger(row.generation) || row.generation < 1 || typeof row.state !== "string") return [];
      return [{chatId: row.chat_id, title: String(row.title || row.chat_id), generation: row.generation,
        state: row.state, busy: row.busy === true, ageSeconds: amount(row.age_s), idleSeconds: amount(row.idle_s),
        memoryBytes: amount(process.rss_bytes), treeMemoryBytes: amount(tree.rss_bytes), processes: amount(tree.processes),
        treeComplete: tree.complete === true, measurementSource: String(resources.measurement_source || "unavailable"), sampledAt: amount(resources.sampled_at)}];
    });
    store.setState({items, requestId: ""});
  } else if (message.type === "kernel:release:result") {
    const chatId = String(message.chat_id || "");
    if (!requestId || state.closing[chatId] !== requestId || !state.connected) return;
    finish(requestId);
    const closing = {...state.closing}; delete closing[chatId];
    const error = message.ok === true ? "" : String(message.error || "Kernel close failed");
    store.setState({closing});
    // Do not remove an observation optimistically: refresh authoritative state.
    refreshKernelInventory();
    store.setState({error});
  }
}
