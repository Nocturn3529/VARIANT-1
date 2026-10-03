import {createModuleStore} from "./state/createModuleStore";
import type {RuntimeContext} from "./types";

export type KernelCurrentCell = Readonly<{executionId: string; runId: string; outerToolCallId: string; startedAt: number | null; label: string}>;
export type KernelLastCell = Readonly<{executionId: string; status: string; durationMs: number | null; errorCode: string; completedAt: number | null}>;
export type KernelExit = Readonly<{reason: string; at: number | null; generation: number}>;
export type KernelInventoryRow = Readonly<{
  chatId: string; title: string; generation: number; state: string; busy: boolean;
  ageSeconds: number | null; idleSeconds: number | null;
  memoryBytes: number | null; treeMemoryBytes: number | null; processes: number | null;
  treeComplete: boolean; measurementSource: string; sampledAt: number | null;
  /** Interpreter CPU between the last two samples, percent of one core. */
  cpuPercent: number | null;
  currentCell: KernelCurrentCell | null; queuedCells: number;
  lastCell: KernelLastCell | null; lastExit: KernelExit | null;
}>;
export type ChildrenCapacity = Readonly<{mode: string; maxActive: number; maxAdmitted: number; maxDepth: number; active: number; admitted: number}>;
export type KernelHistoryItem = Readonly<{sequence: number; executionId: string; status: string; startedAt: number | null; completedAt: number | null; durationMs: number | null; errorCode: string; label: string}>;
type HistoryState = Readonly<{requestId: string; items: readonly KernelHistoryItem[]; error: string}>;
export type KernelActionKind = "interrupt" | "restart";
type InventoryState = {connected: boolean; items: readonly KernelInventoryRow[];
  requestId: string; closing: Readonly<Record<string, string>>; error: string;
  capacity: ChildrenCapacity | null;
  history: Readonly<Record<string, HistoryState>>;
  actions: Readonly<Record<string, Readonly<{kind: KernelActionKind; requestId: string}>>>};
const store = createModuleStore<InventoryState>({initialState: {
  connected: false, items: [], requestId: "", closing: {}, error: "", capacity: null, history: {}, actions: {}}});
