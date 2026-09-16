import type {RuntimeContext} from "./types";
import {createModuleStore} from "./state/createModuleStore";
import {asRecord, createRequestIdFactory} from "./state/storePrimitives";
import type {SettingsCategory} from "./state/appStore";

export type ServiceSetting = {id: string; service: string; provider: string; name: string; group: string; description: string; settings_category: SettingsCategory;
  editable: boolean; configured: boolean; stored: boolean; source: string; revision: string};
type Operation = "get" | "set" | "clear";
type Receipt = {id: string; ok: boolean; error: string; operation: Operation};
const store = createModuleStore<{connected: boolean; items: ServiceSetting[]; pending: Record<string, {id: string; operation: Operation}>; receipts: Record<string, Receipt>}>({
  initialState: {connected: false, items: [], pending: {}, receipts: {}},
});
const nextId = createRequestIdFactory("service-settings");
const timers = new Map<string, ReturnType<typeof setTimeout>>();
export const useServiceSettings = store.useStore;
export const getServiceSettings = store.getState;
export const setServiceSettingsContext = (context: RuntimeContext) => store.setContext(context);
function finish(key: string, ok: boolean, error = "") {
  const state = store.getState(), entry = state.pending[key]; if (!entry) return;
  clearTimeout(timers.get(key)); timers.delete(key);
  const pending = {...state.pending}; delete pending[key];
  store.setState({pending, receipts: {...state.receipts, [key]: {...entry, ok, error}}});
}
export function setServiceSettingsConnection(status: string) {
  store.setConnected(status === "connected");
  if (status !== "connected") for (const key of Object.keys(store.getState().pending)) finish(key, false, "Connection interrupted. Refresh to check the saved value.");
}
function request(key: string, operation: Operation, payload: Record<string, unknown> = {}): string | false {
  const state = store.getState(); if (!state.connected || state.pending[key]) return false;
  const id = nextId(operation);
  store.setState({pending: {...state.pending, [key]: {id, operation}}});
  timers.set(key, setTimeout(() => finish(key, false, "No response received. Refresh before trying again."), 15000));
  if (!store.send({...payload, type: `service-settings:${operation}`, request_id: id})) finish(key, false, "Request could not be sent.");
  return id;
}
export function refreshServiceSettings() {return request("catalog", "get");}
export function changeServiceCredential(row: ServiceSetting, value: string | null, expectedRevision: string) {
  if (!row.editable) return false;
  return request(row.id, value === null ? "clear" : "set", {service: row.service, provider: row.provider, expected_revision: expectedRevision,
    ...(value === null ? {} : {key: value})});
}
export function ingestServiceSettings(message: Record<string, unknown>) {
  if (message.type !== "service-settings:result") return;
  const state = store.getState();
  const entry = Object.entries(state.pending).find(([, item]) => item.id === message.request_id && item.operation === message.operation);
  if (!entry) return;
  if (message.ok !== true) {finish(entry[0], false, String(asRecord(message.error).message || "The service setting could not be updated.")); return;}
  const result = asRecord(message.result);
  if (entry[1].operation === "get") {
    if (!Array.isArray(result.items)) {finish(entry[0], false, "Service settings could not be read."); return;}
    store.setState({items: result.items.filter(item => typeof asRecord(item).id === "string") as ServiceSetting[]});
  } else {
    const row = asRecord(result.item);
    if (row.id !== entry[0]) {finish(entry[0], false, "The saved service did not match this request."); return;}
    store.setState({items: state.items.map(item => item.id === entry[0] ? row as ServiceSetting : item)});
    if (state.pending.catalog) finish("catalog", false); // Ignore a pre-edit inventory response.
  }
  finish(entry[0], true);
}
