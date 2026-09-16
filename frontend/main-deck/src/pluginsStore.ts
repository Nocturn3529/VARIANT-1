import {createModuleStore} from "./state/createModuleStore";
import {asRecord, createRequestIdFactory} from "./state/storePrimitives";
import type {RuntimeContext} from "./types";

export type PluginRow = Readonly<{
  package_id: string;
  name: string;
  version: string;
  active: boolean;
  description: string;
  contribution_count: number;
  contribution_kinds: string[];
  status: string;
  error: string;
  source_path: string;
}>;

type PluginsState = Readonly<{
  connected: boolean;
  plugins: PluginRow[];
  pending: Readonly<Record<string, string>>;
  loading: boolean;
  error: string;
  scanSummary: string;
}>;

const store = createModuleStore<PluginsState>({
  initialState: {
    connected: false,
    plugins: [],
    pending: {},
    loading: false,
    error: "",
    scanSummary: "",
  },
});

const nextRequestId = createRequestIdFactory("deck-plugins");

function parsePlugin(value: unknown): PluginRow | null {
  const row = asRecord(value);
  const packageId = String(row.package_id || "").trim();
  if (!packageId) return null;
  return {
    package_id: packageId,
    name: String(row.name || packageId),
    version: String(row.version || ""),
    active: !!row.active,
    description: String(row.description || ""),
    contribution_count: Number(row.contribution_count || 0),
    contribution_kinds: Array.isArray(row.contribution_kinds)
      ? row.contribution_kinds.map(String).filter(Boolean)
      : [],
    status: String(row.status || "ready"),
    error: String(row.error || ""),
    source_path: String(row.source_path || ""),
  };
}

function parsePlugins(value: unknown): PluginRow[] {
  if (!Array.isArray(value)) return [];
  return value.map(parsePlugin).filter((item): item is PluginRow => item !== null);
}

function send(
  operation: string,
  payload: {type: string; [key: string]: unknown},
): boolean {
  const request_id = nextRequestId(operation);
  if (!store.send({...payload, request_id})) {
    store.setState({error: "Plugin runtime is offline", loading: false});
    return false;
  }
  const state = store.getState();
  store.setState({
    pending: {...state.pending, [request_id]: operation},
    loading: true,
    error: "",
  });
  return true;
}

function settle(requestId: string): string {
  const state = store.getState();
  const operation = state.pending[requestId] || "";
  if (!operation) return "";
  const pending = {...state.pending};
  delete pending[requestId];
  store.setState({pending, loading: Object.keys(pending).length > 0});
  return operation;
}

export function setPluginsContext(context: RuntimeContext): void {
  store.setContext(context);
}

export function setPluginsConnection(status: string): void {
  const connected = status === "connected";
  store.setState({connected, ...(!connected ? {pending: {}, loading: false} : {})});
  if (connected) refreshPlugins();
}

export function refreshPlugins(): boolean {
  return send("list", {type: "extension-v2:list", limit: 500});
}

export function rescanPlugins(): boolean {
  return send("rescan", {type: "extension-v2:rescan"});
}

export function setPluginEnabled(packageId: string, enabled: boolean): boolean {
  return send("set-enabled", {
    type: "extension-v2:set-enabled",
    package_id: packageId,
    enabled,
  });
}

export async function openPluginsFolder(): Promise<boolean> {
  try {
    const result = await store.getContext()?.api?.openAppPath?.("plugins");
    if (result && result.ok === false) {
      store.setState({error: result.reason || "Could not open the plugins folder"});
      return false;
    }
    return true;
  } catch {
    store.setState({error: "Could not open the plugins folder"});
    return false;
  }
}

export function ingestPlugins(message: Record<string, unknown>): void {
  const type = String(message.type || "");
  if (type !== "extension-v2:accepted" && type !== "extension-v2:rejected") return;
  const operation = settle(String(message.request_id || ""));
  if (!operation) return;
  if (type === "extension-v2:rejected") {
    store.setState({error: String(message.error || "Plugin operation failed")});
    return;
  }
  const result = message.result;
  if (operation === "list") {
    store.setState({plugins: parsePlugins(result), error: ""});
    return;
  }
  if (operation === "rescan") {
    const payload = asRecord(result);
    const scan = asRecord(payload.scan);
    const errors = Array.isArray(scan.errors) ? scan.errors.length : 0;
    store.setState({
      plugins: parsePlugins(payload.plugins),
      scanSummary: errors
        ? `Scan finished with ${errors} error${errors === 1 ? "" : "s"}`
        : `Scan finished · ${Number(scan.discovered || 0)} found`,
      error: "",
    });
    return;
  }
  if (operation === "set-enabled") {
    const payload = asRecord(result);
    const packageId = String(payload.package_id || "");
    const active = !!payload.active;
    const state = store.getState();
    store.setState({
      plugins: state.plugins.map(plugin => plugin.package_id === packageId
        ? {...plugin, active}
        : plugin),
      error: "",
    });
    refreshPlugins();
  }
}

export function usePluginsState(): PluginsState {
  return store.useStore();
}

export function getPluginsState(): PluginsState {
  return store.getState();
}