const timers = new Map<string, ReturnType<typeof setTimeout>>();
/** Previous cumulative CPU sample per chat generation, for CPU% deltas. */
const cpuSamples = new Map<string, {seconds: number; at: number}>();
const record = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const amount = (value: unknown): number | null => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
const text = (value: unknown): string => typeof value === "string" ? value : "";
function finish(id: string) { clearTimeout(timers.get(id)); timers.delete(id); }
export const useKernelInventory = store.useStore;
export const getKernelInventory = store.getState;
export const canReleaseKernel = (row: KernelInventoryRow) => !row.busy && ["ready", "unhealthy", "close_failed"].includes(row.state);
export const setKernelInventoryContext = (context: RuntimeContext) => store.setContext(context);
export function setKernelInventoryConnection(status: string) {
  const connected = status === "connected";
  if (!connected) {
    for (const id of timers.keys()) finish(id);
    // Their replies can no longer arrive, and a pending history id refuses
    // every later load for that chat, so release it with the other requests.
    const history = Object.fromEntries(Object.entries(store.getState().history)
      .map(([chatId, entry]) => [chatId, entry.requestId ? {...entry, requestId: ""} : entry]));
    store.setState({requestId: "", closing: {}, actions: {}, history});
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
  if (!state.connected || !row || row.generation !== expectedGeneration || !canReleaseKernel(row) || state.closing[chatId]) return false;
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

/** Interrupt the running cell, or restart the kernel, of any chat. */
export function kernelCommand(chatId: string, kind: KernelActionKind): boolean {
  const state = store.getState();
  if (!state.connected || !chatId || state.actions[chatId]) return false;
  const requestId = crypto.randomUUID();
  store.setState({actions: {...state.actions, [chatId]: {kind, requestId}}, error: ""});
  const clear = (error = "") => {
    const actions = {...store.getState().actions};
    if (actions[chatId]?.requestId !== requestId) return;
    delete actions[chatId]; store.setState({actions, ...(error ? {error} : {})});
  };
  timers.set(requestId, setTimeout(() => { finish(requestId); clear(`The kernel did not confirm the ${kind}. Refresh to check its state.`); }, 15000));
  const command = kind === "interrupt"
    ? {type: "kernel:interrupt", chat_id: chatId, request_id: requestId}
    : {type: "kernel:restart", chat_id: chatId, request_id: requestId, reason: "operator_restart"};
  if (store.send(command)) return true;
  finish(requestId); clear("Backend disconnected"); return false;
}

/** Stop the chat's whole turn, or reset its session tools. */
export function chatRuntimeAction(chatId: string, action: "stop_cell" | "reset_session_tools"): boolean {
  return !!chatId && store.getState().connected && store.send({type: "chat:runtime:action", id: chatId, action});
}

/** The newest cells from a kernel's durable ledger. */
export function loadKernelHistory(chatId: string, tail = 12): boolean {
  const state = store.getState();
  if (!state.connected || !chatId || state.history[chatId]?.requestId) return false;
  const requestId = crypto.randomUUID();
  const previous = state.history[chatId];
  store.setState({history: {...state.history, [chatId]: {requestId, items: previous?.items || [], error: ""}}});
  timers.set(requestId, setTimeout(() => {
    finish(requestId);
    const current = store.getState().history[chatId];
    if (current?.requestId === requestId) store.setState({history: {...store.getState().history, [chatId]: {...current, requestId: "", error: "No cell history reply received."}}});
  }, 15000));
  if (store.send({type: "kernel:history", chat_id: chatId, tail, request_id: requestId})) return true;
  finish(requestId);
  store.setState({history: {...store.getState().history, [chatId]: {requestId: "", items: previous?.items || [], error: "Backend disconnected"}}});
  return false;
}

function parseRow(value: unknown): KernelInventoryRow[] {
  const row = record(value), resources = record(row.resources), process = record(resources.process), tree = record(resources.tree);
  if (typeof row.chat_id !== "string" || !row.chat_id || typeof row.generation !== "number" || !Number.isSafeInteger(row.generation) || row.generation < 1 || typeof row.state !== "string") return [];
  const sampledAt = amount(resources.sampled_at);
  const cpuUser = amount(process.cpu_user_s), cpuSystem = amount(process.cpu_system_s);
  const seconds = cpuUser === null && cpuSystem === null ? null : (cpuUser || 0) + (cpuSystem || 0);
  const key = `${row.chat_id}:${row.generation}`;
  let cpuPercent: number | null = null;
  if (seconds !== null && sampledAt !== null) {
    const previous = cpuSamples.get(key);
    if (previous && sampledAt > previous.at) cpuPercent = Math.max(0, (seconds - previous.seconds) / (sampledAt - previous.at) * 100);
    if (!previous || sampledAt > previous.at) cpuSamples.set(key, {seconds, at: sampledAt});
  }
  const current = record(row.current_cell), last = record(row.last_cell), exit = record(row.last_exit);
  return [{chatId: row.chat_id, title: String(row.title || row.chat_id), generation: row.generation,
    state: row.state, busy: row.busy === true, ageSeconds: amount(row.age_s), idleSeconds: amount(row.idle_s),
    memoryBytes: amount(process.rss_bytes), treeMemoryBytes: amount(tree.rss_bytes), processes: amount(tree.processes),
    treeComplete: tree.complete === true, measurementSource: String(resources.measurement_source || "unavailable"), sampledAt,
    cpuPercent,
    currentCell: row.current_cell ? {executionId: text(current.execution_id), runId: text(current.run_id), outerToolCallId: text(current.outer_tool_call_id), startedAt: amount(current.started_at), label: text(current.label)} : null,
    queuedCells: amount(row.queued_cells) || 0,
    lastCell: row.last_cell ? {executionId: text(last.execution_id), status: text(last.status), durationMs: amount(last.duration_ms), errorCode: text(last.error_code), completedAt: amount(last.completed_at)} : null,
    lastExit: row.last_exit ? {reason: text(exit.reason), at: amount(exit.at), generation: amount(exit.generation) || 0} : null}];
}

function parseCapacity(value: unknown): ChildrenCapacity | null {
  if (!value || typeof value !== "object") return null;
  const capacity = record(value);
  return {mode: text(capacity.mode), maxActive: amount(capacity.max_active) || 0, maxAdmitted: amount(capacity.max_admitted) || 0,
    maxDepth: amount(capacity.max_depth) || 0, active: amount(capacity.active) || 0, admitted: amount(capacity.admitted) || 0};
}

export function ingestKernelInventory(message: Record<string, unknown>): void {
  const state = store.getState(), requestId = String(message.request_id || "");
  if (message.type === "kernel:inventory:result") {
    if (!requestId || state.requestId !== requestId || !state.connected) return;
    finish(requestId);
    if (!Array.isArray(message.items)) {store.setState({requestId: "", error: "Invalid kernel inventory reply"}); return;}
    store.setState({items: message.items.flatMap(parseRow), requestId: "", capacity: parseCapacity(message.children_capacity)});
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
  } else if (message.type === "kernel:accepted" || message.type === "kernel:rejected") {
    const entry = Object.entries(state.actions).find(([, action]) => action.requestId === requestId);
    if (!requestId || !entry) return;
    finish(requestId);
    const actions = {...state.actions}; delete actions[entry[0]];
    store.setState({actions, error: message.type === "kernel:rejected" ? String(message.error || `Kernel ${entry[1].kind} was rejected`) : ""});
    refreshKernelInventory();
  } else if (message.type === "kernel:history") {
    const entry = Object.entries(state.history).find(([, history]) => history.requestId === requestId);
    if (!requestId || !entry) return;
    finish(requestId);
    const items = (Array.isArray(message.items) ? message.items : []).map(value => {
      const item = record(value);
      return {sequence: amount(item.sequence) || 0, executionId: text(item.execution_id), status: text(item.status),
        startedAt: amount(item.started_at), completedAt: amount(item.completed_at), durationMs: amount(item.duration_ms),
        errorCode: text(item.error_code), label: text(item.label)};
    });
    store.setState({history: {...state.history, [entry[0]]: {requestId: "", items, error: ""}}});
  }
}
