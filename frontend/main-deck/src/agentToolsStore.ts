import type { WsCommand } from "./protocol";
import {createModuleStore} from "./state/createModuleStore";
import type {
  AgentToolsState,
  RuntimeContext,
  WebSearchProvider,
  WebSearchProviderInfo,
} from "./types";

const store = createModuleStore<AgentToolsState>({
  initialState: {
    connected: false,
    webSearchProvider: "variant1",
    webSearchProviders: [],
    variant1Engines: ["ddg", "bing"],
    variant1SearchChecked: false,
    variant1HealthyEngines: 0,
    variant1EngineCount: 3,
    variant1SearchDegraded: false,
    variant1SearchLatencyMs: 0,
    variant1SearchCacheHit: false,
    variant1SearchError: "",
    variant1EngineHealth: {},
    searxngBaseUrl: "http://127.0.0.1:8888",
    searxngAutostart: false,
    searxngManaged: true,
    searxngReady: false,
    searxngOwned: false,
    searxngRunning: false,
    searxngDockerAvailable: false,
    searxngError: "",
    searxngRuntime: "",
    toolsReceipt: null,
  },
});

export function setAgentToolsContext(next: RuntimeContext) {
  store.setContext(next);
}

export function setAgentToolsConnection(status: string) {
  const open = status === "connected";
  if (store.getState().connected === open) return;
  store.setConnected(open);
}

export function sendAgentTools(payload: WsCommand) {
  return store.send(payload);
}

export function notifyAgentTools(message: string) {
  store.getContext()?.notify(message);
}

export function ingestAgentTools(message: Record<string, unknown>) {
  const type = String(message.type || "");
  const state = store.getState();
  if (type === "tools:accepted" || type === "tools:rejected") {
    store.setState({
      toolsReceipt: {
        requestId: String(message.request_id || ""),
        status: type === "tools:accepted" ? "accepted" : "rejected",
        error: String(message.error || ""),
      },
    });
    return;
  }
  if (type === "tools") {
    const web = (message.web_search || {}) as Record<string, unknown>;
    const searx = (web.searxng || {}) as Record<string, unknown>;
    const miy = (web.variant1 || {}) as Record<string, unknown>;
    const prov = String(web.provider || "variant1").toLowerCase();
    const known: WebSearchProvider[] = [
      "variant1", "ddgs", "brave-free", "exa", "firecrawl",
      "keenable", "parallel", "searxng", "tavily", "xai",
    ];
    const providers = (Array.isArray(web.providers) ? web.providers : []).flatMap(item => {
      if (!item || typeof item !== "object") return [];
      const row = item as Record<string, unknown>;
      const id = String(row.id || "") as WebSearchProvider;
      if (!known.includes(id)) return [];
      return [{
        id,
        name: String(row.name || id),
        description: String(row.description || ""),
        auth: (["none", "api_key", "optional", "endpoint", "shared"].includes(String(row.auth))
          ? String(row.auth)
          : "none") as WebSearchProviderInfo["auth"],
        signup_url: String(row.signup_url || ""),
        env_vars: Array.isArray(row.env_vars) ? row.env_vars.map(String) : [],
        keyless: !!row.keyless,
        configured: !!row.configured,
        available: !!row.available,
        active: !!row.active,
        config: row.config && typeof row.config === "object"
          ? row.config as Record<string, unknown>
          : {},
      } satisfies WebSearchProviderInfo];
    });
    const engines = Array.isArray(miy.engines)
      ? miy.engines.map(item => String(item || "")).filter(Boolean)
      : ["ddg", "bing"];
    const rawEngineStatus = (
      miy.engine_status && typeof miy.engine_status === "object"
        ? miy.engine_status
        : {}
    ) as Record<string, Record<string, unknown>>;
    const engineHealth = Object.fromEntries(
      Object.entries(rawEngineStatus).map(([name, item]) => [
        name,
        String(item.status || "unknown"),
      ]),
    );
    store.replaceState({
      ...state,
      connected: true,
      webSearchProvider: (known.includes(prov as WebSearchProvider)
        ? prov
        : "variant1") as WebSearchProvider,
      webSearchProviders: providers,
      variant1Engines: engines,
      variant1SearchChecked: !!miy.checked,
      variant1HealthyEngines: Number(miy.healthy_engines || 0),
      variant1EngineCount: Number(miy.engine_count || engines.length),
      variant1SearchDegraded: !!miy.degraded,
      variant1SearchLatencyMs: Number(miy.last_latency_ms || 0),
      variant1SearchCacheHit: !!miy.cache_hit,
      variant1SearchError: String(miy.last_error || ""),
      variant1EngineHealth: engineHealth,
      searxngBaseUrl: String(searx.base_url || "http://127.0.0.1:8888"),
      searxngAutostart: !!searx.autostart,
      searxngManaged: searx.managed !== false,
      searxngReady: !!searx.ready,
      searxngOwned: !!searx.owned,
      searxngRunning: !!searx.running,
      searxngDockerAvailable: !!searx.docker_available,
      searxngError: String(searx.error || ""),
      searxngRuntime: String(searx.runtime || ""),
    });
    return;
  }
  if (type === "searxng:status") {
    store.replaceState({
      ...state,
      searxngReady: !!message.ready,
      searxngOwned: !!message.owned,
      searxngRunning: !!message.running,
      searxngAutostart: typeof message.autostart === "boolean" ? message.autostart : state.searxngAutostart,
      searxngManaged: message.managed !== false,
      searxngDockerAvailable: !!message.docker_available,
      searxngError: String(message.error || ""),
      searxngRuntime: String(message.runtime || ""),
      searxngBaseUrl: String(message.base_url || state.searxngBaseUrl),
    });
    return;
  }
}

export function refreshAgentTools() {
  sendAgentTools({type: "tools:get"});
  sendAgentTools({type: "searxng:status"});
}

export function useAgentToolsState() {
  return store.useStore();
}
